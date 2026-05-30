// fbbackup scanner — background (works as a Firefox background page AND a Chrome
// MV3 service worker: listener-only, no persistent state, sendResponse + return
// true so async replies work in both). Reaches localhost / the tunnel directly,
// so it isn't blocked by facebook.com's CORS.

const ext = (typeof browser !== "undefined") ? browser : chrome;

async function cfg() {
  const d = await ext.storage.local.get(["endpoint", "token"]);
  return {
    endpoint: (d.endpoint || "http://localhost:9119").replace(/\/+$/, ""),
    token: d.token || "",
  };
}

async function jfetch(path, opts) {
  const { endpoint, token } = await cfg();
  const headers = Object.assign({ "X-Weftbase-Token": token }, (opts && opts.headers) || {});
  return fetch(endpoint + path, Object.assign({}, opts, { headers }));
}

async function handle(msg) {
  if (!msg) return {};
  // single post OR a batch (an array) — same endpoint.
  if (msg.type === "import" || msg.type === "importBatch") {
    try {
      const r = await jfetch("/api/fb/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(msg.payload),
      });
      const result = await r.json().catch(() => ({}));
      // normalise: batch → {results:[...]}, single → one object.
      const results = result.results || (result.ok ? [result] : []);
      return { ok: r.ok, status: r.status, result, results };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }
  if (msg.type === "cutoff") {
    try { return await (await jfetch("/api/fb/cutoff")).json(); }
    catch (e) { return {}; }
  }
  if (msg.type === "ping") {
    try { const r = await jfetch("/api/fb/db"); return { ok: r.ok, status: r.status }; }
    catch (e) { return { ok: false, error: String(e) }; }
  }
  return {};
}

ext.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handle(msg).then(sendResponse).catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // async sendResponse — required by Chrome MV3, honored by Firefox
});
