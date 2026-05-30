"""Parse a Facebook post export into one canonical record per post.

Read-only: this never writes to the export. It produces, under the output
dir, ``posts.jsonl`` (one canonical post per line, newest first) and
``media-manifest.json`` (resolution status for every referenced media uri).

A canonical record:

    {
      "fb_id":      str,     # stable dedup key (media id, else hash)
      "timestamp":  int,     # unix seconds
      "datetime":   str,     # ISO 8601, local-naive
      "type":       str,     # photo|video|link|checkin|share|status|other
      "title":      str,     # FB's action line ("… added a new photo.")
      "group":      str|None,# group name if "… to the group: X"
      "text":       str,     # post body (mojibake-fixed)
      "hashtags":   [str],
      "place":      {name,address,latitude,longitude}|None,
      "links":      [{url,name,source}],
      "media":      [{uri,abs_path,kind,caption,creation_timestamp}],
    }
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .mojibake import fix

POSTS_GLOB = "your_posts__check_ins__photos_and_videos_*.json"
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".gif"}
_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_GROUP_RE = re.compile(r"to the group:\s*(.+?)\.?\s*$")

# Facebook-generated framing lines inside "memory" reshares' text attachments
# ("2 Years Ago", a date, "X added 3 new photos") — skip these so we keep only
# the original post's real text.
_FRAMING = re.compile(
    r"^(?:"
    r"\d+\s+(?:year|month|week|day|hour|minute)s?\s+ago"
    r"|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\b.*"
    r"|.*\b(?:updated\s+(?:his|her|their)\s+(?:status|profile|cover)"
    r"|added\s+\d*\s*(?:a\s+)?(?:new\s+)?(?:photo|video)s?"
    r"|shared\s+(?:a|an|his|her|their|the)"
    r"|changed\s+(?:his|her|their)"
    r"|is\s+(?:feeling|with|at|now|listening|watching|reading)"
    r"|wrote\s+on|created\s+(?:a|an)|was\s+(?:tagged|with)|likes\s+a)\b.*"
    r")$", re.I)


def _att_text(entry: dict) -> str | None:
    """A `text` attachment entry's real content, or None if it's framing."""
    if "text" not in entry:
        return None
    t = (fix(entry["text"]) or "").strip()
    return t if t and len(t) >= 15 and not _FRAMING.match(t) else None


def _media_index(export_root: Path) -> tuple[list[Path], dict[str, Path]]:
    """Return (export subfolders, basename→path fallback index).

    Most uris resolve by joining the uri onto an export subfolder; the
    basename index is a fallback for media reorganized into a second export.
    """
    subfolders = [p for p in export_root.iterdir() if p.is_dir()]
    by_name: dict[str, Path] = {}
    for sub in subfolders:
        media_root = sub / "your_facebook_activity" / "posts" / "media"
        if not media_root.is_dir():
            continue
        for f in media_root.rglob("*"):
            if f.is_file():
                by_name.setdefault(f.name, f)
    return subfolders, by_name


def _resolve(uri: str, subfolders: list[Path], by_name: dict[str, Path]) -> Path | None:
    for sub in subfolders:
        cand = sub / uri
        if cand.is_file():
            return cand
    return by_name.get(Path(uri).name)


def _iter_attachment_data(post: dict):
    for att in post.get("attachments", []) or []:
        for entry in att.get("data", []) or []:
            yield entry


def _post_text(post: dict) -> str:
    parts = [fix(d["post"]) for d in post.get("data", []) or [] if d.get("post")]
    return "\n".join(p for p in parts if p)


def _classify(media: list, links: list, place, title: str, text: str) -> str:
    if media:
        return "video" if any(m["kind"] == "video" for m in media) else "photo"
    if links:
        return "link"
    if place:
        return "checkin"
    if "shared a memory" in title or "shared a post" in title:
        return "share"
    if text:
        return "status"
    return "other"


def _share_links(posts_dir: Path) -> dict[int, str]:
    """Map post timestamp → the Facebook permalink of the content shared.

    Facebook strips reshared content from your_posts (external_context is empty),
    but ``content_sharing_links_you_have_created.json`` records the actual URLs
    you shared, by timestamp. We attach these (exact-timestamp match only) so
    those reshares at least show a clickable link back to the original.
    """
    f = posts_dir / "content_sharing_links_you_have_created.json"
    out: dict[int, str] = {}
    if not f.is_file():
        return out
    try:
        for e in json.load(open(f, encoding="utf-8")):
            ts = e.get("timestamp")
            urls = [lv.get("value") for lv in e.get("label_values", [])
                    if lv.get("label") == "URL" and lv.get("value")]
            if ts and urls:
                out.setdefault(int(ts), urls[0])
    except Exception:
        pass
    return out


def normalize_post(post: dict, subfolders, by_name, share_links: dict | None = None) -> dict:
    title = fix(post.get("title", "")) or ""
    text = _post_text(post)

    media, links, place = [], [], None
    for entry in _iter_attachment_data(post):
        if "media" in entry:
            m = entry["media"]
            uri = m.get("uri", "")
            abs_path = _resolve(uri, subfolders, by_name)
            ext = Path(uri).suffix.lower()
            media.append({
                "uri": uri,
                "abs_path": str(abs_path) if abs_path else None,
                "kind": "video" if ext in _VIDEO_EXTS else "image",
                "caption": fix(m.get("description") or m.get("title")) or "",
                "creation_timestamp": m.get("creation_timestamp"),
            })
        elif "external_context" in entry:
            ec = entry["external_context"]
            # Pure reshares export an EMPTY external_context ({"url": ""}) — no
            # link, no original content. Skip those so they don't masquerade as
            # link posts; they fall through to "share" (only the live extension
            # can recover the reshared content).
            url = ec.get("url", "")
            if url:
                links.append({
                    "url": url,
                    "name": fix(ec.get("name")) or "",
                    "source": fix(ec.get("source")) or "",
                })
        elif "place" in entry and place is None:
            pl = entry["place"]
            coord = pl.get("coordinate", {}) or {}
            place = {
                "name": fix(pl.get("name")) or "",
                "address": fix(pl.get("address")) or "",
                "latitude": coord.get("latitude"),
                "longitude": coord.get("longitude"),
            }

    # Recover content the main attachment path skips: "memory" reshares carry
    # the original post's text (mixed with FB framing, stripped by _att_text);
    # group/page/link shares carry a `name`.
    att_names = [n for e in _iter_attachment_data(post) if (n := (fix(e["name"]) if e.get("name") else None))]
    if not text:
        att_texts = [t for e in _iter_attachment_data(post) if (t := _att_text(e))]
        text = "\n\n".join(att_texts) if att_texts else (att_names[0] if att_names else "")
    for l in links:  # use a name as the link's title when it lacks one
        if not l["name"] and att_names:
            l["name"] = att_names[0]

    ts = int(post.get("timestamp", 0))
    # Recover the reshared link from content_sharing_links (exact-ts match) when
    # your_posts has none — gives empty reshares a clickable Facebook permalink.
    if not links and share_links and ts in share_links:
        links.append({"url": share_links[ts], "name": "", "source": "Facebook"})
    hashtag_src = " ".join([text] + [m["caption"] for m in media])
    hashtags = sorted(set(_HASHTAG_RE.findall(hashtag_src)))
    group_m = _GROUP_RE.search(title)

    # Stable per-post content identity. A content hash, NOT the media id —
    # the same photo recurs across its original post, later "memory" reshares,
    # and profile/cover reuse, so media id collapses *distinct* posts. This
    # signature collapses only genuine export duplicates (FB repeats entries):
    # same time + title + text + media + links.
    sig = "|".join([
        str(ts), title, text,
        ",".join(m["uri"] for m in media),
        ",".join(l["url"] for l in links),
    ])
    fb_id = hashlib.sha1(sig.encode()).hexdigest()[:16]

    return {
        "fb_id": fb_id,
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts).isoformat() if ts else "",
        "type": _classify(media, links, place, title, text),
        "title": title,
        "group": group_m.group(1).strip() if group_m else None,
        "text": text,
        "hashtags": hashtags,
        "place": place,
        "links": links,
        "media": media,
    }


def parse(export_root: Path, out_dir: Path) -> dict:
    """Parse the export; write posts.jsonl + media-manifest.json. Returns stats."""
    export_root = Path(export_root).expanduser()
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    subfolders, by_name = _media_index(export_root)

    posts_files = sorted(
        f for sub in subfolders
        for f in (sub / "your_facebook_activity" / "posts").glob(POSTS_GLOB)
    )
    if not posts_files:
        raise SystemExit(f"No posts files ({POSTS_GLOB}) found under {export_root}")

    share_links: dict[int, str] = {}
    for pf in posts_files:
        share_links.update(_share_links(pf.parent))

    records: list[dict] = []
    for pf in posts_files:
        with open(pf, encoding="utf-8") as fh:
            for raw in json.load(fh):
                records.append(normalize_post(raw, subfolders, by_name, share_links))

    records.sort(key=lambda r: r["timestamp"], reverse=True)

    posts_path = out_dir / "posts.jsonl"
    with open(posts_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Media manifest: resolution status for every referenced uri.
    manifest, unresolved = {}, 0
    for r in records:
        for m in r["media"]:
            manifest[m["uri"]] = m["abs_path"]
            if m["abs_path"] is None:
                unresolved += 1
    (out_dir / "media-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8"
    )

    types: dict[str, int] = {}
    for r in records:
        types[r["type"]] = types.get(r["type"], 0) + 1
    dated = [r for r in records if r["timestamp"]]
    stats = {
        "posts": len(records),
        "posts_files": [str(p) for p in posts_files],
        "by_type": types,
        "media_refs": len(manifest),
        "media_unresolved": unresolved,
        "date_range": [dated[-1]["datetime"], dated[0]["datetime"]] if dated else None,
        "out": str(posts_path),
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/mnt/d/dev/facebook-data")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("index")
    s = parse(root, out)
    print(json.dumps(s, indent=2, ensure_ascii=False))
