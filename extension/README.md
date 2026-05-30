# fbbackup scanner (Firefox + Chrome)

Scans your Facebook posts into your local **fbbackup** archive as you scroll —
filling in the reshares and recent posts the data export can't include. Each
scraped post becomes a markdown row alongside the export data, so it shows up in
the FB Browser seamlessly.

No scheduling, no cron: **scroll → import live**, and it **stops on its own** when
it reaches posts you already have.

## Setup

1. **Get your token** (skip if your dashboard runs token-free on localhost):
   ```
   cat ~/.hermes/weftbase-token
   ```
2. **Load the extension:**
   - **Firefox:** `about:debugging#/runtime/this-firefox` → **Load Temporary
     Add-on…** → pick `extension/manifest.json`.
   - **Chrome / Edge:** `chrome://extensions` → enable **Developer mode** → **Load
     unpacked** → pick the `extension/` folder. (Chrome reads `manifest.json`, so
     rename `manifest.chrome.json` → `manifest.json` first, or keep a Chrome copy
     of the folder with the MV3 manifest as `manifest.json`.)
3. Click the extension icon → set **endpoint** (`http://localhost:9119`, or your
   Cloudflare tunnel URL) + paste the **token** → **Save** → **Test connection**
   (should say *Connected ✓*).

## Use

1. Open your Facebook **profile** or **Activity Log** (`facebook.com/me`).
2. Click the blue **“▶ Scan to fbbackup”** button (bottom-right).
3. **Scroll.** Posts are scraped, imported in batches, and the counter shows
   `scanned · new`. It **stops automatically** when it scrolls into posts you
   already have; click to stop sooner.
4. Open the **FB Browser** — scanned posts appear with reshared video/post embeds
   and images backed up locally.

To keep current, just run it again now and then — it only grabs what's new.

## How the auto-stop works

Two boundaries, whichever comes first (or your manual stop):

- **Re-scans:** after ~12 consecutive posts that are **already imported** (same FB
  permalink id), it stops — you've scrolled back into known territory.
- **First scan:** it fetches `GET /api/fb/cutoff` (your export's newest post time)
  and stops after ~12 consecutive posts **older than the export** — so a first run
  only imports the gap between the export and now.

## How it works / limits

- Captures: permalink, text (with emoji), images, reshare attribution, and a
  best-effort timestamp. Reshared videos/posts display via the FB embed, so the
  extension doesn't download videos.
- Imports are **batched** (8/request) to `POST /api/fb/import`; images saved to
  `spaces-data/_live-media/` (permanent local backup).
- Live posts are browsable + keyword-searchable immediately; re-run
  `fbbackup embed` to add them to **semantic** search.
- Facebook's feed DOM changes often → scraping is best-effort; some posts/fields
  may be missed. When FB doesn't expose the original time, it falls back to "now"
  (those don't count toward the timestamp cutoff).
- Cross-browser: one codebase; `background.js` works as a Firefox background page
  and a Chrome MV3 service worker. Auth: `X-Weftbase-Token` (token-free on local
  loopback in the FB profile).
