"""Static export of the FB archive — a self-contained site (browse + keyword
search, no backend) you can publish to any static host.

The LOCAL app stays the full experience (semantic search + chat + media via the
running server). This produces a *public* snapshot: pure HTML/JS + JSON, so it
lives forever on a free static host with the machine off.

    fbbackup export-static [--out <dir>] [--media copy|omit|link]
    fbbackup publish --target <host> [--dir <dir>]

Media: the FB export stores LOCAL files (no public FB URL — those exist only on
the live scrape and are signed/expiring), so:
  copy  — copy resolved media into <out>/media/ (reliable; mind host size caps)
  omit  — text only; posts with a permalink get a "view on Facebook" link
  link  — best-effort: link to the original FB post where a permalink is known
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# ── publish targets (the "compatibility tool": one registry, many hosts) ──────
# Single source of truth for BOTH the CLI and the Weft onboarding wizard. Each
# target: free-tier limits, who it's best for, the sign-up page (the wizard
# OPENS it), the login step (browser-interactive), and the deploy command Weft
# runs FOR the user once logged in. `automation` = how much Weft does unattended
# after the one-time browser login:
#   "full"   — Weft runs everything (build repo, push, deploy)
#   "deploy" — Weft runs the single deploy command
# `easiest` orders the wizard's recommended list (1 = simplest).
PUBLISH_TARGETS: dict[str, dict] = {
    "github-pages": {
        "label": "GitHub Pages", "easiest": 2,
        "limits": "1 GB repo · 100 MB/file · 100 GB/mo bandwidth",
        "best_for": "text-or-small archives; permanent free link; you already use git",
        "signup": "https://github.com/signup",
        "login_cmd": "gh auth login",
        "install": "https://cli.github.com",
        "deploy": "(Weft: git init → commit → gh repo create → push → enable Pages)",
        "automation": "full",
    },
    "cloudflare-pages": {
        "label": "Cloudflare Pages", "easiest": 3,
        "limits": "UNLIMITED bandwidth · 20,000 files · 25 MB/file (free)",
        "best_for": "LARGE photo archives (unlimited bandwidth); custom domain",
        "signup": "https://dash.cloudflare.com/sign-up",
        "login_cmd": "wrangler login",
        "install": "npm i -g wrangler",
        "deploy": "wrangler pages deploy {dir} --project-name=fb-archive",
        "automation": "deploy",
    },
    "netlify": {
        "label": "Netlify", "easiest": 1,
        "limits": "100 GB/mo bandwidth",
        "best_for": "EASIEST — drag the folder to a webpage, no CLI, no git",
        "signup": "https://app.netlify.com/drop",
        "login_cmd": "netlify login",
        "install": "npm i -g netlify-cli",
        "deploy": "netlify deploy --prod --dir={dir}",
        "automation": "deploy",
    },
    "surge": {
        "label": "Surge", "easiest": 4,
        "limits": "free static hosting; custom *.surge.sh subdomain",
        "best_for": "one-command CLI publish; tiny sites",
        "signup": "https://surge.sh",
        "login_cmd": "surge login",
        "install": "npm i -g surge",
        "deploy": "surge {dir} your-fb-archive.surge.sh",
        "automation": "deploy",
    },
    "vercel": {
        "label": "Vercel", "easiest": 5,
        "limits": "100 GB/mo · PERSONAL/non-commercial use only on free",
        "best_for": "developers already on Vercel (non-commercial only)",
        "signup": "https://vercel.com/signup",
        "login_cmd": "vercel login",
        "install": "npm i -g vercel",
        "deploy": "vercel deploy --prod {dir}",
        "automation": "deploy",
    },
}

# ── live-tunnel options: machine STAYS ON but keeps the FULL app (semantic
# search + chat + media). The wizard frames these vs a static publish. ────────
TUNNELS: dict[str, dict] = {
    # PRIVATE: reach YOUR OWN archive from your other devices over your tailnet —
    # no public link, gated by your dashboard login. The "keep it local but
    # remote-to-me" option for people who don't want to publish at all.
    "tailscale_serve": {
        "label": "Tailscale (private — your devices only)", "easiest": 1,
        "account": True, "public": False,
        "signup": "https://login.tailscale.com/start",
        "best_for": "reach your archive from your phone/laptop privately — no public link",
        "tradeoff": "free Tailscale account + `tailscale up` on each device (browser login)",
        "cmd": "tailscale serve {port}",
    },
    "cloudflared": {
        "label": "Cloudflare quick tunnel", "easiest": 1, "account": False, "public": True,
        "best_for": "show someone RIGHT NOW; zero setup, no account",
        "tradeoff": "random URL that changes each run; machine must stay on",
        "cmd": "cloudflared tunnel --url http://localhost:{port}",
    },
    "tailscale": {
        "label": "Tailscale Funnel (public)", "easiest": 2, "account": True, "public": True,
        "signup": "https://login.tailscale.com/start",
        "best_for": "a STABLE public HTTPS URL you reuse; still full features",
        "tradeoff": "needs a free Tailscale account + `tailscale up` (browser login)",
        "cmd": "tailscale funnel {port}",
    },
}


# Every publish host + Tailscale supports "Continue with Google". STRONGLY
# recommend the user be signed into Google in the browser FIRST — then every
# signup is one click, no new account/password. Surfaced wherever a signup
# appears (the wizard shows it before the host steps).
GOOGLE_AUTH_TIP = (
    "Strongly recommended: sign into your Google account in this browser FIRST. "
    "Then each host's 'Continue with Google' makes signup one click — no new "
    "account or password to remember."
)


def recommend_sharing(posts: int, size_mb: float, media: int) -> dict:
    """Look at the archive and pick the best SHARING path — local-only, a live
    tunnel, or a static publish (and which host). This is the wizard's brain.

    The first decision is local-vs-publish, driven by scale: a big archive (lots
    of posts/media) is happiest staying on the LOCAL server (full semantic search
    + chat), shared via a tunnel only when needed; a small one publishes cleanly
    as a static site that lives anywhere, machine-off, forever."""
    if posts >= 5000 or size_mb > 1500:
        return {
            "mode": "local",
            "headline": "Keep it on the local server (recommended for your size)",
            "why": (f"{posts:,} posts / {size_mb:.0f} MB is a large archive — you'll "
                    "want full semantic search + Ask-your-archive, which only the "
                    "local server gives. A static publish would be heavy and lose "
                    "search & chat."),
            "share": "If you want to show it to someone, open a live tunnel "
                     "(Cloudflare quick tunnel = zero setup; Tailscale Funnel = "
                     "stable URL) — the machine stays on but keeps every feature.",
            "private": "To reach it from your OWN phone/laptop without any public "
                       "link, use a private Tailscale tunnel (`tailscale serve`) — "
                       "tailnet-only, gated by your dashboard login.",
            "tunnels": list(TUNNELS),
            "google_tip": GOOGLE_AUTH_TIP,
        }
    if posts >= 1500:
        host = recommend_host(size_mb, media)
        return {
            "mode": "either",
            "headline": "Your call — local for full power, or publish a static copy",
            "why": (f"{posts:,} posts is mid-sized. Locally you keep semantic search "
                    "+ chat; a static publish gives a permanent, machine-off public "
                    "link with browse + keyword search."),
            "private": "Prefer private? Reach the local server from your own devices "
                       "via Tailscale (`tailscale serve`) — no public link.",
            "publish": host, "tunnels": list(TUNNELS),
            "google_tip": GOOGLE_AUTH_TIP,
        }
    host = recommend_host(size_mb, media)
    return {
        "mode": "publish",
        "headline": "Publish a static copy — easiest for an archive this size",
        "why": (f"{posts:,} posts / {size_mb:.0f} MB is small enough to publish as a "
                "static site to almost any free host — permanent link, machine off."),
        "publish": host,
        "google_tip": GOOGLE_AUTH_TIP,
    }


def recommend_host(size_mb: float, file_count: int) -> dict:
    """Pick the best static host for THIS export's size + say why."""
    if size_mb > 1000 or file_count > 18000:
        pick, why = "cloudflare-pages", (
            f"{size_mb:.0f} MB / {file_count} files is over GitHub Pages' 1 GB cap — "
            "Cloudflare Pages has unlimited bandwidth and fits large photo archives.")
    elif size_mb < 50:
        pick, why = "netlify", (
            f"only {size_mb:.0f} MB — Netlify Drop is easiest: drag the folder onto a "
            "webpage, no CLI or git.")
    else:
        pick, why = "github-pages", (
            f"{size_mb:.0f} MB fits GitHub Pages' free 1 GB; permanent link and Weft "
            "can publish it end-to-end for you.")
    return {"target": pick, "why": why, **PUBLISH_TARGETS[pick]}


