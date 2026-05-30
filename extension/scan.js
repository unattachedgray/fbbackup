// fbbackup scanner — scan mode.
//
// Toggle the floating button, then scroll your Facebook profile / activity log.
// Every post that scrolls into view is scraped and POSTed (via the background
// script) to your local fbbackup import endpoint, where it becomes a markdown
// row alongside the export data. No scheduling, no queue — just scroll.
//
// FB's feed DOM is messy and changes often, so this is best-effort: it captures
// permalink, text (with emoji), images, timestamp, and reshare attribution.
// Reshared videos/posts display in the FB Browser via the original-URL embed,
// so the extension does not need to download videos.
(function () {
  if (window.__fbbackupScan) return;
  window.__fbbackupScan = true;

  let scanning = false;
  const seen = new Set();
  let scanned = 0, imported = 0, failed = 0;

  // ── floating UI ──────────────────────────────────────────────────────────
  const btn = document.createElement("div");
  btn.style.cssText =
    "position:fixed;bottom:16px;right:16px;z-index:2147483647;background:#1877f2;color:#fff;" +
    "font:600 13px system-ui,sans-serif;padding:9px 14px;border-radius:22px;cursor:pointer;" +
    "box-shadow:0 2px 10px rgba(0,0,0,.35);user-select:none";
  btn.textContent = "▶ Scan to fbbackup";
  const status = document.createElement("div");
  status.style.cssText =
    "position:fixed;bottom:58px;right:16px;z-index:2147483647;background:rgba(0,0,0,.8);color:#fff;" +
    "font:11px ui-monospace,monospace;padding:5px 9px;border-radius:7px;max-width:240px;display:none";
  function paint() {
    btn.textContent = scanning ? "⏸ Scanning… (click to stop)" : "▶ Scan to fbbackup";
    btn.style.background = scanning ? "#e4405f" : "#1877f2";
    status.style.display = scanning || scanned ? "block" : "none";
    status.textContent = `scanned ${scanned} · imported ${imported}` + (failed ? ` · ${failed} failed` : "");
  }
  function mount() {
    if (!document.body.contains(btn)) document.documentElement.appendChild(btn);
    if (!document.body.contains(status)) document.documentElement.appendChild(status);
  }
  mount();
  setInterval(mount, 3000); // FB SPA nav can wipe the DOM; re-attach

  btn.onclick = () => {
    scanning = !scanning;
    paint();
    if (scanning) sweep();
  };

  // ── extraction helpers ───────────────────────────────────────────────────
  // innerText drops emoji (rendered as <img alt>); walk nodes to keep them.
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

  function postTime(article) {
    // Best-effort: FB sometimes embeds the unix creation time in the markup.
    const m = article.innerHTML.match(/"(?:creation_time|publish_time)":(\d{9,11})/);
    if (m) return Number(m[1]) * 1000;
    return Date.now(); // fallback — recent posts land in the current period
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

    // Reshare: a nested article is the original post.
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

    seen.add(id);
    return {
      fb_id: id,
      source_url: permalink,
      post_time: postTime(article),
      body_text,
      image_urls: images(article),
      is_share,
      original_author,
      original_url,
      original_text,
    };
  }

  // ── sweep loop ───────────────────────────────────────────────────────────
  async function sweep() {
    if (!scanning) return;
    const arts = document.querySelectorAll('[role="article"]');
    for (const a of arts) {
      // top-level posts only (skip the nested original inside a reshare)
      if (a.parentElement && a.parentElement.closest('[role="article"]')) continue;
      let p;
      try { p = scrape(a); } catch (e) { p = null; }
      if (!p) continue;
      if (!p.body_text && !p.image_urls.length && !p.is_share) continue;
      scanned++; paint();
      try {
        const res = await browser.runtime.sendMessage({ type: "import", payload: p });
        if (res && res.ok) imported++; else failed++;
      } catch (e) { failed++; }
      paint();
    }
    if (scanning) setTimeout(sweep, 1500);
  }
})();
