"""spaces-lite backend — Weft's Spaces API, ported standalone.

A faithful port of Weft's Notion-like database API (weftdb.py + the object
endpoints from weftbase.py + image serving from files.py), decoupled from the
Weft agent harness: its own data root, no cookie/OAuth, loopback-open with an
optional bearer token gating writes (so a shared archive stays read-only).

A *database* is a folder of notes under ``<spaces_root>/<workspace>/<name>/``;
a *row* is one ``.md`` file; *properties* are YAML frontmatter; *relations* are
``[[wiki-links]]``. The notes are the single source of truth — agents and
Obsidian edit the same files. Endpoints (same wire contract as Weft, so the
ported SpacesPage frontend talks to it unchanged):

    GET  /api/wiki/db                     list databases in a workspace
    GET  /api/wiki/db/{name}              inferred schema + rows
    POST /api/wiki/db/{name}/set          set one frontmatter property
    POST /api/wiki/db/{name}/new          create a row
    POST /api/wiki/db/{name}/export-base  write an Obsidian .base file
    GET  /api/wiki/objects/{id}           read a note (frontmatter + body)
    POST /api/wiki/objects/{id}           write a note
    POST /api/wiki/append                 append to a note
    GET  /api/weft/files?path=&w=         serve an allowlisted image (+thumbnail)
"""
from __future__ import annotations

import datetime as _dt
import hmac
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

