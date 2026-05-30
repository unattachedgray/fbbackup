// fbbackup scanner — scan mode (Chrome + Firefox).
//
// Toggle the floating button, then scroll your Facebook profile / activity log.
// Posts that scroll into view are scraped, batched, and POSTed (via the
// background script) to your local fbbackup archive, where they become markdown
// rows alongside the export. It stops on its own when it scrolls into posts you
// already have — either ones already imported (a re-scan) or ones older than
// your export (a first scan) — so each run only grabs what's new. Stop anytime.
//
// FB's feed DOM is messy and changes often, so scraping is best-effort: it
// captures permalink, text (with emoji), images, timestamp, and reshare
// attribution. Reshared videos/posts display via the original-URL embed, so the
// extension doesn't download videos.
(function () {
  if (window.__fbbackupScan) return;
  window.__fbbackupScan = true;

  const ext = (typeof browser !== "undefined") ? browser : chrome;

  const BATCH = 8;        // posts per import request
  const STOP_AFTER = 12;  // consecutive "already have it" posts → reached the boundary

  let scanning = false;
  const seen = new Set();
  let scanned = 0, imported = 0, failed = 0;
  let buf = [], existedRun = 0, oldRun = 0, cutoffMs = 0, doneReason = "";

  // ── floating UI ──────────────────────────────────────────────────────────
  const btn = document.createElement("div");
  btn.style.cssText =
    "position:fixed;bottom:16px;right:16px;z-index:2147483647;background:#1877f2;color:#fff;" +
    "font:600 13px system-ui,sans-serif;padding:9px 14px;border-radius:22px;cursor:pointer;" +
    "box-shadow:0 2px 10px rgba(0,0,0,.35);user-select:none";
  const status = document.createElement("div");
  status.style.cssText =
    "position:fixed;bottom:58px;right:16px;z-index:2147483647;background:rgba(0,0,0,.82);color:#fff;" +
    "font:11px ui-monospace,monospace;padding:5px 9px;border-radius:7px;max-width:260px;display:none";
  function paint() {
    btn.textContent = scanning ? "⏸ Scanning… (click to stop)" : "▶ Scan to fbbackup";
    btn.style.background = scanning ? "#e4405f" : "#1877f2";
    status.style.display = scanning || scanned ? "block" : "none";
    let s = `scanned ${scanned} · new ${imported}` + (failed ? ` · ${failed} failed` : "");
    if (!scanning && doneReason) s += `\n✓ ${doneReason}`;
    status.textContent = s;
  }
  function mount() {
    if (!document.documentElement.contains(btn)) document.documentElement.appendChild(btn);
    if (!document.documentElement.contains(status)) document.documentElement.appendChild(status);
  }
  mount();
  setInterval(mount, 3000); // FB SPA nav can wipe the DOM; re-attach

  btn.onclick = () => { if (scanning) stop("stopped"); else start(); };

  // ── extraction helpers ───────────────────────────────────────────────────
  function richText(el) {
    if (!el) return "";
    let out = "";
    el.childNodes.forEach((n) => {
      if (n.nodeType === 3) out += n.textContent;
      else if (n.tagName === "IMG" && n.alt) out += n.alt;
      else if (n.tagName === "BR") out += "\n";
      else if (n.nodeType === 1) out += richText(n);
    });
    return out;
  }
  function findPermalink(scope) {
    for (const a of scope.querySelectorAll("a[href]")) {
      const h = a.getAttribute("href") || "";
      if (/(\/(posts|permalink|videos|reel)\/|story_fbid=|\/share\/[pvr]\/|pfbid)/.test(h) &&
          !/(\/photo|comment_id|__tn__|\/reactions\/)/.test(h)) {
        return a.href;
      }
    }
    return "";
  }
  function fbIdOf(url) {
    const m = url.match(/(pfbid[A-Za-z0-9]+)/) || url.match(/story_fbid=(\d{6,})/) || url.match(/\/(\d{8,})/);
    return m ? m[1] : "";
  }
  // {ms, exact}: `exact` only when FB embedded the real creation time (we trust
  // it for the timestamp cutoff; the Date.now() fallback is not trusted).
  function postTime(article) {
    const m = article.innerHTML.match(/"(?:creation_time|publish_time)":(\d{9,11})/);
    if (m) return { ms: Number(m[1]) * 1000, exact: true };
    return { ms: Date.now(), exact: false };
  }
  function images(scope) {
    const out = [];
    scope.querySelectorAll("img").forEach((im) => {
      const s = im.currentSrc || im.src || "";
      if (/(scontent|fbcdn)/.test(s) && im.naturalWidth >= 250 &&
          !/(static|emoji|s60x60|p24x24|safe_image|sticker)/.test(s)) {
        out.push(s);
      }
    });
    return [...new Set(out)].slice(0, 12);
  }
  function scrape(article) {
    const permalink = findPermalink(article);
    const id = fbIdOf(permalink) || permalink;
    if (!id || seen.has(id)) return null;

    const msg = article.querySelector('[data-ad-comet-preview="message"],[data-ad-preview="message"]');
    const body_text = richText(msg).trim();

    let is_share = 0, original_author = "", original_url = "", original_text = "";
    const nested = article.querySelector('[role="article"]');
    if (nested && nested !== article) {
      is_share = 1;
      const au = nested.querySelector("h3 a, h4 a, strong span, strong a");
      original_author = au ? (au.innerText || "").trim() : "";
      original_url = findPermalink(nested);
      const omsg = nested.querySelector('[data-ad-comet-preview="message"],[data-ad-preview="message"]');
      original_text = richText(omsg).trim();
    }

    const t = postTime(article);
    seen.add(id);
    return {
      fb_id: id, source_url: permalink, post_time: t.ms, _exact: t.exact,
      body_text, image_urls: images(article),
      is_share, original_author, original_url, original_text,
    };
  }

  // ── scan loop: scrape → batch → import → detect the boundary ──────────────
  async function start() {
    scanning = true; buf = []; existedRun = 0; oldRun = 0; doneReason = "";
    try { const c = await ext.runtime.sendMessage({ type: "cutoff" }); cutoffMs = (c && c.max_ts) || 0; }
    catch (e) { cutoffMs = 0; }
    paint();
    sweep();
  }
  function stop(reason) { scanning = false; doneReason = reason || ""; void flush(); paint(); }

  async function flush() {
    if (!buf.length) return;
    const batch = buf; buf = [];
    let res;
    try { res = await ext.runtime.sendMessage({ type: "importBatch", payload: batch }); }
    catch (e) { failed += batch.length; paint(); return; }
    const results = (res && res.ok && res.results) || [];
    results.forEach((r, i) => {
      if (r && r.ok) {
        if (r.existed) existedRun++; else { existedRun = 0; imported++; }
      } else failed++;
      const p = batch[i];
      if (p && p._exact && cutoffMs) { if (p.post_time < cutoffMs) oldRun++; else oldRun = 0; }
    });
    paint();
    if (existedRun >= STOP_AFTER) stop("reached posts already in your archive — done");
    else if (oldRun >= STOP_AFTER) stop("reached your export's date — older posts are already backed up");
  }

  async function sweep() {
    if (!scanning) return;
    for (const a of document.querySelectorAll('[role="article"]')) {
      if (a.parentElement && a.parentElement.closest('[role="article"]')) continue; // skip nested original
      let p;
      try { p = scrape(a); } catch (e) { p = null; }
      if (!p) continue;
      if (!p.body_text && !p.image_urls.length && !p.is_share) continue;
      scanned++; buf.push(p); paint();
      if (buf.length >= BATCH) { await flush(); if (!scanning) return; }
    }
    await flush();
    if (!scanning) return;
    setTimeout(sweep, 1500);
  }
})();
