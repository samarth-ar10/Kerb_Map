/* Kerb Map: map page. Data calls go through KerbAPI (api.js). */
(function () {
  "use strict";
  var A = window.KerbAPI, UI = window.KerbUI, esc = A.esc;
  var $ = function (id) { return document.getElementById(id); };
  var STEP_NAMES = {
    gate: "Distance gate", boxes: "Stored car boxes", geometry: "Kerb geometry", rule: "Street rules (OSM)",
    cosmos: "Cosmos check", track: "Tracked over time", position: "Position", overrule: "Overruled by a person"
  };
  var START = [43.6532, -79.4017];

  var currentTheme = UI.theme();
  var map = L.map("map", { zoomControl: true, zoomSnap: 0.25 }).setView(START, 17);
  var tiles = null;
  function setTiles() {
    if (tiles) return;
    tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, className: "basemap",
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);
  }
  setTiles();
  document.addEventListener("kerb-theme", setTiles);

  var coverageLayer = L.layerGroup().addTo(map);
  var pinLayer = L.layerGroup().addTo(map);
  var queryLayer = L.layerGroup().addTo(map);
  var markers = {};
  var state = { pin: null, rows: [], selected: null, q: "" };
  var replay = { active: false, id: null, timer: null, seen: {} };

  // ---------------------------------------------------------------- legend
  var legend = L.control({ position: "bottomright" });
  legend.onAdd = function () {
    var d = L.DomUtil.create("div", "legend-box");
    d.innerHTML = "<h3>Kerb</h3>" +
      ["free", "occupied", "not_space", "cannot_tell"].map(function (s) { return "<div>" + UI.glyph(s) + UI.stateLabel(s) + "</div>"; }).join("") +
      '<div><span class="glyph unc">?</span>Position uncertain</div>' +
      '<h3 style="margin-top:8px">Coverage</h3>' +
      '<div><span class="ln" style="border-top:8px solid var(--streetcam);opacity:.55"></span>Street camera view</div>' +
      '<div><span class="ln" style="border-top:4px dotted var(--dashcam)"></span>Dashcam pass</div>' +
      '<div><span class="ln" style="border-top:2px solid var(--line)"></span>No mark: not seen</div>';
    L.DomEvent.disableClickPropagation(d);
    return d;
  };
  legend.addTo(map);

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // ---------------------------------------------------------------- coverage
  async function drawCoverage(rows) {
    coverageLayer.clearLayers();
    var cov;
    try { cov = await A.coverage(rows); } catch (e) { return; }
    if (!cov) return;
    var sc = cssVar("--streetcam"), dc = cssVar("--dashcam");
    (cov.passes || []).forEach(function (p) {
      if (!p.line || p.line.length < 2) return;
      L.polyline(p.line, { color: dc, weight: 4, dashArray: "1 9", lineCap: "round", opacity: .95, interactive: false }).addTo(coverageLayer);
    });
    (cov.streetcam_stretches || []).forEach(function (p) {
      if (!p.line || !p.line.length) return;
      L.polyline(p.line.length > 1 ? p.line : [p.line[0], p.line[0]], { color: sc, weight: 16, opacity: .35, lineCap: "round", interactive: false }).addTo(coverageLayer);
    });
    (cov.cameras || []).forEach(function (c) {
      if (c.lat == null || c.lon == null) return;
      if (c.heading != null) {
        var pts = [[c.lat, c.lon]], R = 70, spread = 32;
        for (var a = -spread; a <= spread; a += 8) {
          var br = (c.heading + a) * Math.PI / 180;
          pts.push([c.lat + (R * Math.cos(br)) / 111320, c.lon + (R * Math.sin(br)) / (111320 * Math.cos(c.lat * Math.PI / 180))]);
        }
        L.polygon(pts, { color: sc, weight: 2, fillColor: sc, fillOpacity: .18, interactive: false }).addTo(coverageLayer);
      }
      L.marker([c.lat, c.lon], {
        icon: L.divIcon({ className: "", html: '<span class="kpin" style="background:' + sc + ';color:#04150f;width:24px;height:24px;border-radius:50%">▣</span>', iconSize: [24, 24], iconAnchor: [12, 12] }),
        keyboard: false
      }).bindTooltip("Street camera " + esc(c.camera_id) + (c.place ? " · " + esc(c.place) : "")).addTo(coverageLayer);
    });
  }

  // ---------------------------------------------------------------- query parsing
  function parse(text) {
    var t = text.toLowerCase(), out = { q: text.trim() };
    if (/\bon the right\b|\bright[- ]hand\b|\bright side\b/.test(t)) out.side = "R";
    else if (/\bon the left\b|\bleft[- ]hand\b|\bleft side\b/.test(t)) out.side = "L";
    if (/\bfree\b|\bempty\b|\bspace to park\b/.test(t)) out.state = "free";
    else if (/\boccupied\b|\btaken\b/.test(t)) out.state = "occupied";
    var near = text.match(/\b(?:near|around|by)\s+(.+)$/i) || text.match(/\b(?:on|at)\s+(?!the\s+(?:right|left)\b)(.+)$/i);
    if (near) out.place = near[1].replace(/[?.!]+$/, "").trim();
    var ll = text.match(/(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)/);
    if (ll) out.place = ll[0];
    return out;
  }

  // ---------------------------------------------------------------- run a query
  async function run(opts) {
    opts = opts || {};
    var text = $("q").value.trim();
    var parsed = text ? parse(text) : { q: "" };
    var o = {
      state: $("fState").value || parsed.state || "",
      side: parsed.side || "",
      source: $("fSource").value,
      radius_m: +$("fRadius").value
    };
    var chips = [];
    if (parsed.place && !opts.keepPin) {
      var g = await A.geocode(parsed.place);
      if (g) { setPin(g.lat, g.lon, false); chips.push("near " + g.label); }
      else chips.push('place "' + parsed.place + '" not found; pin unchanged');
    }
    if (state.pin) { o.lat = state.pin[0]; o.lon = state.pin[1]; }
    if (o.state) chips.push("state: " + UI.stateLabel(o.state));
    if (o.side) chips.push("side: " + UI.sideLabel(o.side));
    if (o.source) chips.push("source: " + UI.sourceLabel(o.source));
    if (state.pin) chips.push("within " + o.radius_m + " m of the pin");
    $("understood").innerHTML = chips.map(function (c) { return '<span class="chip">' + esc(c) + "</span>"; }).join("");

    $("counts").innerHTML = '<span class="muted">Loading…</span>';
    var rows, explain;
    try {
      if (text) {
        o.q = text;
        var d = await A.search(o);
        rows = d.results || [];
        explain = d.query_explain || "";
        if (d.embedder) explain += "\nembedder: " + d.embedder;
        if (d.filtered != null) explain += "\n" + d.filtered + " receipts passed the filter, " + rows.length + " shown.";
      } else {
        var s = await A.spaces(o);
        rows = s.spaces || [];
        explain = s.query_explain || "";
      }
    } catch (e) {
      $("counts").innerHTML = '<span class="err">Query failed: ' + esc(e.message) + "</span>";
      return;
    }
    state.q = text;
    state.rows = rows;
    $("howText").textContent = explain || "No query text was returned.";
    drawRows(rows, opts.fit);
    drawCoverage(rows);
  }

  function setPin(lat, lon, runNow) {
    state.pin = [lat, lon];
    queryLayer.clearLayers();
    L.circle(state.pin, { radius: +$("fRadius").value, color: cssVar("--accent"), weight: 2, dashArray: "6 6", fill: false, interactive: false }).addTo(queryLayer);
    L.circleMarker(state.pin, { radius: 7, color: cssVar("--ink"), weight: 3, fillColor: cssVar("--surface"), fillOpacity: 1 }).addTo(queryLayer);
    $("hint").textContent = "Pin at " + lat.toFixed(5) + ", " + lon.toFixed(5) + ". Click elsewhere to move it.";
    if (runNow) run({ keepPin: true });
  }

  // Age is measured against now (live) or the newest receipt on screen (preview / replay).
  function refTime(rows) {
    if (!A.isPreview() && !replay.active) return Date.now();
    return (rows || state.rows).reduce(function (m, s) { var t = Date.parse(s.last_seen); return isNaN(t) ? m : Math.max(m, t); }, 0) || Date.now();
  }
  function shortAge(iso, ref) {
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var m = Math.max(0, Math.round((ref - t) / 60000));
    return m < 60 ? m + "m" : m < 2880 ? Math.round(m / 60) + "h" : Math.round(m / 1440) + "d";
  }
  function fade(iso, ref) {
    var t = Date.parse(iso);
    if (isNaN(t)) return 0.5;
    var min = (ref - t) / 60000;
    return min <= 15 ? 1 : min <= 60 ? 0.85 : min <= 360 ? 0.65 : 0.45;
  }
  function pinIcon(s, selected, ref, pop) {
    var unc = s.position_status === "uncertain";
    var meta = UI.STATES[s.state] || { glyph: "-" };
    var cls = "kpin s-" + s.state + (unc ? " unc" : "") + (selected ? " sel" : "");
    ref = ref || refTime();
    var age = shortAge(s.last_seen, ref);
    return L.divIcon({ className: "", iconSize: [40, 46], iconAnchor: [20, 14],
      html: '<span class="kwrap' + (pop ? " pop" : "") + '" style="opacity:' + (selected ? 1 : fade(s.last_seen, ref)) + '"><span class="' + cls + '">' + meta.glyph + "</span>" +
        (age ? '<span class="kage">' + age + "</span>" : "") + "</span>" });
  }

  function rowTitle(s) {
    return UI.stateLabel(s.state) + (s.state === "not_space" && s.reason ? " (" + s.reason + ")" : "") +
      " · " + (s.street || "Street unknown");
  }

  function drawRows(rows, fit, popIds) {
    pinLayer.clearLayers();
    markers = {};
    var ref = refTime(rows);
    var counts = {}, pts = [];
    rows.forEach(function (s) { counts[s.state] = (counts[s.state] || 0) + 1; });
    var anyPreview = rows.some(function (r) { return r.preview; });
    $("counts").innerHTML = rows.length ?
      ["free", "occupied", "not_space", "cannot_tell"].filter(function (k) { return counts[k]; }).map(function (k) {
        return '<span class="count" title="' + UI.stateLabel(k) + '">' + UI.glyph(k) + counts[k] + " " + UI.stateLabel(k).toLowerCase() + "</span>";
      }).join("") + (anyPreview ? ' <span class="tag-preview">PREVIEW ENTRIES</span>' : "")
      : '<span class="muted">No receipts here. That means not seen, not empty.</span>';

    $("list").innerHTML = "";
    rows.forEach(function (s) {
      var li = document.createElement("li");
      var bits = [];
      if (s.length_m != null) bits.push((+s.length_m).toFixed(1) + " m");
      bits.push(UI.sideLabel(s.side));
      bits.push("seen " + UI.ago(s.last_seen));
      if (s.distance_m != null) bits.push(Math.round(s.distance_m) + " m away");
      bits.push(UI.sourceLabel(s.source).toLowerCase());
      if (s.position_status === "uncertain") bits.push("position uncertain");
      li.innerHTML = '<button class="row" type="button" data-id="' + esc(s.space_id) + '">' +
        UI.glyph(s.state, s.position_status === "uncertain" ? "" : "") +
        '<span><span class="t">' + esc(rowTitle(s)) + (s.preview ? ' <span class="tag-preview">PREVIEW</span>' : "") + "</span><br>" +
        '<span class="m">' + esc(bits.join(" · ")) + "</span></span></button>";
      li.firstChild.addEventListener("click", function () { openReceipt(s.space_id); });
      $("list").appendChild(li);
      if (s.lat != null && s.lon != null) {
        var m = L.marker([s.lat, s.lon], { icon: pinIcon(s, false, ref, popIds && popIds[s.space_id]), title: rowTitle(s), riseOnHover: true })
          .on("click", function () { openReceipt(s.space_id); })
          .bindTooltip(esc(rowTitle(s)) + " · seen " + esc(UI.ago(s.last_seen)) + (s.position_status === "uncertain" ? " · position uncertain" : ""))
          .addTo(pinLayer);
        markers[s.space_id] = { m: m, s: s };
        pts.push([s.lat, s.lon]);
      }
    });
    if (fit && pts.length) { map.invalidateSize(); map.fitBounds(pts, { padding: [40, 40], maxZoom: 18 }); }
  }

  function select(id) {
    if (state.selected && markers[state.selected]) markers[state.selected].m.setIcon(pinIcon(markers[state.selected].s, false));
    state.selected = id;
    if (id && markers[id]) {
      markers[id].m.setIcon(pinIcon(markers[id].s, true));
      if (!map.getBounds().pad(-0.1).contains(markers[id].m.getLatLng())) map.panTo(markers[id].m.getLatLng());
    }
  }

  // ---------------------------------------------------------------- receipt
  function ruler(len) {
    if (len == null) return '<span class="muted">not measured</span>';
    var scale = Math.max(12, Math.ceil(len / 2) * 2 + 2);
    var pct = Math.min(100, (len / scale) * 100);
    return '<div class="ruler" role="img" aria-label="' + len.toFixed(1) + ' metres of kerb; one car is 5.5 metres">' +
      '<div class="track" style="--m:' + (100 / scale) + '%">' +
      '<div class="fill" style="width:' + pct + '%"></div><div class="ticks"></div>' +
      '<div class="car" style="width:' + (5.5 / scale * 100) + '%" title="one car length, 5.5 m"></div></div>' +
      '<div class="legend"><span>0</span><span>┆ one car 5.5 m</span><span>' + scale + " m</span></div></div>";
  }

  function stepDetail(step) {
    var d = A.parseDetail(step);
    var keys = Object.keys(d).filter(function (k) { return k !== "frame_size"; });
    return keys.slice(0, 4).map(function (k) {
      var v = d[k];
      if (Array.isArray(v)) v = "[" + v.join(", ") + "]";
      else if (v && typeof v === "object") v = JSON.stringify(v);
      return k.replace(/_/g, " ") + ": <b>" + esc(v) + "</b>";
    }).join(" · ");
  }

  async function openReceipt(id) {
    select(id);
    document.querySelectorAll(".row").forEach(function (r) { r.classList.toggle("sel", r.getAttribute("data-id") === id); });
    var box = $("receipt");
    box.hidden = false; $("results").hidden = true; $("find").hidden = true;
    box.innerHTML = '<p class="muted">Opening receipt…</p>';
    var d;
    try { d = await A.receipt(id, state.q || undefined); }
    catch (e) { box.innerHTML = backBtn() + '<p class="err">Could not open receipt: ' + esc(e.message) + "</p>"; wireBack(); return; }
    renderReceipt(d);
    $("receipt").scrollTop = 0;
    document.querySelector(".panel").scrollTop = 0;
  }

  function backBtn() { return '<button class="btn ghost back" id="back" type="button">← Results</button>'; }
  function wireBack() {
    $("back").addEventListener("click", function () {
      $("receipt").hidden = true; $("results").hidden = false; $("find").hidden = false;
      select(null);
    });
  }

  function renderReceipt(d) {
    var s = d.space || {};
    var unc = s.position_status === "uncertain" || s.lat == null;
    var overruled = d.model_state && d.model_state !== s.state;
    var conf = s.confidence != null ? Math.max(0, Math.min(1, +s.confidence)) : null;
    var trail = d.trail || [];
    var savedName = A.store("kerb.name") || "";
    var start = d.evidence_start != null ? d.evidence_start : null, end = d.evidence_end != null ? d.evidence_end : null;

    var html = backBtn() +
      '<div class="badge s-' + esc(s.state) + '">' + '<span class="glyph">' + (UI.STATES[s.state] || { glyph: "–" }).glyph + "</span>" +
      '<div><div class="big">' + esc(UI.stateLabel(s.state)) + "</div>" +
      '<div class="sub">' + esc(s.state === "not_space" && s.reason ? "Reason: " + s.reason : (s.reason || UI.sourceLabel(s.source))) + "</div></div></div>" +
      (s.preview ? '<p><span class="tag-preview">PREVIEW ENTRY · NOT A RESULT</span></p>' : "") +
      (overruled ? '<p class="muted">Model said <b>' + esc(UI.stateLabel(d.model_state)) + "</b>; a person overruled it.</p>" : "") +
      '<p class="receipt-text">' + esc(s.receipt_text || "No receipt text.") + "</p>" +
      '<dl class="facts">' +
      "<dt>Street</dt><dd>" + esc(s.street || "Street unknown") + " · " + esc(UI.sideLabel(s.side)) + "</dd>" +
      "<dt>Position</dt><dd>" + (unc ? "Position uncertain" : (+s.lat).toFixed(5) + ", " + (+s.lon).toFixed(5)) + "</dd>" +
      "<dt>Last seen</dt><dd>" + esc(UI.ago(s.last_seen)) + ' <span class="muted mono">' + esc(s.last_seen || "") + "</span></dd>" +
      "<dt>Confidence</dt><dd>" + (conf == null ? '<span class="muted">not given</span>' :
        '<span class="conf"><span class="bar"><i style="width:' + conf * 100 + '%"></i></span>' + conf.toFixed(2) + "</span>") + "</dd>" +
      "<dt>Source</dt><dd>" + esc(UI.sourceLabel(s.source)) + (s.camera_id ? ' <span class="muted mono">' + esc(s.camera_id) + "</span>" : "") + "</dd>" +
      "<dt>Length</dt><dd>" + (s.length_m != null ? (+s.length_m).toFixed(1) + " m" : "") + ruler(s.length_m != null ? +s.length_m : null) + "</dd>" +
      "</dl>" +
      '<h2 class="sec">Evidence</h2><div class="media" id="ev"></div>' +
      '<p class="media-cap">' + (s.original_video ? esc(s.original_video) : "") + (s.t_sec != null ? " · t = " + (+s.t_sec).toFixed(1) + " s" : "") + "</p>" +
      '<h2 class="sec">Decision trail</h2>' +
      (trail.length ? '<ol class="trail">' + trail.map(function (t, i) {
        return '<li class="' + (t.step === "overrule" ? "ovr" : "") + '"><span class="n">' + (t.step_no || i + 1) + "</span>" +
          '<div class="st">' + esc(STEP_NAMES[t.step] || t.step) + "</div>" +
          '<div class="kv">' + stepDetail(t) + "</div></li>";
      }).join("") + "</ol>" : '<p class="muted">No trail steps recorded.</p>') +
      '<h2 class="sec">Overrule</h2>' +
      '<form class="ovr-form" id="ovr">' +
      '<div class="seg" role="radiogroup" aria-label="Correct state">' +
      ["free", "occupied", "not_space", "cannot_tell"].map(function (k) {
        return '<label><input type="radio" name="ns" value="' + k + '"' + (k === s.state ? " checked" : "") + ">" + UI.glyph(k) + UI.stateLabel(k) + "</label>";
      }).join("") + "</div>" +
      '<label class="field">Note<input id="ovNote" maxlength="300" placeholder="What the footage shows"></label>' +
      '<label class="field">Your name<input id="ovBy" maxlength="60" value="' + esc(savedName) + '"></label>' +
      '<button class="btn" type="submit">Record overrule</button>' +
      '<div id="ovOut"></div></form>' +
      ((d.overrules || []).length ? '<h2 class="sec">Earlier overrules</h2><ul>' + d.overrules.map(function (o) {
        return "<li>" + esc(UI.ago(o.at)) + " · <b>" + esc(o.by) + "</b> → " + esc(UI.stateLabel(o.new_state)) + (o.note ? " · " + esc(o.note) : "") + "</li>";
      }).join("") + "</ul>" : "") +
      '<details class="how"><summary>How this was found</summary><pre class="mono">' +
      esc((d.query_explain ? "Search: " + d.query_explain.q + "\n" + (d.query_explain.how || "") + (d.query_explain.score != null ? "\nscore " + d.query_explain.score : "") + "\n\n" : "") +
        "Receipt: GET api/receipt/" + s.space_id + "\n" + $("howText").textContent) + "</pre></details>";

    $("receipt").innerHTML = html;
    wireBack();
    UI.media($("ev"), d.evidence_uri || s.evidence_uri, d.mark, start, end);

    $("ovr").addEventListener("submit", async function (e) {
      e.preventDefault();
      var ns = (document.querySelector('input[name="ns"]:checked') || {}).value;
      var by = $("ovBy").value.trim();
      var out = $("ovOut");
      if (!by) { out.innerHTML = '<p class="err">Add your name.</p>'; $("ovBy").focus(); return; }
      if (ns === s.state) { out.innerHTML = '<p class="err">Pick a different state to overrule.</p>'; return; }
      A.store("kerb.name", by);
      var btn = e.target.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        var r = await A.overrule({ space_id: s.space_id, new_state: ns, note: $("ovNote").value.trim(), by: by });
        var fresh = await A.receipt(s.space_id, state.q || undefined);
        renderReceipt(fresh);
        $("ovOut").innerHTML = '<div class="beforeafter"><span class="pill">Before ' + UI.glyph(r.before.state) + esc(UI.stateLabel(r.before.state)) +
          '</span>→<span class="pill">After ' + UI.glyph(r.after.state) + esc(UI.stateLabel(r.after.state)) + "</span>" +
          (r.preview ? '<span class="tag-preview">PREVIEW · NOT SAVED</span>' : "") + "</div>";
        $("ovOut").scrollIntoView({ block: "nearest" });
        refreshRowsQuietly();
      } catch (err) {
        out.innerHTML = '<p class="err">Not saved: ' + esc(err.message) + "</p>";
        btn.disabled = false;
      }
    });
  }

  async function refreshRowsQuietly() {
    var keep = state.selected;
    await run({ keepPin: true });
    if (keep) select(keep);
  }

  // ---------------------------------------------------------------- wiring
  map.on("click", function (e) { setPin(e.latlng.lat, e.latlng.lng, true); });
  $("searchForm").addEventListener("submit", function (e) { e.preventDefault(); run({ fit: true }); });
  ["fState", "fSource"].forEach(function (id) { $(id).addEventListener("change", function () { run({ keepPin: true }); }); });
  $("fRadius").addEventListener("change", function () { if (state.pin) setPin(state.pin[0], state.pin[1], true); });

  // ---------------------------------------------------------------- destination
  $("destForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    var place = $("dest").value.trim();
    if (!place) return;
    var g = await A.geocode(place);
    if (!g) { $("understood").innerHTML = '<span class="chip">"' + esc(place) + '" not found</span>'; return; }
    map.setView([g.lat, g.lon], 18);
    $("q").value = "";
    setPin(g.lat, g.lon, true);
  });

  // ---------------------------------------------------------------- live chip
  async function paintLive() {
    var d = await A.live(state.rows);
    var n = d && d.newest_clip;
    var chip = $("live");
    chip.classList.toggle("on", !!(n && !d.derived && !d.preview));
    if (!n) { $("liveText").textContent = "No clips yet"; return; }
    var cap = n.captured_at ? new Date(n.captured_at) : null;
    var hhmm = cap && !isNaN(cap) ? cap.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "?";
    var arrived = n.arrived_at ? UI.ago(n.arrived_at) : UI.ago(n.captured_at);
    $("liveText").textContent = (d.derived || d.preview ? "Newest " : "Live · newest ") + hhmm + " · " + arrived;
    chip.title = d.derived || d.preview ? "Newest receipt on screen (no live feed status)" : "Newest clip captured " + n.captured_at + (n.arrived_at ? ", arrived " + n.arrived_at : "");
  }
  setInterval(function () { if (!replay.active) paintLive(); }, 30000);

  // ---------------------------------------------------------------- replay a drive
  async function fillDrives() {
    var drives = await A.replayDrives(state.rows);
    $("rDrive").innerHTML = drives.length ? drives.map(function (d) {
      return '<option value="' + esc(d.drive_id) + '">' + esc(d.label || d.drive_id) + (d.clips ? " · " + d.clips + " clips" : "") + "</option>";
    }).join("") : '<option value="">No drives</option>';
    $("rBtn").disabled = !drives.length;
  }
  function speed() { return +((document.querySelector('input[name="rSpeed"]:checked') || {}).value || 1); }
  async function replayStart() {
    var drive = $("rDrive").value;
    if (!drive) return;
    try {
      var r = await A.replayStart(drive, speed());
      replay = { active: true, id: r.replay_id, timer: null, seen: {} };
    } catch (e) { $("understood").innerHTML = '<span class="chip">Replay failed: ' + esc(e.message) + "</span>"; return; }
    $("rBtn").textContent = "Stop";
    $("understood").innerHTML = ""; $("q").value = "";
    $("replayFlag").hidden = false;
    $("replayProg").textContent = "starting…";
    $("receipt").hidden = true; $("results").hidden = false; $("find").hidden = false;
    pinLayer.clearLayers(); coverageLayer.clearLayers(); queryLayer.clearLayers(); state.pin = null;
    replayPoll(true);
  }
  async function replayPoll(first) {
    if (!replay.active) return;
    var st;
    try { st = await A.replayStatus(replay.id); } catch (e) { st = null; }
    if (!replay.active) return;
    if (st) {
      var rows = st.spaces || [], pop = {}, fresh = 0;
      rows.forEach(function (s) { if (!replay.seen[s.space_id]) { replay.seen[s.space_id] = 1; pop[s.space_id] = 1; fresh++; } });
      if (fresh || first) {
        state.rows = rows;
        drawRows(rows, false, pop);
        var pts = rows.filter(function (s) { return s.lat != null; }).map(function (s) { return [s.lat, s.lon]; });
        if (pts.length) map.panTo(pts[pts.length - 1]);
        $("howText").textContent = "Replay " + (st.drive_id || "") + " at " + (st.speed || speed()) + "×: receipts written as each clip is processed.";
      }
      $("replayProg").textContent = (st.clips_done != null ? st.clips_done + "/" + (st.clips_total || "?") + " clips" : "") +
        (st.clip_time ? " · " + new Date(st.clip_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
      if (st.clip_time) $("liveText").textContent = "Replay · " + new Date(st.clip_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (st.active === false) { replayEnd(true); return; }
    }
    replay.timer = setTimeout(replayPoll, 1000);
  }
  function replayEnd(finished) {
    clearTimeout(replay.timer);
    replay.active = false;
    $("rBtn").textContent = "Start";
    $("replayProg").textContent = finished ? "done" : "stopped";
    setTimeout(function () { if (!replay.active) $("replayFlag").hidden = true; }, finished ? 4000 : 0);
    paintLive();
  }
  $("rBtn").addEventListener("click", async function () {
    if (replay.active) { var id = replay.id; replayEnd(false); await A.replayStop(id); run({ fit: true }); }
    else replayStart();
  });

  A.init().then(async function () { await run({ fit: true }); paintLive(); fillDrives(); }).catch(function (e) {
    $("counts").innerHTML = '<span class="err">Could not start: ' + esc(e.message) + "</span>";
  });
})();
