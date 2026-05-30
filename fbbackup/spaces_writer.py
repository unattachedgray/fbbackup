"""The hub: turn one canonical post into one Weft Spaces markdown row.

A Weft "row" is a markdown file with YAML frontmatter (→ typed Space
properties) + a markdown body — see an existing gallery row for the shape.
Both producers (the bulk export parser and the live Firefox importer) funnel
through `post_to_row` so export posts and freshly-scraped posts are identical
on disk and unify in one Space.

Filenames are derived from timestamp + fb_id (not content), so re-running is
idempotent: the same post always lands on the same file and overwrites in
place rather than duplicating.

Images are embedded inline via Weft's `/api/weft/files?path=` serve route
(requires the FB-media allowlist root, see plan). Videos and external links
become markdown links; the FB-native viewer plays videos.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import yaml

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# FB media is served by the fbspaces plugin under /api/fb/files (distinct from
# Weft's own /api/weft/files, whose allowlist doesn't include the export).
_WEFT_FILES = "/api/fb/files?path="


def _slug(post: dict) -> str:
    # fb_id is a per-post content hash, so this is unique per distinct post;
    # genuine duplicates share it (and the timestamp) and collapse to one file.
    date = (post["datetime"][:10] or "undated")
    return f"{date}-{post['timestamp']}-{post['fb_id'][:8]}"


def _title(post: dict) -> str:
    for line in (post["text"] or "").splitlines():
        if line.strip():
            return line.strip()[:80]
    return post["title"] or post["datetime"][:10] or "post"


def _img_url(abs_path: str) -> str:
    return _WEFT_FILES + quote(abs_path)


def _label(s: str | None, fallback: str = "") -> str:
    """Single-line, bracket-free, truncated label — FB stuffs the whole
    multi-line post text into captions/link names, which would otherwise break
    the markdown link/image (multi-line / stray `]`) and stop it rendering."""
    return " ".join((s or "").split()).replace("[", "").replace("]", "")[:80] or fallback


def post_to_row(post: dict, source: str = "facebook-export") -> tuple[str, str]:
    """Return (filename, markdown_content) for a canonical post."""
    images = [m for m in post["media"] if m["kind"] == "image" and m["abs_path"]]
    videos = [m for m in post["media"] if m["kind"] == "video" and m["abs_path"]]

    props: dict = {
        "title": _title(post),
        "date": post["datetime"][:10],
        "type": post["type"],
        "fb_id": post["fb_id"],
        "source": source,
    }
    if post["group"]:
        props["group"] = post["group"]
    if post["place"] and post["place"]["name"]:
        props["place"] = post["place"]["name"]
    if post["hashtags"]:
        props["tags"] = post["hashtags"]
    props["has_media"] = bool(post["media"])
    if post["media"]:
        props["media_count"] = len(post["media"])
    if images:
        props["image_path"] = images[0]["abs_path"]  # gallery thumbnail

    # Body
    lines = [f"# {props['title']}", ""]
    if post["text"]:
        lines += [post["text"], ""]
    for m in images:
        lines.append(f"![{_label(m['caption'])}]({_img_url(m['abs_path'])})")
    for m in videos:
        # Serve the local video through /api/fb/files (range-request capable) so
        # it plays in a <video> tag — file:// can't load in the dashboard browser.
        lines.append(f"[▶ {_label(m['caption'], 'video')}]({_img_url(m['abs_path'])})")
    if images or videos:
        lines.append("")
    for l in post["links"]:
        lines.append(f"🔗 [{_label(l['name']) or l['url']}]({l['url']})")
    if post["place"] and post["place"]["name"]:
        lines.append(f"📍 {post['place']['name']}")

    fm = yaml.safe_dump(props, sort_keys=False, allow_unicode=True).strip("\n")
    body = "\n".join(lines).rstrip("\n") + "\n"
    content = f"---\n{fm}\n---\n\n{body}"
    return _slug(post) + ".md", content
