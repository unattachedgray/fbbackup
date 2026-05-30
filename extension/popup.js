const $ = (id) => document.getElementById(id);

browser.storage.local.get(["endpoint", "token"]).then((d) => {
  $("endpoint").value = d.endpoint || "http://localhost:9119";
  $("token").value = d.token || "";
});

$("save").onclick = async () => {
  await browser.storage.local.set({
    endpoint: $("endpoint").value.trim(),
    token: $("token").value.trim(),
  });
  $("st").textContent = "Saved ✓";
};

$("test").onclick = async () => {
  await browser.storage.local.set({
    endpoint: $("endpoint").value.trim(),
    token: $("token").value.trim(),
  });
  $("st").textContent = "Testing…";
  const r = await browser.runtime.sendMessage({ type: "ping" });
  $("st").textContent = r && r.ok ? "Connected ✓" : `Failed: ${r ? r.status || r.error : "no response"}`;
};
