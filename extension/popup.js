const $ = (id) => document.getElementById(id);
const ext = (typeof browser !== "undefined") ? browser : chrome;

ext.storage.local.get(["endpoint", "token"]).then((d) => {
  $("endpoint").value = d.endpoint || "http://localhost:9119";
  $("token").value = d.token || "";
});

$("save").onclick = async () => {
  await ext.storage.local.set({
    endpoint: $("endpoint").value.trim(),
    token: $("token").value.trim(),
  });
  $("st").textContent = "Saved ✓";
};

$("test").onclick = async () => {
  await ext.storage.local.set({
    endpoint: $("endpoint").value.trim(),
    token: $("token").value.trim(),
  });
  $("st").textContent = "Testing…";
  const r = await ext.runtime.sendMessage({ type: "ping" });
  $("st").textContent = r && r.ok ? "Connected ✓" : `Failed: ${r ? r.status || r.error : "no response"}`;
};
