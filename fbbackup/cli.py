"""fbbackup CLI — turn a Facebook export into a browsable, searchable archive.

    fbbackup parse     export JSON  → index/posts.jsonl + media-manifest
    fbbackup build     index        → spaces-data/<year>/*.md  (markdown rows)
    fbbackup embed     spaces-data  → index/embeddings.npy  (semantic search)
    fbbackup index     parse + build + embed  (the whole pipeline, one command)
    fbbackup serve     run the standalone timeline viewer
    fbbackup share     expose the running dashboard via a Cloudflare quick tunnel
    fbbackup status     what's present (export? index? rows? embeddings?)

Paths resolve in this order: CLI flag > FBBACKUP_* env > config.toml > default.
Everything is written under FBBACKUP_HOME (default: the current directory); the
Facebook export is treated as READ-ONLY and never modified.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("FBBACKUP_HOME", Path.cwd())).expanduser()


def _load_config() -> dict:
    """Read FBBACKUP_HOME/config.toml if present (best-effort)."""
    p = _home() / "config.toml"
    if not p.is_file():
        return {}
    try:
        import tomllib
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — missing/old toml shouldn't break the CLI
        print(f"warning: could not read {p}: {e}", file=sys.stderr)
        return {}


def _resolve(flag: str | None, env: str, cfg_val, default) -> str:
    """flag > env var > config value > default."""
    if flag:
        return flag
    if os.environ.get(env):
        return os.environ[env]
    if cfg_val:
        return str(cfg_val)
    return str(default)


def _abs(path: str) -> Path:
    """Relative paths resolve under FBBACKUP_HOME; absolute paths pass through."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else _home() / p


def _paths(args) -> dict[str, Path]:
    cfg = _load_config()
    exp = cfg.get("export", {})
    out = cfg.get("output", {})
    export_root = _abs(_resolve(getattr(args, "export", None), "FBBACKUP_EXPORT_ROOT",
                                exp.get("root"), "/mnt/d/dev/facebook-data"))
    index_dir = _abs(_resolve(getattr(args, "index", None), "FBBACKUP_INDEX",
                              out.get("dir"), "index"))
    spaces_root = _abs(_resolve(getattr(args, "spaces", None), "FBBACKUP_SPACES_ROOT",
                                None, "spaces-data"))
    return {"export": export_root, "index": index_dir, "spaces": spaces_root, "cfg": cfg}


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_parse(args) -> int:
    from fbbackup.parse import parse
    p = _paths(args)
    if not p["export"].is_dir():
        print(f"✗ export folder not found: {p['export']}\n"
              f"  Download it from Facebook (Settings → Your Information → Download "
              f"Your Information → JSON → Posts), unzip it, and point --export at it.",
              file=sys.stderr)
        return 2
    print(f"parsing {p['export']} → {p['index']} …", flush=True)
    res = parse(p["export"], p["index"])
    print(f"✓ parsed {res.get('posts', '?')} posts", flush=True)
    return 0


def cmd_build(args) -> int:
    from fbbackup.materialize import materialize
    p = _paths(args)
    print(f"materializing {p['index']} → {p['spaces']} …", flush=True)
    res = materialize(p["index"], p["spaces"])
    print(f"✓ wrote {res.get('written', '?')} rows "
          f"(new={res.get('new', '?')}, updated={res.get('updated', '?')})", flush=True)
    return 0


def cmd_embed(args) -> int:
    from fbbackup.embed import embed
    p = _paths(args)
    res = embed(p["spaces"], p["index"])
    print(f"✓ embedded {res.get('count', '?')} rows via {res.get('provider', '?')} "
          f"(dim={res.get('dim', '?')})", flush=True)
    return 0


def cmd_index(args) -> int:
    """The whole pipeline: parse → build → embed."""
    for step in (cmd_parse, cmd_build, cmd_embed):
        rc = step(args)
        if rc != 0:
            return rc
    print("✓ index ready — start the dashboard and open the FB Browser.", flush=True)
    return 0


