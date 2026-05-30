"use strict";

const feed = document.getElementById("feed");
const statusEl = document.getElementById("status");
const countEl = document.getElementById("count");
const qEl = document.getElementById("q");
const typeEl = document.getElementById("type");
const yearEl = document.getElementById("year");
const hasMediaEl = document.getElementById("hasMedia");

let offset = 0, done = false, loading = false;

function esc(s) {
  return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function linkify(text) {
  // escape, then turn bare URLs into links
  return esc(text).replace(/(https?:\/\/[^\s]+)/g,
    u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`);
}

const fmtDate = ts => new Date(ts * 1000).toLocaleString(undefined,
  { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function mediaGrid(media) {
  if (!media.length) return "";
  const shown = media.slice(0, 4);
  const cls = media.length === 1 ? "n1" : media.length === 2 ? "n2" : "nmany";
  const cells = shown.map((m, i) => {
    const src = `/media?uri=${encodeURIComponent(m.uri)}`;
    const extra = (i === 3 && media.length > 4)
      ? ` class="more-badge" data-more="+${media.length - 4}"` : "";
    const el = m.kind === "video"
      ? `<video src="${src}" controls preload="metadata"></video>`
      : `<img loading="lazy" src="${src}" alt="${esc(m.caption)}">`;
    return `<div${extra}>${el}</div>`;
  }).join("");
  return `<div class="media-grid ${cls}">${cells}</div>`;
}

function linkCards(links) {
  return links.map(l => `
    <div class="linkcard"><a href="${esc(l.url)}" target="_blank" rel="noopener">
      ${l.source ? `<div class="src">${esc(l.source)}</div>` : ""}
      ${l.name ? `<div class="nm">${esc(l.name)}</div>` : ""}
      <div class="url">${esc(l.url)}</div>
    </a></div>`).join("");
}

function renderPost(p) {
  const el = document.createElement("article");
  el.className = "post";
  const place = p.place && p.place.name
    ? `<div class="post-place">📍 ${esc(p.place.name)}${p.place.address ? " — " + esc(p.place.address) : ""}</div>` : "";
  const group = p.group ? ` <span class="post-group">› ${esc(p.group)}</span>` : "";
  const tags = p.hashtags.length
    ? `<div class="tags">${p.hashtags.map(t => `<span class="tag">#${esc(t)}</span>`).join("")}</div>` : "";
  el.innerHTML = `
    <div class="post-head">
      <div class="post-title">${esc(p.title)}${group}</div>
      <div class="post-meta">${fmtDate(p.timestamp)} · ${esc(p.type)}</div>
    </div>
    ${p.text ? `<div class="post-body">${linkify(p.text)}</div>` : ""}
    ${place}
    ${mediaGrid(p.media)}
    ${linkCards(p.links)}
    ${tags}`;
  return el;
}

async function loadMore() {
  if (loading || done) return;
  loading = true;
  statusEl.textContent = "Loading…";
  const params = new URLSearchParams({ offset, limit: 30 });
  if (qEl.value.trim()) params.set("q", qEl.value.trim());
  if (typeEl.value) params.set("type", typeEl.value);
  if (yearEl.value) params.set("year", yearEl.value);
  if (hasMediaEl.checked) params.set("has_media", "true");
  const r = await fetch("/api/posts?" + params);
  const data = await r.json();
  for (const p of data.posts) feed.appendChild(renderPost(p));
  offset = data.next_offset;
  done = data.done;
  statusEl.textContent = done ? (feed.children.length ? "— end of archive —" : "No matching posts.") : "";
  loading = false;
  // keep filling until the viewport is covered
  if (!done && document.body.scrollHeight <= window.innerHeight + 300) loadMore();
}

function reset() {
  feed.innerHTML = "";
  offset = 0; done = false; loading = false;
  loadMore();
}

let debounce;
qEl.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(reset, 250); });
[typeEl, yearEl, hasMediaEl].forEach(e => e.addEventListener("change", reset));

new IntersectionObserver(es => { if (es[0].isIntersecting) loadMore(); }, { rootMargin: "600px" })
  .observe(document.getElementById("sentinel"));

(async function init() {
  const m = await (await fetch("/api/meta")).json();
  countEl.textContent = m.total.toLocaleString() + " posts";
  for (const [t, n] of Object.entries(m.by_type).sort((a, b) => b[1] - a[1]))
    typeEl.insertAdjacentHTML("beforeend", `<option value="${t}">${t} (${n})</option>`);
  for (const y of m.years)
    yearEl.insertAdjacentHTML("beforeend", `<option value="${y}">${y}</option>`);
  loadMore();
})();
