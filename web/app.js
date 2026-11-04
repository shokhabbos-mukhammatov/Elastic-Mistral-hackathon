const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ICON = { low: "✓", elevated: "!", high: "⚠", severe: "⛔" };
const LABEL = { low: "Low", elevated: "Elevated", high: "High", severe: "Severe" };
const COLOR = { elevated: "#e0a100", high: "#d93025", severe: "#8e24aa" };
let here = { lat: 37.7637, lon: -122.4152 };

async function api(url, opts) {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

// ---------------------------------------------------------------- map (Elasticsearch _mvt vector tiles)
const map = new maplibregl.Map({
  container: "map",
  style: { version: 8, sources: { base: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256,
    attribution: "© OpenStreetMap contributors" } }, layers: [{ id: "base", type: "raster", source: "base" }] },
  center: [here.lon, here.lat], zoom: 13, cooperativeGestures: true,
});
map.on("load", () => {
  const origin = location.origin;
  map.addSource("zones", { type: "vector", tiles: [`${origin}/tiles/zones/{z}/{x}/{y}.pbf`], minzoom: 10 });
  map.addLayer({ id: "zones", type: "fill", source: "zones", "source-layer": "hits", paint: { "fill-color": "#1e6fd9", "fill-opacity": 0.22 } });
  map.addSource("reports", { type: "vector", tiles: [`${origin}/tiles/reports/{z}/{x}/{y}.pbf`], minzoom: 10 });
  map.addLayer({ id: "reports", type: "circle", source: "reports", "source-layer": "hits", paint: { "circle-radius": 5, "circle-color": ["match", ["get", "category"],
    "flooding", "#0b5cad", "catch_basin", "#00897b", "sewer_backup", "#6d4c41", "tree", "#2e7d32", "#757575"], "circle-stroke-width": 1, "circle-stroke-color": "#fff" } });
  map.addSource("replay", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "replay", type: "fill", source: "replay", paint: { "fill-color": ["get", "color"], "fill-opacity": 0.55 } });
  map.addSource("rreports", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "rreports", type: "circle", source: "rreports", paint: { "circle-radius": 4, "circle-color": "#0b5cad", "circle-stroke-width": 1, "circle-stroke-color": "#fff" } });
  map.addSource("me", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "me", type: "circle", source: "me", paint: { "circle-radius": 9, "circle-color": "#111", "circle-stroke-width": 3, "circle-stroke-color": "#fff" } });
});

function setMe(lat, lon) {
  here = { lat, lon };
  const src = map.getSource("me");
  if (src) src.setData({ type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] } }] });
  map.flyTo({ center: [lon, lat], zoom: 15 });
}

// ---------------------------------------------------------------- briefing
function renderBrief(b) {
  const lv = b.risk.level;
  const reasons = b.reasons.map((r) => `<li>${esc(r.text)} <span class="src">— ${esc(r.source || "")}</span></li>`).join("");
  const top = (b.history.top_days || []).map((d) => `${esc(d.day)} (${d.reports})`).join(", ");
  $("#brief").innerHTML = `
    <p><span class="level ${lv}" role="img" aria-label="Risk level ${LABEL[lv]}">${ICON[lv]} ${LABEL[lv]} risk</span>
      <span class="src">${esc(b.place)}${b.neighborhood ? " · " + esc(b.neighborhood) : ""} · as of ${new Date(b.as_of).toLocaleTimeString()}</span></p>
    <p class="narr" lang="${esc(b.narrative.lang)}">${esc(b.narrative.text)}</p>
    <p class="src">Written by ${esc(b.narrative.by)}${b.narrative.dropped?.length ? ` · ${b.narrative.dropped.length} unsupported sentence(s) removed by the receipts check` : ""}</p>
    <h3>Why</h3><ul class="list">${reasons || "<li>No active rain, alerts or reports near this block.</li>"}</ul>
    <h3>What to do</h3><ul class="list">${b.actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
    <h3>Storm history (5 years)</h3><p>${b.history.flood_reports_5y} flooding / drain reports on or next to this block.${top ? " Worst days: " + top + "." : ""}</p>
    <h3>Help</h3><ul class="list">${b.help.map((h) => `<li><a href="${esc(h.url)}">${esc(h.text)}</a></li>`).join("")}</ul>
    <p class="src">Rain last 3 h: ${b.inputs.rain_3h} mm · next 3 h: ${b.inputs.rain_next3h} mm · tide: ${b.inputs.tide_ft ?? "n/a"} ft · ${esc(b.disclaimer)}</p>`;
}

async function brief(params) {
  $("#brief").textContent = "Checking your block…";
  try {
    const b = await api(`/api/briefing?${new URLSearchParams({ ...params, lang: $("#lang").value })}`);
    renderBrief(b);
    setMe(b.location.lat, b.location.lon);
    loadNearby();
  } catch (e) { $("#brief").textContent = e.message; }
}
$("#f-brief").addEventListener("submit", (e) => { e.preventDefault(); brief({ address: $("#addr").value }); });
$("#b-locate").addEventListener("click", () => navigator.geolocation?.getCurrentPosition(
  (p) => brief({ lat: p.coords.latitude, lon: p.coords.longitude }), () => ($("#brief").textContent = "Location unavailable. Type an address instead.")));

async function loadNearby() {
  const rows = await api(`/api/nearby?lat=${here.lat}&lon=${here.lon}&hours=24`).catch(() => []);
  $("#nearby").innerHTML = rows.length ? rows.slice(0, 20).map((r) =>
    `<li>${esc(r.category.replace("_", " "))} — ${esc(r.address || "")} — ${r.distance_m} m away — ${new Date(r.requested_at).toLocaleString()} (${esc(r.status || "open")})</li>`).join("")
    : "<li>No reports within 500 m in the last 24 hours.</li>";
}