def cmd_serve(args) -> int:
    from fbbackup.server import serve
    p = _paths(args)
    cfg_serve = p["cfg"].get("serve", {})
    host = args.host or os.environ.get("FBBACKUP_HOST") or cfg_serve.get("host", "127.0.0.1")
    port = int(args.port or os.environ.get("FBBACKUP_PORT") or cfg_serve.get("port", 8730))
    print(f"serving the timeline viewer at http://{host}:{port} (Ctrl-C to stop)", flush=True)
    serve(p["index"], host=host, port=port)
    return 0


def cmd_share(args) -> int:
    """Expose a locally-running server publicly via a Cloudflare quick tunnel
    (no account, ephemeral *.trycloudflare.com URL)."""
    if not _which("cloudflared"):
        print("✗ cloudflared not found on PATH.\n"
              "  Install it: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
              file=sys.stderr)
        return 2
    url = f"http://localhost:{args.port}"
    print(f"opening a public tunnel to {url} … (Ctrl-C to stop sharing)", flush=True)
    print("⚠  this publishes whatever the server exposes — use the dashboard's "
          "public read-only Share for a safe public archive.", flush=True)
    return subprocess.call(["cloudflared", "tunnel", "--url", url])


def cmd_status(args) -> int:
    p = _paths(args)
    posts = p["index"] / "posts.jsonl"
    emb = p["index"] / "embeddings.npy"
    n_posts = sum(1 for _ in posts.open(encoding="utf-8")) if posts.is_file() else 0
    n_rows = sum(1 for _ in p["spaces"].rglob("*.md")) if p["spaces"].is_dir() else 0
    print(f"FBBACKUP_HOME : {_home()}")
    print(f"export        : {p['export']}  {'✓' if p['export'].is_dir() else '✗ missing'}")
    print(f"index         : {posts}  {'✓ ' + str(n_posts) + ' posts' if n_posts else '✗ run `fbbackup parse`'}")
    print(f"rows          : {p['spaces']}  {'✓ ' + str(n_rows) + ' md' if n_rows else '✗ run `fbbackup build`'}")
    print(f"embeddings    : {emb}  {'✓' if emb.is_file() else '✗ run `fbbackup embed`'}")
    return 0


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


# ── parser ───────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fbbackup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, *, export=False, index=False, spaces=False):
        if export:
            sp.add_argument("--export", help="Facebook export folder (read-only)")
        if index:
            sp.add_argument("--index", help="index dir (posts.jsonl, embeddings)")
        if spaces:
            sp.add_argument("--spaces", help="spaces-data dir (markdown rows)")

    common(sub.add_parser("parse", help="export → index/posts.jsonl"), export=True, index=True)
    common(sub.add_parser("build", help="index → spaces-data markdown rows"), index=True, spaces=True)
    common(sub.add_parser("embed", help="spaces-data → embeddings.npy"), index=True, spaces=True)
    common(sub.add_parser("index", help="parse + build + embed"), export=True, index=True, spaces=True)

    sv = sub.add_parser("serve", help="run the standalone timeline viewer")
    common(sv, index=True)
    sv.add_argument("--host"); sv.add_argument("--port")

    sh = sub.add_parser("share", help="Cloudflare quick tunnel to a running server")
    sh.add_argument("--port", default="9119", help="local port to expose (default 9119)")

    common(sub.add_parser("status", help="show what's present"), export=True, index=True, spaces=True)
    return ap


_DISPATCH = {
    "parse": cmd_parse, "build": cmd_build, "embed": cmd_embed, "index": cmd_index,
    "serve": cmd_serve, "share": cmd_share, "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.cmd](args)
    except KeyboardInterrupt:
        return 130
    except SystemExit as e:  # parse/materialize raise SystemExit on user errors
        if e.code and not isinstance(e.code, int):
            print(e.code, file=sys.stderr)
            return 1
        return int(e.code or 0)


if __name__ == "__main__":
    sys.exit(main())
