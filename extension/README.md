# fbbackup scanner (Firefox extension)

Scans your Facebook posts into your local **fbbackup** archive as you scroll —
filling in the reshares and recent posts the data export can't include. Each
scraped post becomes a markdown row alongside the export data, so it shows up in
the FB Browser seamlessly.

No scheduling, no queue, no daily cron: **scan mode = scroll → import live.**

## Setup

1. **Get your token:**
   ```
   cat ~/.hermes/weftbase-token
   ```
2. **Load the extension** in Firefox:
   - Go to `about:debugging#/runtime/this-firefox`
   - **Load Temporary Add-on…** → pick `extension/manifest.json`
3. Click the extension icon → set **endpoint** (`http://localhost:9119`, or your
   Cloudflare tunnel URL) + paste the **token** → **Save** → **Test connection**
   (should say *Connected ✓*).

## Use

1. Open your Facebook **profile** or **Activity Log** (`facebook.com/me`).
2. Click the blue **“▶ Scan to fbbackup”** button (bottom-right).
3. **Scroll.** Each post that loads is scraped and imported; the counter shows
   progress. Click again to stop.
4. Open the **FB Browser** in your Weft dashboard — scanned posts appear with the
   reshared video/post embedded and images backed up locally.

## How it works / limits

- Captures: permalink, text (with emoji), images, reshare attribution, and a
  best-effort timestamp. Reshared videos/posts display via the FB embed in the
  browser, so the extension doesn't download videos.
- Images are downloaded to `spaces-data/_live-media/` (permanent local backup).
- Dedupe is by post id; re-scanning updates in place. Cleanup can happen later.
- Facebook's feed DOM changes often, so scraping is best-effort — some posts or
  fields may be missed. Timestamps fall back to "now" when FB doesn't expose the
  original time in the markup (fine for catching up recent posts; older
  backfills may need the year corrected).
- Auth: the background script sends `X-Weftbase-Token`; the dashboard accepts it
  for `/api/fb/*` (same mechanism the wiki uses).
