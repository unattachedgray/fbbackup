"""Local web server: faithful FB-native timeline + read-only media.

Loads the canonical index once at startup and serves:
  GET /                      → the timeline viewer (static)
  GET /api/posts             → filtered, paginated posts (newest first)
  GET /api/meta              → counts, types, years (for the filter UI)
  GET /media?uri=<uri>       → the media file, ONLY if present in the manifest
                               (the manifest allowlist prevents path traversal)

The export is never modified — media is streamed in place from its resolved
absolute path. The import endpoint (Phase 4) will mount onto this same app.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

_PKG = Path(__file__).resolve().parent
_VIEWER = _PKG.parent / "viewer"


def load_index(out_dir: Path) -> tuple[list[dict], dict[str, str]]:
    out_dir = Path(out_dir).expanduser()
    posts_path = out_dir / "posts.jsonl"
    if not posts_path.is_file():
        raise SystemExit(f"No index at {posts_path} — run `fbbackup parse` first.")
    posts = [json.loads(line) for line in posts_path.open(encoding="utf-8")]
    manifest = json.loads((out_dir / "media-manifest.json").read_text(encoding="utf-8"))
    return posts, manifest


def create_app(out_dir: Path) -> FastAPI:
    posts, manifest = load_index(out_dir)
    years = sorted({p["datetime"][:4] for p in posts if p["datetime"]}, reverse=True)
    types: dict[str, int] = {}
    for p in posts:
        types[p["type"]] = types.get(p["type"], 0) + 1

    app = FastAPI(title="fbbackup")

    @app.get("/api/meta")
    def meta():
        return {"total": len(posts), "by_type": types, "years": years}

    @app.get("/api/posts")
    def api_posts(
        offset: int = 0,
        limit: int = Query(30, le=200),
        type: str | None = None,
        year: str | None = None,
        has_media: bool | None = None,
        q: str | None = None,
    ):
        ql = q.lower().strip() if q else None
        out, scanned = [], 0
        for p in posts[offset:]:
            scanned += 1
            if type and p["type"] != type:
                continue
            if year and p["datetime"][:4] != year:
                continue
            if has_media is not None and bool(p["media"]) != has_media:
                continue
            if ql and ql not in (p["text"] or "").lower() \
                    and ql not in (p["title"] or "").lower() \
                    and not any(ql in m["caption"].lower() for m in p["media"]):
                continue
            out.append(p)
            if len(out) >= limit:
                break
        return {"posts": out, "next_offset": offset + scanned, "done": offset + scanned >= len(posts)}

    @app.get("/media")
    def media(uri: str):
        abs_path = manifest.get(uri)
        if not abs_path:
            raise HTTPException(404, "unknown media")
        f = Path(abs_path)
        if not f.is_file():
            raise HTTPException(404, "media file missing")
        return FileResponse(f)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (_VIEWER / "index.html").read_text(encoding="utf-8")

    app.mount("/static", StaticFiles(directory=_VIEWER), name="static")
    return app


def serve(out_dir: Path, host: str = "127.0.0.1", port: int = 8730):
    import uvicorn
    uvicorn.run(create_app(out_dir), host=host, port=port)
