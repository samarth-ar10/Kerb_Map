/* Kerb Map: Find parking. Nearest free kerb first; tap a spot for a simple card. Data via KerbAPI (api.js). */
(function () {
  "use strict";
  var A = window.KerbAPI, UI = window.KerbUI, esc = A.esc;
  var $ = function (id) { return document.getElementById(id); };
  var CAR = 5.5;          // metres per car
  var RADIUS = 800;       // metres around the centre
  var STEP_NAMES = {
    gate: "Picked a frame", boxes: "Found cars in the frame", geometry: "Measured the kerb gap", rule: "Checked street rules",
    cosmos: "Checked the unclear gap (Cosmos)", track: "Watched over time", position: "Placed on the map", overrule: "Corrected by a person"
  };

  UI.theme();
  var map = L.map("map", { zoomControl: false, zoomSnap: 0.25 }).setView([43.6532, -79.4017], 17);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, className: "basemap", attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);
  var spotLayer = L.layerGroup().addTo(map);
  var hereLayer = L.layerGroup().addTo(map);
  var markers = {};
  var S = { centre: null, rows: [], selected: null, label: "" };

  function dist(a, b, c, d) {
    var R = 6371000, r = Math.PI / 180, x = Math.sin((c - a) * r / 2), y = Math.sin((d - b) * r / 2);
    return 2 * R * Math.asin(Math.sqrt(x * x + Math.cos(a * r) * Math.cos(c * r) * y * y));
  }
  function fmtDist(m) { return m == null ? "" : m < 20 ? "< 20 m" : m < 1000 ? Math.round(m / 10) * 10 + " m" : (m / 1000).toFixed(1) + " km"; }
  function fits(len) {
    if (len == null) return "Size not measured";
    var n = Math.floor(len / CAR);
    return n >= 1 ? "Fits " + n + (n === 1 ? " car" : " cars") : "Short gap, " + (+len).toFixed(1) + " m";
  }
  function seen(iso) { return "Seen " + UI.ago(iso); }
  function ageMin(iso) { var t = Date.parse(iso); return isNaN(t) ? null : (Date.now() - t) / 60000; }
  function fade(iso) {
    var m = ageMin(iso);
    if (m == null) return 0.55;
    return m <= 15 ? 1 : m <= 60 ? 0.85 : m <= 360 ? 0.7 : 0.55;
  }
  function shortAge(iso) {
    var m = ageMin(iso);
    if (m == null) return "";
    m = Math.max(0, Math.round(m));
    return m < 60 ? m + "m" : m < 2880 ? Math.round(m / 60) + "h" : Math.round(m / 1440) + "d";
  }
  function visible(s) {
    if (s.state === "free") return true;
    if (s.state === "occupied") return $("showOcc").checked;
    return $("showOther").checked;
  }
  var CAR_SVG = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M5 11l2-5h10l2 5h1v6h-2v2h-3v-2H9v2H6v-2H4v-6z" fill="currentColor"/><circle cx="7.5" cy="14" r="1.3" fill="var(--occupied)"/><circle cx="16.5" cy="14" r="1.3" fill="var(--occupied)"/></svg>';
  function glyph(state) {
    return state === "free" ? "P" : state === "occupied" ? CAR_SVG : state === "not_space" ? "⊘" : "?";
  }
  function label(state) {
    return state === "free" ? "Free" : state === "occupied" ? "Taken" : state === "not_space" ? "Not a space" : state === "cannot_tell" ? "Can't tell" : "Unknown";
  }
  function icon(s, selected) {
    var cls = "bigpin s-" + s.state + (s.position_status === "uncertain" ? " unc" : "") + (selected ? " sel" : "");
    var age = shortAge(s.last_seen);
    return L.divIcon({ className: "", iconSize: [52, 62], iconAnchor: [26, 22],
      html: '<span class="bigwrap" style="opacity:' + (selected ? 1 : fade(s.last_seen)) + '"><span class="' + cls + '">' + glyph(s.state) + "</span>" +
        (age ? '<span class="kage">' + age + "</span>" : "") + "</span>" });
  }

  // ------------------------------------------------------------ load + draw
  async function load(lat, lon, label, zoom) {
    S.centre = [lat, lon];
    S.label = label || "";
    hereLayer.clearLayers();
    L.circleMarker(S.centre, { radius: 9, color: "#fff", weight: 3, fillColor: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(), fillOpacity: 1, interactive: false }).addTo(hereLayer);
    if (zoom) map.setView(S.centre, zoom);
    $("plist").innerHTML = '<li class="muted">Looking…</li>';
    var d;
    try { d = await A.spaces({ lat: lat, lon: lon, radius_m: RADIUS, limit: 500 }); }
    catch (e) { $("plist").innerHTML = '<li class="err">Could not load: ' + esc(e.message) + "</li>"; return; }
    S.rows = (d.spaces || []).filter(function (s) { return s.lat != null && s.lon != null; });
    S.rows.forEach(function (s) { s._d = dist(lat, lon, s.lat, s.lon); });
    var area = $("area");
    area.hidden = !S.label;
    area.textContent = S.label;
    draw();
  }

  function draw() {
    spotLayer.clearLayers();
    markers = {};
    var rows = S.rows.filter(visible);
    rows.forEach(function (s) {
      var m = L.marker([s.lat, s.lon], { icon: icon(s, s.space_id === S.selected), title: label(s.state) + " · " + (s.street || ""), riseOnHover: true,
        zIndexOffset: s.state === "free" ? 500 : 0 })
        .on("click", function () { openSpot(s.space_id); }).addTo(spotLayer);
      markers[s.space_id] = { m: m, s: s };
    });
    var free = S.rows.filter(function (s) { return s.state === "free"; }).sort(function (a, b) { return a._d - b._d; });
    var rest = rows.filter(function (s) { return s.state !== "free"; }).sort(function (a, b) { return a._d - b._d; });
    $("nfree").textContent = free.length ? free.length + " free" : "";
    var html = free.map(row).join("");
    if (!free.length) html = '<li class="empty"><b>No free spots seen near here yet.</b></li>';
    if (rest.length) html += '<li class="sub">Also nearby</li>' + rest.slice(0, 12).map(row).join("");
    $("plist").innerHTML = html;
    $("plist").querySelectorAll("button[data-id]").forEach(function (b) {
      b.addEventListener("click", function () { openSpot(b.getAttribute("data-id")); });
    });
  }

  function row(s) {
    return '<li><button type="button" class="prow' + (s.state === "free" ? "" : " dim") + '" data-id="' + esc(s.space_id) + '">' +
      '<span class="pg s-' + esc(s.state) + '">' + glyph(s.state) + "</span>" +
      '<span class="pmain"><b>' + esc(fmtDist(s._d)) + " · " + esc(s.street || "Street unknown") + "</b>" +
      '<span class="pmeta">' + esc(label(s.state)) + " · " + esc(seen(s.last_seen).toLowerCase()) + (s.state === "free" ? " · " + esc(fits(s.length_m).toLowerCase()) : "") +
      (s.preview ? ' <span class="tag-preview">PREVIEW</span>' : "") + "</span></span></button></li>";
  }

  // ------------------------------------------------------------ spot card
  function select(id) {
    if (S.selected && markers[S.selected]) markers[S.selected].m.setIcon(icon(markers[S.selected].s, false));
    S.selected = id;
    if (id && markers[id]) { markers[id].m.setIcon(icon(markers[id].s, true)); map.panTo(markers[id].m.getLatLng()); }
  }

  async function openSpot(id) {
    select(id);
    var s = S.rows.find(function (r) { return r.space_id === id; }) || {};
    $("listView").hidden = true;
    var card = $("cardView");
    card.hidden = false;
    card.innerHTML = cardHtml(s, null);
    wireCard(s, null);
    $("sheet").scrollTop = 0;
    $("sheet").classList.add("open");
    try {
      var d = await A.receipt(id);
      if (S.selected !== id) return;
      card.innerHTML = cardHtml(Object.assign({}, s, d.space || {}), d);
      wireCard(Object.assign({}, s, d.space || {}), d);
    } catch (e) { /* keep the short card */ }
  }

  function cardHtml(s, d) {
    var trail = (d && d.trail) || [];
    var conf = s.confidence != null ? Math.round(Math.max(0, Math.min(1, +s.confidence)) * 100) : null;
    return '<button class="close" id="closeCard" type="button" aria-label="Back to list">×</button>' +
      '<div class="status s-' + esc(s.state) + '"><span class="sg">' + glyph(s.state) + "</span>" + esc(label(s.state)) + "</div>" +
      '<h2 class="cstreet">' + esc(s.street || "Street unknown") + (s.side === "L" ? " · left side" : s.side === "R" ? " · right side" : "") + "</h2>" +
      (s.preview ? '<p><span class="tag-preview">PREVIEW ENTRY · NOT A RESULT</span></p>' : "") +
      '<div class="facts3"><div><b>' + esc(fits(s.length_m)) + "</b></div><div><b>" + esc(seen(s.last_seen)) + "</b></div>" +
      (s._d != null ? "<div><b>" + esc(fmtDist(s._d)) + " away</b></div>" : "") + "</div>" +
      (s.position_status === "uncertain" ? '<p class="muted">Position is approximate.</p>' : "") +
      '<div class="media" id="cardMedia"></div>' +
      '<div class="fb" id="fb"><button type="button" class="fbb" data-v="up">👍 Looks right</button><button type="button" class="fbb" data-v="down">👎 Not right</button></div>' +
      '<div class="fbreasons" id="fbReasons" hidden>' + A.FEEDBACK_REASONS.map(function (r) {
        return '<button type="button" class="fbr" data-r="' + r[0] + '">' + esc(r[1]) + "</button>";
      }).join("") + "</div>" +
      '<p class="fbdone" id="fbDone" hidden></p>' +
      '<details class="why"><summary>Why we think so</summary>' +
      (s.receipt_text ? "<p>" + esc(s.receipt_text) + "</p>" : "") +
      '<p class="muted">' + (conf != null ? "Confidence " + conf + "% · " : "") + esc(UI.sourceLabel(s.source)) + (s.last_seen ? " · " + esc(s.last_seen) : "") + "</p>" +
      (trail.length ? '<ol class="trail">' + trail.map(function (t, i) {
        var det = A.parseDetail(t), keys = Object.keys(det).filter(function (k) { return k !== "frame_size"; }).slice(0, 3);
        return '<li class="' + (t.step === "overrule" ? "ovr" : "") + '"><span class="n">' + (t.step_no || i + 1) + '</span><div class="st">' + esc(STEP_NAMES[t.step] || t.step) + "</div>" +
          '<div class="kv">' + keys.map(function (k) { var v = det[k]; return esc(k.replace(/_/g, " ")) + ": <b>" + esc(Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v) + "</b>"; }).join(" · ") + "</div></li>";
      }).join("") + "</ol>" : (d ? '<p class="muted">No steps recorded.</p>' : '<p class="muted">Loading…</p>')) +
      "</details>";
  }

  function wireCard(s, d) {
    $("closeCard").addEventListener("click", closeCard);
    UI.media($("cardMedia"), (d && d.evidence_uri) || s.evidence_uri, d && d.mark, d && d.evidence_start, d && d.evidence_end, { autoplay: true });
    $("fb").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-v]");
      if (!b) return;
      if (b.getAttribute("data-v") === "up") send(s, "up");
      else { $("fbReasons").hidden = false; $("fb").hidden = true; }
    });
    $("fbReasons").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-r]");
      if (b) send(s, "down", b.getAttribute("data-r"));
    });
  }

  async function send(s, vote, reason) {
    $("fb").hidden = true; $("fbReasons").hidden = true;
    var done = $("fbDone");
    done.hidden = false; done.textContent = "Sending…";
    try {
      var r = await A.feedback({ space_id: s.space_id, vote: vote, reason: reason });
      done.textContent = "Thanks, noted." + (r && r.preview ? " (preview, not saved)" : "");
    } catch (e) {
      done.textContent = "Not sent: " + e.message;
      $("fb").hidden = false;
    }
  }

  function closeCard() {
    $("cardView").hidden = true;
    $("cardView").innerHTML = "";
    $("listView").hidden = false;
    select(null);
  }

  // ------------------------------------------------------------ controls
  $("destForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    var place = $("dest").value.trim();
    if (!place) return;
    var g = await A.geocode(place);
    if (!g) { $("area").hidden = false; $("area").textContent = "Couldn't find “" + place + "”"; return; }
    closeCard();
    load(g.lat, g.lon, "Near " + g.label.replace(/ \(from receipts\)$/, ""), 17.5);
  });
  $("nearMe").addEventListener("click", function () {
    if (!navigator.geolocation) { $("area").hidden = false; $("area").textContent = "Location not available"; return; }
    $("area").hidden = false; $("area").textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition(function (p) {
      closeCard();
      load(p.coords.latitude, p.coords.longitude, "Near you", 17.5);
    }, function () { $("area").textContent = "Location not shared"; }, { timeout: 8000, maximumAge: 60000 });
  });
  $("filterBtn").addEventListener("click", function () {
    var pop = $("filterPop"), open = pop.hidden;
    pop.hidden = !open;
    this.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", function (e) {
    if (!e.target.closest(".fmenu")) { $("filterPop").hidden = true; $("filterBtn").setAttribute("aria-expanded", "false"); }
  });
  ["showOcc", "showOther"].forEach(function (id) { $(id).addEventListener("change", draw); });
  map.on("click", function () { if (!$("cardView").hidden) closeCard(); });

  // ------------------------------------------------------------ start: URL pin, else the area with data
  A.init().then(async function () {
    var qs = new URLSearchParams(location.search);
    if (qs.get("lat") && qs.get("lon")) { load(+qs.get("lat"), +qs.get("lon"), "", 17.5); return; }
    var all = [];
    try { all = ((await A.spaces({ limit: 5000 })).spaces || []).filter(function (s) { return s.lat != null && s.lon != null; }); } catch (e) { all = []; }
    if (!all.length) { load(43.6532, -79.4017, "", 17); return; }
    var free = all.filter(function (s) { return s.state === "free"; });
    var pool = free.length ? free : all;
    var lat = pool.reduce(function (a, s) { return a + s.lat; }, 0) / pool.length;
    var lon = pool.reduce(function (a, s) { return a + s.lon; }, 0) / pool.length;
    map.invalidateSize();
    map.fitBounds(all.map(function (s) { return [s.lat, s.lon]; }), { padding: [50, 50], maxZoom: 18 });
    load(lat, lon, "Demo area" + (pool[0].street ? " · near " + pool[0].street : ""));
  });
})();
