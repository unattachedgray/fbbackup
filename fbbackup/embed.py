"""Semantic embeddings for every FB Browser row — pluggable providers.

A public app can't assume any one API key, so embeddings are provider-agnostic
with a no-key LOCAL fallback. Resolution (override via FBBACKUP_EMBED_PROVIDER):

  gemini  — Google `gemini-embedding-001` (best; uses GEMINI_*_API_KEY; paid =
            no rate limits; asymmetric retrieval via taskType)
  weft    — apicascade POST /v1/embeddings (mistral-embed, 1024-d, 5-min cache,
            free-pool). Auth = the shared cascade API_KEY in ~/.hermes/.env. The
            ZERO-CONFIG default for Weft deployments: no extra key, no download.
  local   — fastembed (ONNX, CPU, no key, nothing leaves the machine). Default
            model `jinaai/jina-embeddings-v3` (retrieval-grade, multilingual,
            Gemini-class separation); set FBBACKUP_EMBED_MODEL to a concise one
            (e.g. `snowflake/snowflake-arctic-embed-m`, 0.43 GB) to trade size.

Resolution order with no override: gemini key → weft (apicascade) → local.

Corpus + query MUST use the same provider/model — the choice is recorded in
embed-meta.json and the backend reads it for query-time embedding. Outputs live
in the index dir, OUTSIDE the facebook-data folder. Re-run after download/import.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GEMINI_MODEL = "gemini-embedding-001"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:embedContent"
WEFT_EMBED_URL = os.environ.get("APICASCADE_URL", "http://localhost:8090").rstrip("/") + "/v1/embeddings"
LOCAL_MODEL = os.environ.get("FBBACKUP_EMBED_MODEL", "jinaai/jina-embeddings-v3")
# Per-provider score floor for "related" search (chat retrieval uses top-k rank).
# weft = apicascade /v1/embeddings (mistral-embed, 1024-d) — tighter separation
# than gemini/jina, so a higher floor.
THRESHOLDS = {"gemini": 0.62, "weft": 0.78, "local": 0.30}  # jina-v3 similarities run tight


def gemini_key() -> str:
    for var in ("GEMINI_PAID_API_KEY", "GEMINI_FREE_API_KEY", "GEMINI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        p = Path.home() / "dev" / "weft" / "apicascade" / ".env.local"
        kv = dict(l.split("=", 1) for l in p.read_text(encoding="utf-8").splitlines()
                  if "=" in l and not l.strip().startswith("#"))
        for var in ("GEMINI_PAID_API_KEY", "GEMINI_FREE_API_KEY"):
            if kv.get(var):
                return kv[var].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def weft_key() -> str:
    """apicascade /v1/embeddings auth — shared cascade secret in ~/.hermes/.env."""
    try:
        for line in (Path.home() / ".hermes" / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def resolve_provider() -> str:
    forced = os.environ.get("FBBACKUP_EMBED_PROVIDER")
    if forced:
        return forced
    if gemini_key():       # best quality (asymmetric retrieval via taskType)
        return "gemini"
    if weft_key():         # zero-config for Weft deployments (apicascade, cached)
        return "weft"
    return "local"         # no key at all → fastembed jina-v3 on CPU


# ── Gemini ───────────────────────────────────────────────────────────────────
def _gemini_one(text: str, key: str, task: str) -> list[float]:
    body = json.dumps({"content": {"parts": [{"text": text}]}, "taskType": task}).encode()
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"{GEMINI_URL}?key={key}", data=body,
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())["embedding"]["values"]
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    return []


# ── weft (apicascade /v1/embeddings → mistral-embed, symmetric, cached) ───────
def _weft_embed(texts: list[str], key: str, batch: int = 128) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        body = json.dumps({"input": texts[i:i + batch]}).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    WEFT_EMBED_URL, data=body,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
                d = json.loads(urllib.request.urlopen(req, timeout=90).read())
                out.extend(x["embedding"] for x in sorted(d["data"], key=lambda e: e["index"]))
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
    return out


# ── local (fastembed) ────────────────────────────────────────────────────────
_LOCAL = None


def _local_model():
    global _LOCAL
    if _LOCAL is None:
        from fastembed import TextEmbedding
        # GPU is opt-in (needs the `fbbackup[gpu]` extra → onnxruntime-gpu). The
        # setup wizard sets FBBACKUP_EMBED_DEVICE=gpu when it detects a GPU;
        # CUDA→CPU fallback is automatic if the GPU provider isn't available.
        if os.environ.get("FBBACKUP_EMBED_DEVICE", "").lower() == "gpu":
            _LOCAL = TextEmbedding(LOCAL_MODEL, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        else:
            _LOCAL = TextEmbedding(LOCAL_MODEL)
    return _LOCAL


def _local_embed(texts: list[str], is_query: bool) -> list[list[float]]:
    # jina-v3 / e5 / arctic are asymmetric; the query/passage prefixes matter.
    # Small batch by default: jina-v3's attention is memory-heavy — batch 256 OOMs
    # even a 24GB GPU. FBBACKUP_EMBED_BATCH tunes it (raise for lighter models).
    pfx = "query: " if is_query else "passage: "
    bs = int(os.environ.get("FBBACKUP_EMBED_BATCH", "16"))
    return [list(map(float, v)) for v in _local_model().embed([pfx + t for t in texts], batch_size=bs)]


# ── batched embedding + resumable checkpoint ─────────────────────────────────
# A whole-archive embed on a paced free key can take a long time; persist
# progress per batch so an interruption (browser close, 429 storm, power loss)
# resumes instead of re-embedding everything. The checkpoint is keyed to a
# fingerprint of (provider, model, row ids) so a changed corpus invalidates it.
def _embed_batch(provider: str, texts: list[str], key: str) -> list[list[float]]:
    if provider == "gemini":
        with ThreadPoolExecutor(max_workers=16) as ex:
            return list(ex.map(lambda t: _gemini_one(t, key, "RETRIEVAL_DOCUMENT"), texts))
    if provider == "weft":
        return _weft_embed(texts, key)
    return _local_embed(texts, False)


def _fingerprint(provider: str, model: str, ids: list[str]) -> str:
    import hashlib
    h = hashlib.sha256(f"{provider}\0{model}\0{len(ids)}".encode())
    for i in ids:
        h.update(b"\0")
        h.update(i.encode())
    return h.hexdigest()[:16]


def _ckpt_paths(out_dir: Path) -> tuple[Path, Path]:
    return out_dir / "embed-ckpt.npy", out_dir / "embed-ckpt.json"


def _load_ckpt(out_dir: Path, fp: str) -> list[list[float]]:
    import numpy as np
    npy, js = _ckpt_paths(out_dir)
    try:
        meta = json.loads(js.read_text(encoding="utf-8"))
        if meta.get("fingerprint") == fp and npy.exists():
            return [list(map(float, v)) for v in np.load(npy)]
    except Exception:  # corrupt/mismatched checkpoint → start clean
        pass
    return []


def _save_ckpt(out_dir: Path, fp: str, vecs: list[list[float]], provider: str, model: str) -> None:
    import numpy as np
    npy, js = _ckpt_paths(out_dir)
    np.save(npy, np.asarray(vecs, dtype="float32"))
    js.write_text(json.dumps({"fingerprint": fp, "done": len(vecs),
                              "provider": provider, "model": model}), encoding="utf-8")


def _clear_ckpt(out_dir: Path) -> None:
    for p in _ckpt_paths(out_dir):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ── unified API (used by the backend for the query, and embed() for the bulk) ─
def embed_query(text: str, provider: str | None = None, key: str | None = None) -> list[float]:
    provider = provider or resolve_provider()
    if provider == "gemini":  # keys resolved per provider (caller's key may be for another)
        return _gemini_one(text, gemini_key(), "RETRIEVAL_QUERY")
    if provider == "weft":
        return _weft_embed([text], weft_key())[0]
    return _local_embed([text], True)[0]


def _content(text: str) -> str:
    """Clean substantive text for embedding: drop frontmatter, the H1 title, and
    image/link markup. Returns "" only for posts with NO real text, or for pure
    RESHARES whose text is just the boilerplate label (content == title and
    type == share) — those would pollute search. A single-line status/photo/link
    post has content == title too (the writer duplicates the text as title+body),
    but that text is REAL, so it IS embedded. (Earlier this dropped ~3k genuine
    short posts — see the type breakdown in the FB-distribution notes.)"""
    typ, body = "", text
    if body.startswith("---"):
        rest = body[3:].lstrip("\n")
        end = rest.find("\n---")
        if end != -1:
            m = re.search(r"^type:\s*(\S+)", rest[:end], re.M)
            typ = m.group(1) if m else ""
            body = rest[end + 4:].lstrip("\n")
    title, lines = "", []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# ") and not title:
            title = s[2:].strip()
            continue
        if s.startswith(("#", "![", "🔗", "[▶", "📍")):
            continue
        lines.append(s)
    content = " ".join(" ".join(lines).split())
    if not content:
        return ""
    if content == title and typ == "share":  # reshare boilerplate only
        return ""
    return content[:1500]


def embed(spaces_root: Path, out_dir: Path, workspace: str = "default") -> dict:
    import numpy as np

    provider = resolve_provider()
    key = gemini_key() if provider == "gemini" else (weft_key() if provider == "weft" else "")
    if provider in ("gemini", "weft") and not key:
        raise SystemExit(f"provider={provider} but no key")
    spaces_root = Path(spaces_root).expanduser()
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = spaces_root / workspace

    ids, texts = [], []
    for ydir in sorted(base.iterdir()):
        if not ydir.is_dir() or ydir.name.startswith("."):
            continue
        for p in sorted(ydir.glob("*.md")):
            txt = _content(p.read_text(encoding="utf-8", errors="replace"))
            if not txt:
                continue
            ids.append(f"{workspace}/{ydir.name}/{p.stem}")
            texts.append(txt)

    if not texts:
        print("no embeddable rows found (all rows are trivial/empty) — skipping "
              "embeddings; keyword search still works.", flush=True)
        return {"provider": provider, "count": 0, "dim": 0}

    model = {"gemini": GEMINI_MODEL, "weft": "mistral-embed (apicascade)"}.get(provider, LOCAL_MODEL)
    # Batch sizes per provider (remote = fewer requests under a rate limit).
    batch = {"gemini": 256, "weft": 128}.get(provider, 256)
    fp = _fingerprint(provider, model, ids)
    vecs = _load_ckpt(out_dir, fp)  # resume a prior run for THIS exact corpus
    start = len(vecs)
    if start:
        print(f"resuming embed at {start}/{len(texts)} via {provider} (checkpoint) …", flush=True)
    else:
        print(f"embedding {len(texts)} rows via {provider} ({model}) …", flush=True)
    for i in range(start, len(texts), batch):
        vecs.extend(_embed_batch(provider, texts[i:i + batch], key))
        _save_ckpt(out_dir, fp, vecs, provider, model)  # checkpoint each batch
        print(f"  …{min(i + batch, len(texts))}/{len(texts)}", flush=True)

    arr = np.asarray(vecs, dtype="float32")
    arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)
    np.save(out_dir / "embeddings.npy", arr)
    (out_dir / "embed-ids.json").write_text(json.dumps(ids), encoding="utf-8")
    (out_dir / "embed-meta.json").write_text(json.dumps(
        {"provider": provider, "model": model, "dim": int(arr.shape[1]),
         "count": len(ids), "threshold": THRESHOLDS.get(provider, 0.5)}), encoding="utf-8")
    _clear_ckpt(out_dir)  # done → drop the resume checkpoint
    print(f"saved {arr.shape} ({provider}) -> {out_dir / 'embeddings.npy'}", flush=True)
    return {"provider": provider, "count": len(ids), "dim": int(arr.shape[1])}


if __name__ == "__main__":
    embed(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("spaces-data"),
          Path(sys.argv[2]) if len(sys.argv) > 2 else Path("index"))
