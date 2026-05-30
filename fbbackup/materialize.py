"""Materialize canonical posts into the spaces-lite data root as markdown rows.

Reads ``index/posts.jsonl`` and writes one ``.md`` row per post into
``<spaces_root>/<workspace>/<db>/`` via the shared `spaces_writer` (the same
hub the live importer uses). Idempotent: filenames are content-independent, so
re-running overwrites in place rather than duplicating. The FB export is never
touched — media is referenced in place by absolute path.
"""
from __future__ import annotations

import json
from pathlib import Path

from .spaces_writer import post_to_row


def _is_empty(post: dict) -> bool:
    return not post["text"] and not post["media"] and not post["links"]


def materialize(
    index_dir: Path,
    spaces_root: Path,
    workspace: str = "default",
    db: str = "posts",
    shard_by_year: bool = True,
    skip_empty: bool = False,
    since: str | None = None,
    until: str | None = None,
    types: set[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Write one markdown row per post under ``<spaces_root>/<workspace>/``.

    With ``shard_by_year`` (default) each post lands in a ``<year>`` database
    folder, so the Spaces sidebar becomes a year list and no single view has to
    render all ~20k rows. Otherwise everything goes into one ``<db>`` folder.
    """
    index_dir = Path(index_dir).expanduser()
    ws_dir = Path(spaces_root).expanduser() / workspace
    ws_dir.mkdir(parents=True, exist_ok=True)

    posts_path = index_dir / "posts.jsonl"
    if not posts_path.is_file():
        raise SystemExit(f"No index at {posts_path} — run `fbbackup parse` first.")

    new = updated = skipped = 0
    written = 0
    with posts_path.open(encoding="utf-8") as fh:
        for line in fh:
            post = json.loads(line)
            day = post["datetime"][:10]
            if skip_empty and _is_empty(post):
                skipped += 1
                continue
            if since and day < since:
                skipped += 1
                continue
            if until and day > until:
                skipped += 1
                continue
            if types and post["type"] not in types:
                skipped += 1
                continue
            bucket = (post["datetime"][:4] or "undated") if shard_by_year else db
            out_dir = ws_dir / bucket
            out_dir.mkdir(parents=True, exist_ok=True)
            filename, content = post_to_row(post)
            target = out_dir / filename
            if target.exists():
                updated += 1
            else:
                new += 1
            target.write_text(content, encoding="utf-8")
            written += 1
            if written % 4000 == 0:
                print(f"  …{written} rows written")
            if limit and written >= limit:
                break

    stats = {"out": str(ws_dir), "written": written, "new": new,
             "updated": updated, "skipped": skipped}
    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    import sys
    idx = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("index")
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("spaces-data")
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else None
    materialize(idx, root, limit=lim)
