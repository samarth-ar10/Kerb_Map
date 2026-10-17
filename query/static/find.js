/* Kerb Map: Find parking. Mobile first: full-screen map, compact top bar, draggable bottom sheet
   (peek / half / full). From 900 px wide the sheet becomes a left side panel. Data via KerbAPI (api.js). */
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
  var wide = window.matchMedia("(min-width: 900px)");

  UI.theme();
  var map = L.map("map", {
    zoomControl: false, zoomSnap: 0.25, tap: true, attributionControl: false,
    scrollWheelZoom: wide.matches
  }).setView([43.6532, -79.4017], 17);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.control.attribution({ position: wide.matches ? "bottomright" : "topright", prefix: false }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, className: "basemap", attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);
  var spotLayer = L.layerGroup().addTo(map);
  var hereLayer = L.layerGroup().addTo(map);
  var markers = {};
  var S = { centre: null, rows: [], selected: null, label: "" };

  // ------------------------------------------------------------ bottom sheet (mobile)
  var sheet = $("sheet");
  var snapName = "peek";
  function topH() { return $("ftop").getBoundingClientRect().bottom; }
  function snaps() {
    var vh = window.innerHeight;
    var peek = Math.min(200, Math.round(vh * 0.3));
    return { peek: peek, half: Math.round(vh * 0.52), full: Math.max(peek + 40, Math.round(vh - topH() - 6)) };
  }
  function syncTop() { document.documentElement.style.setProperty("--ftop-h", Math.round(topH() + 8) + "px"); }
  function setSnap(name, instant) {
    syncTop();
    snapName = name;
    sheet.setAttribute("data-snap", name);
    if (wide.matches) { sheet.style.height = ""; return; }
    if (instant) sheet.classList.add("nodrag");
    sheet.style.height = snaps()[name] + "px";
    if (instant) setTimeout(function () { sheet.classList.remove("nodrag"); }, 30);
    document.documentElement.style.setProperty("--sheet-h", snaps()[name] + "px");
  }
  (function drag() {
    var grab = $("grab"), y0 = 0, h0 = 0, moved = false, active = false;
    grab.addEventListener("pointerdown", function (e) {
      if (wide.matches) return;
      active = true; moved = false; y0 = e.clientY; h0 = sheet.getBoundingClientRect().height;
      sheet.classList.add("dragging");
      grab.setPointerCapture(e.pointerId);
    });
    grab.addEventListener("pointermove", function (e) {
      if (!active) return;
      var dy = y0 - e.clientY;
      if (Math.abs(dy) > 4) moved = true;
      var s = snaps();
      sheet.style.height = Math.max(s.peek - 40, Math.min(s.full, h0 + dy)) + "px";
    });
    function end(e) {
      if (!active) return;
      active = false;
      sheet.classList.remove("dragging");
      if (!moved) { setSnap(snapName === "peek" ? "half" : snapName === "half" ? "full" : "half"); return; }
      var h = sheet.getBoundingClientRect().height, s = snaps(), best = "peek", bd = 1e9;
      Object.keys(s).forEach(function (k) { var d = Math.abs(s[k] - h); if (d < bd) { bd = d; best = k; } });
      setSnap(best);
    }
    grab.addEventListener("pointerup", end);
    grab.addEventListener("pointercancel", end);
  })();
  function resnap() { setSnap(!wide.matches && !$("cardView").hidden ? "full" : snapName, true); }
  window.addEventListener("resize", function () { resnap(); map.invalidateSize(); });
  wide.addEventListener && wide.addEventListener("change", function () {
    if (wide.matches) map.scrollWheelZoom.enable(); else map.scrollWheelZoom.disable();
    resnap(); setTimeout(function () { map.invalidateSize(); }, 50);
  });

  // ------------------------------------------------------------ helpers
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
    if (m == null) return 0.6;
    return m <= 15 ? 1 : m <= 60 ? 0.88 : m <= 360 ? 0.75 : 0.6;
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
    return L.divIcon({ className: "", iconSize: [52, 64], iconAnchor: [26, 24],
      html: '<span class="bigwrap" style="opacity:' + (selected ? 1 : fade(s.last_seen)) + '"><span class="' + cls + '">' + glyph(s.state) + "</span>" +
        (age ? '<span class="kage">' + age + "</span>" : "") + "</span>" });
  }
  // Keep a point in the part of the map the sheet does not cover.
  function focus(latlng, zoom) {
    if (zoom) map.setView(latlng, zoom, { animate: false });
    if (wide.matches) { map.panTo(latlng); return; }
    var size = map.getSize(), top = topH(), covered = Math.min(snaps().half, sheet.getBoundingClientRect().height);
    var targetY = top + (size.y - top - covered) / 2;
    var p = map.latLngToContainerPoint(latlng);
    map.panBy([p.x - size.x / 2, p.y - targetY], { animate: true });
  }

  // ------------------------------------------------------------ load + draw
  async function load(lat, lon, areaLabel, zoom) {
    S.centre = [lat, lon];
    S.label = areaLabel || "";
    hereLayer.clearLayers();
    L.circleMarker(S.centre, { radius: 9, color: "#fff", weight: 3, fillColor: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(), fillOpacity: 1, interactive: false }).addTo(hereLayer);
    if (zoom) focus(L.latLng(lat, lon), zoom);
    $("sheetH").textContent = "Looking for spaces…";
    $("plist").innerHTML = "";
    var d;
    try { d = await A.spaces({ lat: lat, lon: lon, radius_m: RADIUS, limit: 500 }); }
    catch (e) { $("sheetH").textContent = "Could not load spaces"; $("plist").innerHTML = '<li class="err">' + esc(e.message) + "</li>"; return; }
    S.rows = (d.spaces || []).filter(function (s) { return s.lat != null && s.lon != null; });
    S.rows.forEach(function (s) { s._d = dist(lat, lon, s.lat, s.lon); });
    $("area").hidden = !S.label;
    $("area").textContent = S.label;
    draw();
  }

  function draw() {
    spotLayer.clearLayers();
    markers = {};
    var rows = S.rows.filter(visible);
    rows.forEach(function (s) {
      var m = L.marker([s.lat, s.lon], { icon: icon(s, s.space_id === S.selected), title: label(s.state) + " · " + (s.street || ""),
        keyboard: true, zIndexOffset: s.state === "free" ? 500 : 0 })
        .on("click", function () { openSpot(s.space_id); }).addTo(spotLayer);
      markers[s.space_id] = { m: m, s: s };
    });
    var free = S.rows.filter(function (s) { return s.state === "free"; }).sort(function (a, b) { return a._d - b._d; });
    var rest = rows.filter(function (s) { return s.state !== "free"; }).sort(function (a, b) { return a._d - b._d; });
    $("sheetH").innerHTML = free.length
      ? '<span class="nfree">' + free.length + "</span> free " + (free.length === 1 ? "space" : "spaces") + " nearby"
      : "No free spaces seen nearby";
    var html = free.map(row).join("");
    if (rest.length) html += '<li class="sub">Also nearby</li>' + rest.slice(0, 12).map(row).join("");
    $("plist").innerHTML = html;
    $("plist").querySelectorAll("button[data-id]").forEach(function (b) {
      b.addEventListener("click", function () { openSpot(b.getAttribute("data-id")); });
    });
    var n = ($("showOcc").checked ? 1 : 0) + ($("showOther").checked ? 1 : 0);
    $("filterLabel").textContent = n === 0 ? "Free only" : n === 1 && $("showOcc").checked ? "Free & taken" : n === 1 ? "Free & other" : "All kerb";
  }

  function row(s) {
    return '<li><button type="button" class="prow' + (s.state === "free" ? "" : " dim") + '" data-id="' + esc(s.space_id) + '">' +
      '<span class="pg s-' + esc(s.state) + '">' + glyph(s.state) + "</span>" +
      '<span class="pmain"><b>' + esc(fmtDist(s._d)) + " · " + esc(s.street || "Street unknown") + "</b>" +
      '<span class="pmeta">' + esc(label(s.state)) + " · " + esc(seen(s.last_seen).toLowerCase()) + (s.state === "free" ? " · " + esc(fits(s.length_m).toLowerCase()) : "") +
      (s.preview ? ' <span class="tag-preview">PREVIEW</span>' : "") + "</span></span>" +
      '<span class="chev" aria-hidden="true">›</span></button></li>';
  }

  // ------------------------------------------------------------ spot card
  function select(id) {
    if (S.selected && markers[S.selected]) markers[S.selected].m.setIcon(icon(markers[S.selected].s, false));
    S.selected = id;
    if (id && markers[id]) markers[id].m.setIcon(icon(markers[id].s, true));
  }

  async function openSpot(id) {
    select(id);
    var s = S.rows.find(function (r) { return r.space_id === id; }) || {};
    $("listView").hidden = true;
    var card = $("cardView");
    card.hidden = false;
    card.innerHTML = cardHtml(s, null);
    wireCard(s, null);
    $("sscroll").scrollTop = 0;
    setSnap(wide.matches ? snapName : "full");
    if (markers[id]) focus(markers[id].m.getLatLng());
    try {
      var d = await A.receipt(id);
      if (S.selected !== id) return;
      var full = Object.assign({}, s, d.space || {});
      card.innerHTML = cardHtml(full, d);
      wireCard(full, d);
    } catch (e) { /* keep the short card */ }
  }

  function cardHtml(s, d) {
    var trail = (d && d.trail) || [];
    var conf = s.confidence != null ? Math.round(Math.max(0, Math.min(1, +s.confidence)) * 100) : null;
    return '<div class="chead"><div class="status s-' + esc(s.state) + '"><span class="sg">' + glyph(s.state) + "</span>" + esc(label(s.state)) + "</div>" +
      '<button class="close" id="closeCard" type="button" aria-label="Back to the list">×</button></div>' +
      '<h2 class="cstreet">' + esc(s.street || "Street unknown") + (s.side === "L" ? " · left side" : s.side === "R" ? " · right side" : "") + "</h2>" +
      (s.preview ? '<p class="pvline"><span class="tag-preview">PREVIEW ENTRY · NOT A RESULT</span></p>' : "") +
      '<div class="facts3"><div>' + esc(fits(s.length_m)) + "</div><div>" + esc(seen(s.last_seen)) + "</div>" +
      (s._d != null ? "<div>" + esc(fmtDist(s._d)) + " away</div>" : "") + "</div>" +
      (s.position_status === "uncertain" ? '<p class="muted">Position is approximate.</p>' : "") +
      '<div class="media" id="cardMedia"></div>' +
      '<details class="why"><summary>Why we think so</summary>' +
      (s.receipt_text ? "<p>" + esc(s.receipt_text) + "</p>" : "") +
      '<p class="muted">' + (conf != null ? "Confidence " + conf + "% · " : "") + esc(UI.sourceLabel(s.source)) + (s.last_seen ? " · " + esc(s.last_seen) : "") + "</p>" +
      (trail.length ? '<ol class="trail">' + trail.map(function (t, i) {
        var det = A.parseDetail(t), keys = Object.keys(det).filter(function (k) { return k !== "frame_size"; }).slice(0, 3);
        return '<li class="' + (t.step === "overrule" ? "ovr" : "") + '"><span class="n">' + (t.step_no || i + 1) + '</span><div class="st">' + esc(STEP_NAMES[t.step] || t.step) + "</div>" +
          '<div class="kv">' + keys.map(function (k) { var v = det[k]; return esc(k.replace(/_/g, " ")) + ": <b>" + esc(Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v) + "</b>"; }).join(" · ") + "</div></li>";
      }).join("") + "</ol>" : (d ? '<p class="muted">No steps recorded.</p>' : '<p class="muted">Loading…</p>')) +
      "</details>" +
      '<div class="fbbar">' +
      '<div class="fb" id="fb"><button type="button" class="fbb" data-v="up">👍 Looks right</button><button type="button" class="fbb" data-v="down">👎 Not right</button></div>' +
      '<div class="fbreasons" id="fbReasons" hidden>' + A.FEEDBACK_REASONS.map(function (r) {
        return '<button type="button" class="fbr" data-r="' + r[0] + '">' + esc(r[1]) + "</button>";
      }).join("") + "</div>" +
      '<p class="fbdone" id="fbDone" hidden></p></div>';
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
    if (!wide.matches && snapName === "full") setSnap("half");
  }

  // ------------------------------------------------------------ controls
  $("destForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    var place = $("dest").value.trim();
    if (!place) return;
    $("dest").blur();
    var g = await A.geocode(place);
    if (!g) { $("area").hidden = false; $("area").textContent = "Couldn't find “" + place + "”"; return; }
    closeCard();
    setSnap("peek");
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
  function openFilter(open) {
    $("filterSheet").hidden = !open;
    if (open) $("filterDone").focus(); else $("filterBtn").focus();
  }
  $("filterBtn").addEventListener("click", function () { openFilter(true); });
  $("filterDone").addEventListener("click", function () { openFilter(false); });
  $("filterBack").addEventListener("click", function () { openFilter(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("filterSheet").hidden) openFilter(false); });
  ["showOcc", "showOther"].forEach(function (id) { $(id).addEventListener("change", draw); });
  map.on("click", function () {
    if (!$("cardView").hidden) closeCard();
    else if (!wide.matches && snapName !== "peek") setSnap("peek");
  });

  // ------------------------------------------------------------ start: URL pin, else the area with data
  setSnap("peek", true);
  A.init().then(async function () {
    setSnap("peek", true);
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
    var pad = wide.matches ? { padding: [50, 50] } : { paddingTopLeft: [24, topH() + 16], paddingBottomRight: [24, snaps().peek + 16] };
    map.fitBounds(all.map(function (s) { return [s.lat, s.lon]; }), Object.assign({ maxZoom: 18 }, pad));
    load(lat, lon, "Demo area" + (pool[0].street ? " · " + pool[0].street : ""));
  });
})();