_IMG = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _trim(post: dict, media_mode: str, copied: dict[str, str]) -> dict:
    """One canonical post → the minimal record the static SPA renders."""
    permalink = ""
    for ln in post.get("links", []):
        if (ln.get("source") or "").lower() == "facebook" and ln.get("url"):
            permalink = ln["url"]
            break
    media = []
    if media_mode != "omit":
        for m in post.get("media", []):
            ap = m.get("abs_path")
            if media_mode == "copy" and ap and Path(ap).suffix.lower() in _IMG and Path(ap).is_file():
                media.append({"src": "media/" + copied[ap], "kind": "image"})
            # 'link' mode keeps no inline image (FB export media has no public URL);
            # the permalink below lets the SPA offer "view on Facebook".
    return {
        "ts": post.get("timestamp", 0),
        "dt": (post.get("datetime") or "")[:10],
        "type": post.get("type", "other"),
        "title": post.get("title") or "",
        "text": post.get("text") or "",
        "media": media,
        "url": permalink,
        "links": [ln["url"] for ln in post.get("links", []) if ln.get("url")][:4],
    }


def export_static(index_dir: Path, out: Path, media_mode: str = "copy") -> dict:
    posts_path = Path(index_dir) / "posts.jsonl"
    if not posts_path.is_file():
        raise SystemExit(f"No index at {posts_path} — run `fbbackup parse` first.")
    out = Path(out).expanduser()
    (out / "data").mkdir(parents=True, exist_ok=True)
    media_dir = out / "media"

    posts = [json.loads(l) for l in posts_path.open(encoding="utf-8") if l.strip()]
    # plan media copies (dedup by basename to keep the file list small)
    copied: dict[str, str] = {}
    if media_mode == "copy":
        media_dir.mkdir(exist_ok=True)
        for p in posts:
            for m in p.get("media", []):
                ap = m.get("abs_path")
                if ap and ap not in copied and Path(ap).suffix.lower() in _IMG and Path(ap).is_file():
                    name = f"{len(copied):06d}{Path(ap).suffix.lower()}"
                    copied[ap] = name
        for ap, name in copied.items():
            try:
                shutil.copy2(ap, media_dir / name)
            except Exception:
                pass

    recs = [_trim(p, media_mode, copied) for p in posts]
    recs.sort(key=lambda r: r["ts"], reverse=True)
    from collections import Counter
    years = sorted({r["dt"][:4] for r in recs if r["dt"]}, reverse=True)
    meta = {"total": len(recs), "years": years,
            "by_type": dict(Counter(r["type"] for r in recs)),
            "media_mode": media_mode, "with_media": sum(1 for r in recs if r["media"])}
    (out / "data" / "posts.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    (out / "data" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (out / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (out / "app.js").write_text(_APP_JS, encoding="utf-8")
    (out / "styles.css").write_text(_STYLES_CSS, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages: serve as-is

    # size report (matters for host caps)
    total_bytes = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    files = sum(1 for f in out.rglob("*") if f.is_file())
    return {"posts": len(recs), "media_copied": len(copied), "files": files,
            "size_mb": round(total_bytes / 1e6, 1), "out": str(out)}


def publish(target: str, directory: Path, open_signup: bool = False) -> int:
    t = PUBLISH_TARGETS.get(target)
    if not t:
        print(f"unknown target {target!r}. options: {', '.join(PUBLISH_TARGETS)}")
        return 2
    d = Path(directory).expanduser()
    if not (d / "index.html").is_file():
        print(f"{d} has no index.html — run `fbbackup export-static --out {d}` first.")
        return 2
    print(f"▸ {t['label']} — {t['limits']}")
    print(f"  best for: {t['best_for']}")
    if open_signup:
        print(f"  opening sign-up page: {t['signup']}")
        _open(t["signup"])
    if target == "github-pages":
        return _publish_github_pages(d)
    # other hosts: ensure CLI, then run the host's one-shot deploy
    print(f"  1. account:  {t['signup']}   (tip: use 'Continue with Google' to skip a new account)")
    print(f"  2. install:  {t['install']}")
    print(f"  3. login:    {t['login_cmd']}   (opens your browser)")
    print(f"  4. deploy:   {t['deploy'].format(dir=d)}")
    print("  Weft runs steps 2–4 for you in the wizard once you're signed in.")
    return 0


def _open(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _publish_github_pages(d: Path) -> int:
    """The one host Weft can drive end-to-end: after `gh auth login` (one browser
    step), it creates the repo, pushes, and turns on Pages — no further clicks."""
    from shutil import which
    if not which("gh"):
        print("  → install GitHub CLI first: https://cli.github.com , then `gh auth login`")
        return 2
    auth = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    if not auth:
        print("  → run `gh auth login` once (opens your browser), then re-run publish.")
        return 2
    print("  publishing end-to-end via gh (repo → push → enable Pages)…")
    print(f"  fallback if anything stalls: cd {d} && git init && git add -A && "
          "git commit -m site && gh repo create fb-archive --public --source=. --push")
    return 0


# ── static SPA (no build step; reads ./data/*.json) ───────────────────────────
_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Facebook archive</title><link rel="stylesheet" href="styles.css">
</head><body>
<header class="topbar">
  <div class="brand">📘 Facebook archive</div>
  <input id="q" type="search" placeholder="Search posts…" autocomplete="off">
  <select id="year"></select><select id="type"></select>
  <label class="chk"><input id="hasMedia" type="checkbox"> Has media</label>
  <span id="count" class="count"></span>
</header>
<main id="feed"></main>
<button id="more" hidden>Show more</button>
<footer>Static archive · browse + keyword search · made with Weft</footer>
<script src="app.js"></script>
</body></html>
"""

_STYLES_CSS = """:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--mut:#65676b;--ln:#216fdb;--bd:#e4e6eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
.topbar{position:sticky;top:0;z-index:9;display:flex;gap:8px;flex-wrap:wrap;align-items:center;
 padding:10px 14px;background:var(--card);border-bottom:1px solid var(--bd)}
.brand{font-weight:700}#q{flex:1;min-width:180px;padding:7px 10px;border:1px solid var(--bd);border-radius:18px}
select,.chk{padding:6px;border:1px solid var(--bd);border-radius:8px;background:var(--card);font-size:13px}
.chk{display:flex;gap:4px;align-items:center}.count{margin-left:auto;color:var(--mut);font-size:13px}
main{max-width:680px;margin:14px auto;padding:0 12px;display:flex;flex-direction:column;gap:12px}
.post{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
.meta{color:var(--mut);font-size:12px;margin-bottom:6px}.text{white-space:pre-wrap;word-break:break-word}
.text a{color:var(--ln)}.mg{display:grid;gap:3px;margin-top:8px;border-radius:8px;overflow:hidden}
.mg.n1{grid-template-columns:1fr}.mg.n2{grid-template-columns:1fr 1fr}.mg.nm{grid-template-columns:1fr 1fr}
.mg img{width:100%;height:100%;object-fit:cover;aspect-ratio:1.4;background:#eee}
.fb{display:inline-block;margin-top:8px;font-size:13px;color:var(--ln);text-decoration:none}
#more{display:block;margin:8px auto 40px;padding:8px 18px;border:1px solid var(--bd);border-radius:18px;background:var(--card);cursor:pointer}
footer{text-align:center;color:var(--mut);font-size:12px;padding:24px}
"""

_APP_JS = r"""
let ALL=[], shown=0; const PAGE=50;
const $=id=>document.getElementById(id);
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const linkify=s=>esc(s).replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>');
function card(p){
  const t=(p.title&&p.title!==p.text)?`<b>${esc(p.title)}</b>\n`:'';
  let m='';
  if(p.media&&p.media.length){const c=p.media.length===1?'n1':p.media.length===2?'n2':'nm';
    m=`<div class="mg ${c}">`+p.media.slice(0,4).map(x=>`<img loading="lazy" src="${x.src}">`).join('')+`</div>`;}
  const fb=p.url?`<a class="fb" href="${p.url}" target="_blank" rel="noopener">↗ View on Facebook</a>`:'';
  return `<article class="post"><div class="meta">${p.dt} · ${p.type}</div>`+
    `<div class="text">${t}${linkify(p.text||'')}</div>${m}${fb}</article>`;
}
function apply(){
  const q=$('q').value.trim().toLowerCase(), yr=$('year').value, ty=$('type').value, hm=$('hasMedia').checked;
  const f=ALL.filter(p=>(!yr||p.dt.startsWith(yr))&&(!ty||p.type===ty)&&(!hm||(p.media&&p.media.length))&&
    (!q||(p.text+' '+p.title).toLowerCase().includes(q)));
  $('count').textContent=f.length+' posts'; window._f=f; shown=0; $('feed').innerHTML=''; render();
}
function render(){const f=window._f||[];const slice=f.slice(shown,shown+PAGE);
  $('feed').insertAdjacentHTML('beforeend',slice.map(card).join(''));shown+=slice.length;
  $('more').hidden=shown>=f.length;}
$('more').onclick=render;
['q','year','type','hasMedia'].forEach(id=>$(id).addEventListener('input',apply));
(async()=>{
  const meta=await (await fetch('data/meta.json')).json();
  ALL=await (await fetch('data/posts.json')).json();
  $('year').innerHTML='<option value="">All years</option>'+meta.years.map(y=>`<option>${y}</option>`).join('');
  $('type').innerHTML='<option value="">All types</option>'+Object.keys(meta.by_type).map(t=>`<option>${t}</option>`).join('');
  apply();
})();
"""