# ── regexes / constants (from weftdb.py + weftbase.py) ───────────────────────
_NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")
_KEY_OK = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")
_SLUG_OK = re.compile(r"^[a-zA-Z0-9._\-/]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VID_EXTS = {".mp4", ".mov", ".webm"}
_LOOPBACK = ("127.0.0.1", "::1", "localhost", None, "")
_THUMB_MIN_W, _THUMB_MAX_W = 64, 1024


# ── frontmatter + type inference (weftdb.py) ─────────────────────────────────
def _is_link_val(v: Any) -> bool:
    if isinstance(v, str):
        return bool(_WIKILINK_RE.search(v))
    if isinstance(v, list):
        return len(v) > 0 and all(isinstance(x, str) and _WIKILINK_RE.search(x) for x in v)
    return False


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        rest = text[3:].lstrip("\n")
        end = rest.find("\n---")
        if end != -1:
            try:
                props = yaml.safe_load(rest[:end]) or {}
            except Exception:
                props = {}
            if not isinstance(props, dict):
                props = {}
            return props, rest[end + 4:].lstrip("\n")
    return {}, text


def _join_frontmatter(props: dict, body: str) -> str:
    body = body.lstrip("\n")
    if not props:
        return body if body.endswith("\n") else body + "\n"
    fm = yaml.safe_dump(props, sort_keys=False, allow_unicode=True).strip("\n")
    return f"---\n{fm}\n---\n\n{body}".rstrip("\n") + "\n"


def _infer_type(values: list) -> str:
    vals = [v for v in values if v is not None and v != ""]
    if not vals:
        return "text"
    if all(_is_link_val(v) for v in vals):
        return "relation"
    if all(isinstance(v, bool) for v in vals):
        return "checkbox"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "number"
    if all(isinstance(v, (_dt.date, _dt.datetime)) for v in vals):
        return "date"
    if all(isinstance(v, str) and _DATE_RE.match(v) for v in vals):
        return "date"
    if all(isinstance(v, str) for v in vals):
        distinct = set(vals)
        if len(distinct) <= max(8, len(vals) // 2) and all(len(v) <= 40 for v in vals):
            return "select"
    return "text"


class SpacesBackend:
    """Holds the data root + media allowlist; registers routes on an app."""

    def __init__(self, spaces_root: Path, media_roots: list[Path], write_token: str = "",
                 prefix: str = "/api/fb", profile_url: str = "",
                 llm_url: str = "", llm_key: str = "", embed_key: str = "",
                 chat_model: str = "weft-mixed"):
        self.root = Path(spaces_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Allowlisted roots for image serving: the vault (for _attachments) +
        # the read-only FB media roots.
        self.media_roots = [self.root] + [Path(m).expanduser().resolve() for m in media_roots]
        self.write_token = write_token
        # Route prefix — kept distinct from Weft's /api/wiki + /api/weft/files
        # so this can mount on the Weft dashboard app without colliding.
        self.prefix = prefix
        self.profile_url = profile_url
        # LLM for the archive chat (RAG). Empty url = chat disabled. `chat_model`
        # is an apicascade tier: weft-mixed (free+paid → Opus 4.8 for subscribers,
        # cascades to free for keyless deployers), weft-frontier, or auto.
        self.llm_url = llm_url
        self.llm_key = llm_key
        self.chat_model = chat_model
        # Mistral key for embedding the search/chat QUERY (same provider as the
        # offline `fbbackup embed` step). Empty = semantic search disabled.
        self.embed_key = embed_key
        # Per-workspace embeddings cache: workspace → {emb, ids, threshold, provider}.
        self._emb_cache: dict[str, dict] = {}
        # In-memory browse/search index (built once, lazily) — reading 5k+ .md
        # files per request was the slow path. None = not built yet.
        self._indexes: dict[str, dict] = {}  # per-workspace browse/search index
        # Serialize index builds. Without this, a burst of requests after a
        # cold start (the FB page fires /meta + /db + /search at once) each see
        # _index is None and rebuild all 7k+ files concurrently — 3x the
        # GIL/disk pressure, 15-21s builds that starve the async event loop and
        # trip the dashboard watchdog into a restart loop. With the lock the
        # first caller builds, the rest wait and reuse the result.
        self._index_lock = threading.Lock()
        # Semantic search: local embeddings (index/embeddings.npy) + the query
        # model, both loaded lazily. None = not loaded / unavailable.
        self._index_dir = self.root.parent / "index"
        self._emb_model = None    # fastembed model for embedding the query (shared)

    # ── path / id helpers (weftbase.py) ──────────────────────────────────────
    def _ws_root(self, workspace: str) -> Path:
        if not _NAME_OK.match(workspace):
            raise HTTPException(400, "invalid workspace")
        return (self.root / workspace).resolve()

    def _db_root(self, workspace: str, name: str) -> Path:
        if not _NAME_OK.match(name):
            raise HTTPException(400, "invalid database")
        base = self._ws_root(workspace)
        r = (base / name).resolve()
        if not str(r).startswith(str(base) + "/") and r != base:
            raise HTTPException(400, "path escapes workspace")
        return r

    def _parse_object_id(self, object_id: str) -> tuple[str, str]:
        s = object_id.strip("/")
        if not s:
            raise HTTPException(400, "object id required")
        if "/" not in s:
            return ("default", s)
        head, rest = s.split("/", 1)
        return (head, rest)

    def _validate_path(self, workspace: str, relpath: str) -> Path:
        if not workspace or not _SLUG_OK.match(workspace):
            raise HTTPException(400, "invalid workspace")
        if not relpath or not _SLUG_OK.match(relpath) or ".." in relpath.split("/"):
            raise HTTPException(400, "invalid path")
        if not relpath.endswith(".md"):
            relpath += ".md"
        root = self._ws_root(workspace)
        root.mkdir(parents=True, exist_ok=True)
        full = (root / relpath).resolve()
        if not str(full).startswith(str(root)):
            raise HTTPException(400, "path escapes workspace")
        return full

    def _page_id(self, workspace: str, p: Path) -> str:
        rel = p.relative_to(self._ws_root(workspace))
        return f"{workspace}/{rel.as_posix()[:-3]}"

    def _all_pages(self, workspace: str) -> list[Path]:
        root = self._ws_root(workspace)
        if not root.exists():
            return []
        return sorted((p for p in root.rglob("*.md") if p.is_file()), key=lambda p: str(p))

    @staticmethod
    def _read_h1_and_summary(p: Path) -> tuple[str, str]:
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return (p.stem, "")
        _, body = _split_frontmatter(body) if body.startswith("---") else ({}, body)
        m = _H1_RE.search(body)
        title = m.group(1).strip() if m else p.stem
        summary = ""
        for raw in body.splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or s.startswith(">"):
                continue
            summary = s[:140]
            break
        return (title, summary)

    @staticmethod
    def _extract_links(text: str) -> list[str]:
        return [m.strip() for m in _WIKILINK_RE.findall(text)]

    def _rewrite_images(self, content: str, note_dir: Path) -> str:
        files = f"{self.prefix}/files"
        def _repl(m):
            alt, src = m.group(1), m.group(2).strip()
            if src.startswith(("http://", "https://", files)):
                return m.group(0)
            p = Path(src)
            if not p.is_absolute():
                p = note_dir / src
            return f"![{alt}]({files}?path={quote(str(p))})"
        return _IMG_RE.sub(_repl, content)

    # ── database read/write (weftdb.py) ──────────────────────────────────────
    def _row(self, workspace: str, p: Path) -> dict:
        props, _ = _split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        title, summary = self._read_h1_and_summary(p)
        return {"id": self._page_id(workspace, p), "title": title, "summary": summary, "props": props}

    def _resolve_links(self, workspace: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in self._all_pages(workspace):
            out.setdefault(p.stem.lower(), self._page_id(workspace, p))
        return out

    # ── in-memory index: speed + search + grey-out empties ───────────────────
    def _build_index(self, workspace: str = "default") -> dict:
        rows: list[dict] = []
        by_year: dict[str, list] = {}
        base = self._ws_root(workspace)
        if base.is_dir():
            for ydir in sorted(base.iterdir()):
                if not ydir.is_dir() or ydir.name.startswith("."):
                    continue
                yr = ydir.name
                for p in sorted(ydir.glob("*.md")):
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    props, body = _split_frontmatter(text)
                    title, summary = self._read_h1_and_summary(p)
                    tags = props.get("tags")
                    tagstr = " ".join(tags) if isinstance(tags, list) else str(tags or "")
                    # "empty" (grey) = nothing to read/see/click beyond the title:
                    # a pure reshare, OR a one-liner whose only text just repeats
                    # the title — and no media/link/video to look at.
                    text_lines = [s for line in body.splitlines()
                                  if (s := line.strip()) and not s.startswith(("#", "![", "🔗", "[▶", "📍"))]
                    text_content = " ".join(" ".join(text_lines).split())
                    has_visual = ("![" in body) or ("🔗" in body) or ("[▶" in body)
                    trivial = (not text_content) or (text_content == " ".join(title.split()))
                    empty = trivial and not has_visual
                    row = {
                        "id": self._page_id(workspace, p), "title": title, "summary": summary,
                        "props": props, "year": yr, "empty": empty,
                        "_blob": (title + " " + body + " " + tagstr).lower(),
                    }
                    rows.append(row)
                    by_year.setdefault(yr, []).append(row)
        idx = {"rows": rows, "by_year": by_year, "by_id": {r["id"]: r for r in rows}}
        self._indexes[workspace] = idx
        return idx

    # ── semantic search (per-workspace local embeddings) ─────────────────────
    def _emb_dir(self, workspace: str) -> Path:
        # FB (default) embeddings live in index/ (legacy); others in index/<ws>/.
        return self._index_dir if workspace == "default" else (self._index_dir / workspace)

    def _load_embeddings(self, workspace: str = "default") -> None:
        if workspace in self._emb_cache:
            return
        ent: dict = {"emb": None, "ids": None, "threshold": 0.62, "provider": "gemini"}
        try:
            import numpy as np
            d = self._emb_dir(workspace)
            arr, ids, meta = d / "embeddings.npy", d / "embed-ids.json", d / "embed-meta.json"
            if arr.is_file() and ids.is_file():
                ent["emb"] = np.load(arr)
                ent["ids"] = json.loads(ids.read_text(encoding="utf-8"))
                if meta.is_file():
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    ent["threshold"] = float(m.get("threshold", 0.62))
                    ent["provider"] = m.get("provider", "gemini")
        except Exception:
            ent["emb"] = None
        self._emb_cache[workspace] = ent

    def _embed_query(self, q: str, provider: str):
        try:
            import numpy as np
            from .embed import embed_query
            v = np.asarray(embed_query(q, provider, self.embed_key), dtype="float32")
            return v / (np.linalg.norm(v) + 1e-9)
        except Exception:
            return None

    def _semantic(self, q: str, workspace: str = "default", k: int = 40, exclude: set | None = None) -> list[dict]:
        self._load_embeddings(workspace)
        ent = self._emb_cache[workspace]
        if ent["emb"] is None or not ent["ids"]:
            return []
        import numpy as np
        qv = self._embed_query(q, ent["provider"])
        if qv is None:
            return []
        sims = ent["emb"] @ qv
        order = np.argsort(-sims)
        byid = self._ensure_index(workspace)["by_id"]
        out, ex = [], (exclude or set())
        for i in order[: k * 3]:
            if sims[i] < ent["threshold"]:
                break
            rid = ent["ids"][i]
            if rid in ex:
                continue
            row = byid.get(rid)
            if row:
                out.append(row)
            if len(out) >= k:
                break
        return out

    # ── archive chat (RAG: semantic retrieve → LLM) ──────────────────────────
    def _row_text(self, row: dict) -> str:
        try:
            ws, rel = self._parse_object_id(row["id"])
            full = self._validate_path(ws, rel)
            if full.is_file():
                return _split_frontmatter(full.read_text(encoding="utf-8", errors="replace"))[1]
        except Exception:
            pass
        return row.get("summary", "")

    def _ask(self, question: str, workspace: str = "default", k: int = 8) -> dict:
        hits = self._semantic(question, workspace, k=k)
        if not hits:  # fall back to keyword
            toks = [t for t in question.lower().split() if t]
            hits = [r for r in self._ensure_index(workspace)["rows"] if toks and all(t in r["_blob"] for t in toks)][:k]
        ctx = []
        for r in hits[:k]:
            body = " ".join(self._row_text(r).split())[:450]
            ctx.append(f"[{r['props'].get('date', '?')}] {body}")
        prompt = (
            "You are helping the user explore their OWN Facebook post archive. Answer the question "
            "using only the posts below; cite the relevant dates; if they don't cover it, say so. The "
            "posts are often in Korean — reply in the user's language.\n\nRelevant posts:\n"
            + "\n---\n".join(ctx) + f"\n\nQuestion: {question}\nAnswer:"
        )
        return {"answer": self._llm(prompt),
                "sources": [{"id": r["id"], "title": r["title"], "year": r.get("year")} for r in hits[:k]]}

    def _llm(self, prompt: str) -> str:
        if not self.llm_url:
            return "Chat isn't configured (no LLM endpoint)."
        import urllib.request
        try:
            data = json.dumps({"model": self.chat_model, "messages": [{"role": "user", "content": prompt}],
                               "temperature": 0.4, "max_tokens": 800}).encode()
            req = urllib.request.Request(
                self.llm_url.rstrip("/") + "/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.llm_key}"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception as e:
            return f"(LLM error: {str(e)[:140]})"

    def _ensure_index(self, workspace: str = "default") -> dict:
        # Double-checked locking: the common case (index already built) takes no
        # lock; only the cold/invalidated path serializes through _index_lock so
        # concurrent callers don't stampede a rebuild over 7k+ files.
        idx = self._indexes.get(workspace)
        if idx is not None:
            return idx
        with self._index_lock:
            if workspace not in self._indexes:
                self._build_index(workspace)
            return self._indexes[workspace]

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "_blob"}

    def _schema_of(self, rows: list[dict]) -> list[dict]:
        keys: dict[str, list] = {}
        for r in rows:
            for k, v in r["props"].items():
                keys.setdefault(str(k), []).append(v)
        schema = []
        for k, vs in keys.items():
            t = _infer_type(vs)
            opts = sorted({str(x) for x in vs if isinstance(x, str)})[:20] if t == "select" else None
            schema.append({"key": k, "type": t, "options": opts})
        return schema

    def _read_db(self, workspace: str, name: str) -> dict:
        idx = self._ensure_index(workspace)
        rows_full = idx["by_year"].get(name, [])
        return {"name": name, "workspace": workspace,
                "schema": self._schema_of(rows_full),
                "rows": [self._public(r) for r in rows_full]}

    def _emit_base(self, workspace: str, name: str) -> Path:
        data = self._read_db(workspace, name)
        cols = [c["key"] for c in data["schema"]]
        base = {
            "filters": {"and": [f'file.inFolder("{name}")']},
            "views": [
                {"type": "table", "name": "Table", **({"order": cols} if cols else {})},
                {"type": "cards", "name": "Cards"},
            ],
        }
        out = self._ws_root(workspace) / f"{name}.base"
        out.write_text(yaml.safe_dump(base, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return out

    def _set_prop(self, object_id: str, key: str, value: Any) -> None:
        if not _KEY_OK.match(key):
            raise HTTPException(400, "invalid property key")
        ws, rel = self._parse_object_id(object_id)
        full = self._validate_path(ws, rel)
        if not full.is_file():
            raise HTTPException(404, "row not found")
        text = full.read_text(encoding="utf-8", errors="replace")
        props, body = _split_frontmatter(text)
        if value is None or value == "":
            props.pop(key, None)
        else:
            props[key] = value
        hist = full.parent / ".history" / full.stem
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"{int(time.time())}.md").write_text(text, encoding="utf-8")
        full.write_text(_join_frontmatter(props, body), encoding="utf-8")

    # ── auth: loopback-open; writes gated by token when one is configured ─────
    def _require_write(self, request: Request) -> None:
        if not self.write_token:
            return
        supplied = (request.headers.get("x-spaces-token") or "").strip()
        if supplied and hmac.compare_digest(supplied.encode(), self.write_token.encode()):
            return
        if (request.client.host if request.client else None) in _LOOPBACK:
            return
        raise HTTPException(401, "write requires token")

    def _export_max_ts(self) -> int:
        """Max timestamp (ms) across the export's posts.jsonl, cached. 0 if none.
        Live imports go to spaces-data (not posts.jsonl), so this stays the EXPORT
        boundary — exactly what a first scan should stop at."""
        cached = getattr(self, "_cutoff_ms_cache", None)
        if cached is not None:
            return cached
        mx = 0
        try:
            with (self._index_dir / "posts.jsonl").open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        t = json.loads(line).get("timestamp", 0)
                    except Exception:
                        continue
                    if t and t > mx:
                        mx = t
        except Exception:
            pass
        self._cutoff_ms_cache = mx * 1000  # seconds → ms (the scanner uses ms)
        return self._cutoff_ms_cache

    def _embed_status(self) -> dict:
        """Whether semantic search is stale — i.e. posts were imported since the
        last embed run. (A row-count diff is wrong: embed.py intentionally skips
        trivial posts, so embedded < total even when fully up to date.) We compare
        an import freshness marker against the embeddings' mtime instead."""
        st = getattr(self, "_reembed_state", {"running": False, "done": False, "error": None})
        meta_p = self._index_dir / "embed-meta.json"
        marker = self._index_dir / ".last-import"
        embedded = 0
        try:
            embedded = int(json.loads(meta_p.read_text(encoding="utf-8")).get("count", 0))
        except Exception:
            pass
        pending = False
        try:
            if marker.is_file() and (not meta_p.is_file()
                                     or marker.stat().st_mtime > meta_p.stat().st_mtime):
                pending = True
        except Exception:
            pass
        return {"running": bool(st.get("running")), "done": bool(st.get("done")),
                "error": st.get("error"), "embedded": embedded,
                "has_embeddings": meta_p.is_file(), "pending": pending}

    def _start_reembed(self) -> dict:
        """Re-run embeddings over all rows (incl. live imports) in the background."""
        st = getattr(self, "_reembed_state", None)
        if st and st.get("running"):
            return {"ok": True, "running": True}
        self._reembed_state = {"running": True, "done": False, "error": None}

        def _run() -> None:
            try:
                from .embed import embed
                embed(self.root, self._index_dir)
                self._emb_cache.pop("default", None)  # force reload of the new vectors
                self._load_embeddings("default")
                self._reembed_state = {"running": False, "done": True, "error": None}
            except Exception as e:  # noqa: BLE001
                self._reembed_state = {"running": False, "done": False, "error": str(e)}
        threading.Thread(target=_run, name="fb-reembed", daemon=True).start()
        return {"ok": True, "running": True}

    def _thumb(self, abs_path: Path, w: int) -> Path | None:
        import hashlib
        if abs_path.suffix.lower() == ".gif":
            return None
        try:
            from PIL import Image
        except Exception:
            return None
        try:
            st = abs_path.stat()
            cache = self.root.parent / ".thumb-cache"
            key = hashlib.sha256(f"{abs_path}|{w}|{int(st.st_mtime)}|{st.st_size}".encode()).hexdigest()
            out = cache / f"{key}.webp"
            if out.is_file():
                return out
            with Image.open(abs_path) as im:
                if im.width <= w:
                    return None
                h = max(1, round(im.height * (w / im.width)))
                im = im.convert("RGB") if im.mode not in ("RGBA", "LA", "P") else im.convert("RGBA")
                im = im.resize((w, h), getattr(getattr(Image, "Resampling", Image), "LANCZOS"))
                cache.mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(out.name + ".tmp")
                im.save(tmp, "WEBP", quality=80, method=4)
                tmp.replace(out)
            return out
        except Exception:
            return None

    def _unfurl(self, url: str) -> dict:
        """Open Graph preview (title/description/image/site) for a URL, with a
        Wayback Machine fallback for dead links (common across an 18-year
        archive). Cached to disk (positive AND negative). SSRF-guarded; http(s)
        only; non-ASCII URLs percent-encoded so they don't crash urllib.
        """
        import hashlib
        import ipaddress
        import json as _json
        import re as _re
        import socket
        import urllib.request
        from urllib.parse import quote, urljoin, urlparse

        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return {"ok": False, "url": url}

        cache = self.root.parent / ".unfurl-cache"
        cf = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if cf.is_file():
            try:
                return _json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                pass

        UA = {"User-Agent": "Mozilla/5.0 (fbbackup unfurl)"}

        def ascii_url(u: str) -> str:
            try:
                u.encode("ascii")
                return u
            except UnicodeEncodeError:
                return quote(u, safe=":/?#[]@!$&'()*+,;=%~")

        def og(html: str, prop: str) -> str:
            for pat in (
                r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)["\']',
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']',
            ):
                m = _re.search(pat % _re.escape(prop), html, _re.I)
                if m:
                    return m.group(1).strip()
            return ""

        def fetch(target: str) -> dict:
            """Preview dict for one URL (live fetch). url field stays the original."""
            host = urlparse(target).hostname or ""
            if host.replace("www.", "").replace("m.", "") in ("youtube.com", "youtu.be"):
                try:  # YouTube serves bots a consent page — oEmbed is reliable.
                    oreq = urllib.request.Request(
                        "https://www.youtube.com/oembed?format=json&url=" + quote(target, safe=""), headers=UA)
                    with urllib.request.urlopen(oreq, timeout=6) as o:
                        j = _json.loads(o.read(131072))
                    return {"ok": True, "url": url, "title": (j.get("title") or "")[:300],
                            "description": j.get("author_name") or "", "image": j.get("thumbnail_url") or "",
                            "site": "YouTube"}
                except Exception as e:
                    return {"ok": False, "url": url, "error": str(e)[:120]}
            try:  # SSRF guard
                ip = ipaddress.ip_address(socket.gethostbyname(host))
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return {"ok": False, "url": url, "error": "blocked host"}
            except (socket.gaierror, ValueError):
                pass
            try:
                with urllib.request.urlopen(urllib.request.Request(ascii_url(target), headers=UA), timeout=6) as resp:
                    html = resp.read(524288).decode("utf-8", errors="replace")
            except Exception as e:
                return {"ok": False, "url": url, "error": str(e)[:120]}
            title = og(html, "og:title") or og(html, "twitter:title")
            if not title:
                m = _re.search(r"<title[^>]*>([^<]*)</title>", html, _re.I)
                title = m.group(1).strip() if m else ""
            image = og(html, "og:image") or og(html, "twitter:image")
            return {"ok": True, "url": url, "title": title[:300],
                    "description": (og(html, "og:description") or og(html, "twitter:description") or og(html, "description"))[:500],
                    "image": urljoin(target, image) if image else "",
                    "site": og(html, "og:site_name") or host}

        out = fetch(url)
        if not out["ok"]:
            try:  # Wayback Machine — resurrect a dead link from the Internet Archive.
                wreq = urllib.request.Request(
                    "https://archive.org/wayback/available?url=" + quote(url, safe=""), headers=UA)
                with urllib.request.urlopen(wreq, timeout=6) as o:
                    snap = _json.loads(o.read(65536)).get("archived_snapshots", {}).get("closest", {})
                if snap.get("available") and snap.get("url"):
                    wb = fetch(snap["url"])
                    if wb["ok"]:
                        wb["archived_url"] = snap["url"]
                        wb["site"] = (wb.get("site") or "") + " · via Internet Archive"
                        out = wb
            except Exception:
                pass

        # Cache successes + permanent failures; NOT transient ones (rate-limit /
        # timeout) so a dead link can still be archived-resolved on a later view.
        err = out.get("error", "")
        transient = (not out["ok"]) and any(s in err for s in ("429", "timed out", "timeout", "Temporary"))
        if not transient:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                cf.write_text(_json.dumps(out), encoding="utf-8")
            except Exception:
                pass
        return out

    # ── live import (Firefox extension scan mode) ────────────────────────────
    def _download_media(self, url: str, dest_dir: Path, idx: int) -> Path | None:
        """Download one scraped media URL into the live-media dir (so it's a
        permanent local backup, served in place like the export media)."""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fbbackup)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(25 * 1024 * 1024)  # cap 25 MB
                ct = (resp.headers.get("Content-Type") or "").split(";")[0]
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                   "image/gif": ".gif", "video/mp4": ".mp4"}.get(ct, ".jpg")
            dest_dir.mkdir(parents=True, exist_ok=True)
            p = dest_dir / f"{idx}{ext}"
            p.write_bytes(data)
            return p
        except Exception:
            return None

    def _prep_live(self, p: dict) -> tuple[dict, str, list[str]]:
        """Map a scraped-post payload → a canonical post (same shape as the
        export parser), the year bucket, and the image URLs to download."""
        import hashlib
        from datetime import datetime
        pt = p.get("post_time") or p.get("timestamp") or 0
        ts = int(pt / 1000) if pt and pt > 1e12 else int(pt)
        text = (p.get("body_text") or p.get("text") or "").strip()
        orig_author = (p.get("original_author") or "").strip()
        orig_text = (p.get("original_text") or "").strip()
        if orig_author or orig_text:
            attribution = (f"— {orig_author}:\n" if orig_author else "") + orig_text
            text = (text + "\n\n" + attribution).strip()
        links: list[dict] = []
        orig_url = p.get("original_url") or ""
        src = p.get("source_url") or ""
        if orig_url:
            links.append({"url": orig_url, "name": orig_author, "source": "Facebook"})
        elif src:
            links.append({"url": src, "name": "", "source": "Facebook"})
        for yt in p.get("youtube_urls") or []:
            links.append({"url": yt, "name": "", "source": "YouTube"})
        images = [u for u in (p.get("image_urls") or []) if u]
        fb_id = (p.get("fb_id") or hashlib.sha1(f"{ts}|{text}|{src}".encode()).hexdigest()[:16])
        fb_id = re.sub(r"[^A-Za-z0-9]", "", fb_id)[:24] or "live"
        dt = datetime.fromtimestamp(ts).isoformat() if ts else ""
        hashtags = sorted(set(re.findall(r"#(\w+)", text, re.UNICODE)))
        typ = ("share" if p.get("is_share") else
               "link" if links and not images else
               "photo" if images else
               "status" if text else "other")
        post = {
            "fb_id": fb_id, "timestamp": ts, "datetime": dt, "type": typ,
            "title": p.get("title") or "", "group": None, "text": text,
            "hashtags": hashtags, "place": None, "links": links, "media": [],
        }
        return post, (dt[:4] or "undated"), images

    def _serve_allowed(self, abs_path: Path) -> bool:
        for root in self.media_roots:
            try:
                abs_path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    # ── route registration ───────────────────────────────────────────────────
    def register(self, app: FastAPI, prefix: str | None = None) -> None:
        b = self
        if prefix is not None:
            self.prefix = prefix
        from fastapi import APIRouter
        r = APIRouter()

        @r.get("/db")
        def db_list(workspace: str = "default"):
            if not _NAME_OK.match(workspace):
                raise HTTPException(400, "invalid workspace")
            idx = b._ensure_index(workspace)
            out = [{"name": yr, "rows": len(rows)} for yr, rows in sorted(idx["by_year"].items())]
            return {"workspace": workspace, "databases": out}

        @r.get("/db/{name}")
        def db_get(name: str, workspace: str = "default"):
            return b._read_db(workspace, name)

        @r.get("/meta")
        def meta(workspace: str = "default"):
            idx = b._ensure_index(workspace)
            types: dict[str, int] = {}
            for row in idx["rows"]:
                t = str(row["props"].get("type", ""))
                types[t] = types.get(t, 0) + 1
            return {"total": len(idx["rows"]), "by_type": types,
                    "years": sorted(idx["by_year"].keys(), reverse=True),
                    "profile_url": b.profile_url}

        @r.get("/search")
        def search(q: str, limit: int = 300, semantic: bool = True, workspace: str = "default"):
            idx = b._ensure_index(workspace)
            toks = [t for t in q.lower().split() if t]
            kw = [r for r in idx["rows"] if all(t in r["_blob"] for t in toks)] if toks else []
            kw.sort(key=lambda r: str(r["props"].get("date", "")), reverse=True)
            kw_ids = {r["id"] for r in kw}
            related = []
            if semantic and q.strip():
                related = [b._public(r) for r in b._semantic(q, workspace, k=40, exclude=kw_ids)]
            return {"q": q, "total": len(kw), "rows": [b._public(r) for r in kw[:limit]],
                    "related": related}

        @r.post("/ask")
        def ask(payload: dict, workspace: str = "default"):
            q = (payload.get("question") or "").strip()
            if not q:
                raise HTTPException(400, "question required")
            return b._ask(q, workspace)

        @r.post("/reindex")
        def reindex(request: Request, workspace: str = "default"):
            b._require_write(request)
            b._build_index(workspace)
            return {"ok": True, "rows": len(b._indexes[workspace]["rows"])}

        @r.post("/db/{name}/set")
        def db_set(name: str, request: Request, payload: dict = Body(...)):
            b._require_write(request)
            oid = (payload.get("id") or "").strip()
            key = (payload.get("key") or "").strip()
            if not oid or not key:
                raise HTTPException(400, "id and key required")
            b._set_prop(oid, key, payload.get("value"))
            b._indexes.clear()  # reflect the edit on next browse/search (any workspace)
            return {"ok": True}

        @r.post("/db/{name}/new")
        def db_new(name: str, request: Request, payload: dict = Body(...), workspace: str = "default"):
            b._require_write(request)
            title = (payload.get("title") or "Untitled").strip()
            props = payload.get("props")
            props = props if isinstance(props, dict) else {}
            root = b._db_root(workspace, name)
            root.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "untitled"
            p = root / f"{slug}.md"
            i = 2
            while p.exists():
                p = root / f"{slug}-{i}.md"
                i += 1
            p.write_text(_join_frontmatter(props, f"# {title}\n"), encoding="utf-8")
            return {"id": b._page_id(workspace, p), "title": title}

        @r.post("/db/{name}/export-base")
        def db_export_base(name: str, request: Request, workspace: str = "default"):
            b._require_write(request)
            if not _NAME_OK.match(name):
                raise HTTPException(400, "invalid database")
            return {"path": str(b._emit_base(workspace, name))}

        @r.get("/objects/{object_id:path}")
        def get_object(object_id: str):
            workspace, relpath = b._parse_object_id(object_id)
            full = b._validate_path(workspace, relpath)
            if not full.is_file():
                raise HTTPException(404, f"page not found: {workspace}/{relpath}")
            st = full.stat()
            content = full.read_text(encoding="utf-8", errors="replace")
            rel_norm = relpath if relpath.endswith(".md") else relpath + ".md"
            title, _ = b._read_h1_and_summary(full)
            return {
                "id": f"{workspace}/{rel_norm[:-3]}",
                "workspace": workspace,
                "path": rel_norm,
                "content": content,
                "content_markdown": b._rewrite_images(content, full.parent),
                "title": title,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "links": b._extract_links(content),
            }

        @r.post("/objects/{object_id:path}")
        def put_object(object_id: str, request: Request, body: dict):
            b._require_write(request)
            workspace, relpath = b._parse_object_id(object_id)
            full = b._validate_path(workspace, relpath)
            content = (body or {}).get("content")
            if not isinstance(content, str):
                raise HTTPException(400, "body.content (string) is required")
            full.parent.mkdir(parents=True, exist_ok=True)
            if full.exists():
                hist = full.parent / ".history" / full.stem
                hist.mkdir(parents=True, exist_ok=True)
                (hist / f"{int(time.time())}.md").write_text(
                    full.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            full.write_text(content, encoding="utf-8")
            b._index = None  # reflect the edit on next browse/search
            return {"ok": True, "id": f"{workspace}/{relpath[:-3] if relpath.endswith('.md') else relpath}",
                    "size": full.stat().st_size, "mtime": full.stat().st_mtime,
                    "links": b._extract_links(content)}

        @r.post("/append")
        def append_page(request: Request, body: dict):
            b._require_write(request)
            object_id = (body or {}).get("object_id", "")
            text = (body or {}).get("text", "")
            sep = (body or {}).get("separator", "\n\n")
            if not object_id or not isinstance(text, str):
                raise HTTPException(400, "object_id + text required")
            workspace, relpath = b._parse_object_id(object_id)
            full = b._validate_path(workspace, relpath)
            full.parent.mkdir(parents=True, exist_ok=True)
            existing = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
            full.write_text((existing + sep + text) if existing else text, encoding="utf-8")
            return {"ok": True, "size": full.stat().st_size}

        @r.get("/files")
        def serve_file(path: str, w: int | None = None):
            try:
                abs_path = Path(path).expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                raise HTTPException(400, "bad path")
            if not b._serve_allowed(abs_path):
                raise HTTPException(403, "path not allowlisted")
            if abs_path.suffix.lower() not in (_IMG_EXTS | _VID_EXTS):
                raise HTTPException(403, "unsupported extension")
            if not abs_path.is_file():
                raise HTTPException(404, "not found")
            if w is not None and _THUMB_MIN_W <= w <= _THUMB_MAX_W and abs_path.suffix.lower() in _IMG_EXTS:
                thumb = b._thumb(abs_path, w)
                if thumb is not None:
                    return FileResponse(thumb, media_type="image/webp")
            return FileResponse(abs_path)

        @r.get("/unfurl")
        def unfurl(url: str):
            return b._unfurl(url)

        @r.get("/cutoff")
        def cutoff():
            """Newest EXPORT post timestamp (ms). The scanner stops scrolling
            once it scrolls past this — i.e. into posts the export already has —
            so a first scan only imports the gap between the export and now."""
            return {"max_ts": b._export_max_ts()}

        @r.get("/embed-status")
        def embed_status():
            return b._embed_status()

        @r.post("/reembed")
        def reembed(request: Request):
            b._require_write(request)
            return b._start_reembed()

        def _import_one(payload: dict) -> dict:
            """One scraped post → a markdown row in spaces-data (same format as
            the export), so live posts appear alongside the export in the FB
            Browser. Images are downloaded for a permanent backup; reshared
            videos/posts display via the original-URL FB embed. `existed` lets
            the scanner stop once it scrolls into already-imported territory."""
            from .spaces_writer import post_to_row
            post, year, images = b._prep_live(payload)
            media_dir = b.root / "_live-media" / post["fb_id"]
            for i, u in enumerate(images):
                ap = b._download_media(u, media_dir, i)
                if ap:
                    post["media"].append({"uri": u, "abs_path": str(ap), "kind": "image",
                                          "caption": "", "creation_timestamp": None})
            fn, content = post_to_row(post, source="facebook-live")
            out_dir = b.root / "default" / year
            out_dir.mkdir(parents=True, exist_ok=True)
            existed = (out_dir / fn).exists()
            (out_dir / fn).write_text(content, encoding="utf-8")
            return {"ok": True, "id": f"default/{year}/{fn[:-3]}",
                    "images": len(post["media"]), "existed": existed, "updated": existed}

        @r.post("/import")
        async def fb_import(request: Request):
            """Scan-mode import. Accepts a single post (legacy) OR a batch — a
            JSON array or {"posts":[...]} — and returns per-post results so the
            scanner can batch + detect the already-imported boundary."""
            b._require_write(request)
            body = await request.json()
            batch = body if isinstance(body, list) else (
                body.get("posts") if isinstance(body, dict) and isinstance(body.get("posts"), list) else None)
            items = batch if batch is not None else [body]
            results = [_import_one(p) for p in items]
            b._index = None  # rebuilt lazily on the next browse — keeps it fresh
            try:  # mark semantic search stale until a re-embed
                (b._index_dir / ".last-import").touch()
            except Exception:
                pass
            if batch is not None:
                return {"ok": True, "count": len(results), "results": results}
            return results[0]

        app.include_router(r, prefix=self.prefix)
