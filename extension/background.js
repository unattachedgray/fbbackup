// fbbackup scanner — background.
// Receives scraped posts from scan.js and POSTs them to the fbbackup import
// endpoint. Runs in the extension context (not the page), so it isn't subject
// to facebook.com's CORS — it can reach localhost / the Cloudflare tunnel.

async function cfg() {
  const d = await browser.storage.local.get(["endpoint", "token"]);
  return {
    endpoint: (d.endpoint || "http://localhost:9119").replace(/\/+$/, ""),
    token: d.token || "",
  };
}

browser.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "import") {
    return cfg().then(async ({ endpoint, token }) => {
      try {
        const r = await fetch(endpoint + "/api/fb/import", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Weftbase-Token": token },
          body: JSON.stringify(msg.payload),
        });
        const result = await r.json().catch(() => ({}));
        return { ok: r.ok, status: r.status, result };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    });
  }
  if (msg && msg.type === "ping") {
    return cfg().then(async ({ endpoint, token }) => {
      try {
        const r = await fetch(endpoint + "/api/fb/db", { headers: { "X-Weftbase-Token": token } });
        return { ok: r.ok, status: r.status };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    });
  }
});