// ---------------------------------------------------------------- ask
$("#f-ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#answer").textContent = "Thinking…";
  try {
    const a = await api("/api/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question: $("#q").value, lat: here.lat, lon: here.lon }) });
    const cites = (a.citations || []).map((c) => `<li><a href="${esc(c.url)}">${esc(c.title || c.url)}</a></li>`).join("");
    const tools = (a.tools || []).map((t) => `<code>${esc(t)}</code>`).join(", ");
    $("#answer").innerHTML = `<p>${esc(a.answer)}</p>${cites ? `<ul class="list">${cites}</ul>` : ""}${tools ? `<p class="src">Tools used: ${tools}</p>` : ""}`;
  } catch (err) { $("#answer").textContent = err.message; }
});

// ---------------------------------------------------------------- report
$("#f-report").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("lat", here.lat); fd.append("lon", here.lon); fd.append("text", $("#rtext").value);
  if ($("#rphoto").files[0]) fd.append("photo", $("#rphoto").files[0]);
  $("#draft").textContent = "Checking…";
  try {
    const d = await api("/api/report", { method: "POST", body: fd });
    const dup = (d.duplicates || []).map((x) => `<li>${esc(x.category)} at ${esc(x.address || "")} (${x.distance_m} m, ${new Date(x.requested_at).toLocaleTimeString()})</li>`).join("");
    $("#draft").innerHTML = d.blocked ? `<p>${esc(d.message)}</p>` : `
      ${dup ? `<p><b>Already reported nearby:</b></p><ul class="list">${dup}</ul>` : "<p>No matching report nearby yet.</p>"}
      <p><b>Draft for SF311:</b> ${esc(d.draft.category_label)} — ${esc(d.draft.description)}</p>
      <p class="src">Classified by ${esc(d.by)}${d.vision ? ` · photo: ${esc(JSON.stringify(d.vision))}` : ""}</p>
      <p><a href="${esc(d.handoff_url)}">Send to SF311</a> (opens the official SF311 form)</p>`;
  } catch (err) { $("#draft").textContent = err.message; }
});

// ---------------------------------------------------------------- replay + backtest
let meta = null, timer = null;
async function loadReplay() {
  const day = $("#rday").value;
  meta = await api(`/api/replay/${day}`).catch((e) => { $("#rstats").textContent = e.message; return null; });
  if (meta) frame();
  api(`/api/backtest/${day}`).then((b) => {
    const m = b.median_lead_min;
    $("#backtest").innerHTML = `<div class="stats">
      <div class="stat"><b>${b.blocks_with_flood_reports}</b>blocks with flood reports</div>
      <div class="stat"><b>${b.flagged_before_or_at_report}</b>flagged High before (or when) their first report came in</div>
      <div class="stat"><b>${m == null ? "n/a" : Math.round(m) + " min"}</b>median warning lead time</div>
      <div class="stat"><b>${b.false_alarm_blocks}</b>flagged blocks with no report within 6 h</div></div>
      <p class="src">First High flag: ${new Date(b.first_flag).toLocaleTimeString("en-US", { timeZone: "America/Los_Angeles" })} PT ·
      first flood report: ${new Date(b.first_report).toLocaleTimeString("en-US", { timeZone: "America/Los_Angeles" })} PT · rain: ${esc(b.weather_source)}</p>`;
  }).catch((e) => ($("#backtest").textContent = e.message));
}
async function frame() {
  const t = new Date(new Date(meta.start).getTime() + Number($("#rt").value) * 15 * 60000);
  $("#rclock").textContent = t.toLocaleString("en-US", { timeZone: "America/Los_Angeles", dateStyle: "medium", timeStyle: "short" }) + " PT";
  const f = await api(`/api/replay/${$("#rday").value}?t=${encodeURIComponent(t.toISOString())}`);
  const counts = { elevated: 0, high: 0, severe: 0 };
  const feats = Object.entries(f.cells).map(([c, v]) => { counts[v.level]++; return { type: "Feature", geometry: f.polygons[c], properties: { color: COLOR[v.level] } }; });
  map.getSource("replay")?.setData({ type: "FeatureCollection", features: feats });
  map.getSource("rreports")?.setData({ type: "FeatureCollection", features: f.reports.map((r) => ({ type: "Feature", geometry: { type: "Point", coordinates: [r.lon, r.lat] } })) });
  const h = meta.hours[Math.floor(Number($("#rt").value) / 4)] || {};
  $("#rstats").innerHTML = `<div class="stats"><div class="stat"><b>${h.rain_mm ?? 0} mm</b>rain this hour (central SF)</div>
    <div class="stat"><b>${counts.high + counts.severe}</b>blocks at High or Severe</div><div class="stat"><b>${counts.elevated}</b>blocks Elevated</div>
    <div class="stat"><b>${f.reports.length}</b>311 reports in the last 6 h</div></div>`;
}
$("#rt").addEventListener("input", () => meta && frame());
$("#b-play").addEventListener("click", () => {
  if (timer) { clearInterval(timer); timer = null; $("#b-play").textContent = "Play"; return; }
  $("#b-play").textContent = "Pause";
  map.flyTo({ center: [-122.42, 37.755], zoom: 12 });
  timer = setInterval(() => { const v = (Number($("#rt").value) + 1) % 96; $("#rt").value = v; frame(); }, 900);
});
$("#rday").addEventListener("change", loadReplay);

brief({ address: $("#addr").value });
loadReplay();
