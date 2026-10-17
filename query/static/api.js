/* Kerb Map: the only file that talks to the server.
   Every URL is relative (the app is served under /app). Field names follow common/kerb_schema.py.
   Preview data (preview-data.json) is used only with ?preview=1 or when the API cannot be reached. */
(function () {
  "use strict";
  var QS = new URLSearchParams(location.search);
  var WANT_PREVIEW = QS.get("preview") === "1";

  // ------------------------------------------------------------------ small helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      if (val === null) localStorage.removeItem(key); else localStorage.setItem(key, val);
    } catch (e) { return null; }
    return val;
  }
  function qs(obj) {
    var p = new URLSearchParams();
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k];
      if (v !== undefined && v !== null && v !== "") p.set(k, v);
    });
    var s = p.toString();
    return s ? "?" + s : "";
  }
  function withTimeout(ms) {
    if (typeof AbortController === "undefined") return undefined;
    var c = new AbortController();
    setTimeout(function () { c.abort(); }, ms);
    return c.signal;
  }
  async function http(method, url, body, ms) {
    var opts = { method: method, headers: {}, signal: withTimeout(ms || 15000) };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    var r = await fetch(url, opts);
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      var err = new Error((data && (data.detail || data.error)) || ("HTTP " + r.status));
      err.status = r.status;
      throw err;
    }
    return data;
  }
  var get = function (url, ms) { return http("GET", url, undefined, ms); };
  var post = function (url, body) { return http("POST", url, body); };

  function haversine(a, b, c, d) {
    var R = 6371000, toR = Math.PI / 180;
    var dLat = (c - a) * toR, dLon = (d - b) * toR;
    var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(a * toR) * Math.cos(c * toR) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function nowIso() { return new Date().toISOString().replace(/\.\d+Z$/, "Z"); }
  function parseDetail(step) {
    try { var d = JSON.parse(step.detail_json || "{}"); return d && typeof d === "object" ? d : {}; }
    catch (e) { return {}; }
  }
  function markFrom(trail) {
    for (var i = 0; i < (trail || []).length; i++) {
      var d = parseDetail(trail[i]);
      if (Array.isArray(d.box) && d.box.length === 4) return { box: d.box, frame_size: d.frame_size };
      if (Array.isArray(d.point) && d.point.length === 2) return { point: d.point, frame_size: d.frame_size };
    }
    return null;
  }

  // ------------------------------------------------------------------ mode + preview store
  var K = { mode: "live", reason: "", esc: esc, store: store, parseDetail: parseDetail };
  var P = null; // preview data
  var PV_KEY = "kerb.preview.labels";

  async function loadPreview() {
    if (!P) {
      try { P = await get("preview-data.json", 8000); }
      catch (e) { P = { spaces: [], trail: [], cameras: [], coverage: { cameras: [], passes: [] }, measured: null, missing: true }; } // the deployed app ships no preview file
    }
    P.spaces.forEach(function (s) { s.preview = true; });
    return P;
  }

  K.init = async function () {
    if (WANT_PREVIEW) {
      await loadPreview();
      K.mode = "preview";
      K.reason = "Preview data. These entries are layout placeholders, not results.";
    } else {
      try {
        await get("api/spaces" + qs({ limit: 1 }), 6000);
        K.mode = "live";
      } catch (e) {
        await loadPreview();
        K.mode = "preview";
        K.reason = P.missing ? "API not reachable. Nothing to show until it is back."
                             : "API not reachable, so preview data is shown. These entries are not results.";
      }
    }
    UI.banner();
    UI.keepPreviewInLinks();
    return K.mode;
  };
  K.isPreview = function () { return K.mode === "preview"; };

  // ------------------------------------------------------------------ map: spaces, search, receipt, overrule
  function pvFilter(o) {
    var rows = P.spaces.filter(function (s) {
      return (!o.state || s.state === o.state) && (!o.side || s.side === o.side) && (!o.source || s.source === o.source) &&
        (!o.job_id || s.original_video === PV_JOB_VIDEO);
    }).map(function (s) { return Object.assign({}, s); });
    if (o.lat != null && o.lon != null && o.lat !== "" && o.lon !== "") {
      rows = rows.filter(function (s) { return s.lat != null && s.lon != null; });
      rows.forEach(function (s) { s.distance_m = Math.round(haversine(+o.lat, +o.lon, s.lat, s.lon)); });
      rows = rows.filter(function (s) { return s.distance_m <= +(o.radius_m || 200); });
      rows.sort(function (a, b) { return a.distance_m - b.distance_m; });
    }
    return rows;
  }
  function describe(o, q) {
    var conds = ["state", "side", "source"].filter(function (k) { return o[k]; })
      .map(function (k) { return k + " = '" + o[k] + "'"; });
    var parts = [conds.length ? "filter kerb_spaces where " + conds.join(" and ") : "all kerb_spaces"];
    if (o.lat != null && o.lat !== "") parts.push("within " + (o.radius_m || 200) + " m of (" + (+o.lat).toFixed(5) + ", " + (+o.lon).toFixed(5) + ")");
    if (q) parts.push("then rank by cosine similarity of '" + q + "' to each stored embedding");
    return parts.join(", ");
  }

  // GET api/spaces?lat&lon&radius_m&state&side&source&limit -> {count, query, spaces:[kerb_spaces row + distance_m]}
  K.spaces = async function (o) {
    o = o || {};
    if (K.isPreview()) {
      var rows = pvFilter(o);
      return { count: rows.length, spaces: rows, query_explain: "PREVIEW: no database query ran. Live query: " + describe(o) };
    }
    var url = "api/spaces" + qs({ lat: o.lat, lon: o.lon, radius_m: o.radius_m, state: o.state, side: o.side, source: o.source, job_id: o.job_id, limit: o.limit || 500 });
    var d = await get(url);
    d.query_explain = d.query_explain || ("GET " + url + "  ->  " + describe(o));
    return d;
  };

  // GET api/search?q&lat&lon&radius_m&state&side&source -> {q, filtered, query_explain, results:[row + score]}
  K.search = async function (o) {
    if (K.isPreview()) {
      var rows = pvFilter(o);
      rows.forEach(function (r) { r.score = null; });
      return { q: o.q, filtered: rows.length, results: rows,
        query_explain: "PREVIEW: no database query ran. Live query: " + describe(o, o.q) };
    }
    return get("api/search" + qs({ q: o.q, lat: o.lat, lon: o.lon, radius_m: o.radius_m, state: o.state, side: o.side, source: o.source, limit: o.limit || 50 }));
  };

  // GET api/receipt/{space_id}?q -> {space, model_state, trail, overrules, observations, camera, evidence_uri, mark, query_explain?}
  K.receipt = async function (id, q) {
    if (K.isPreview()) {
      var s = P.spaces.find(function (x) { return x.space_id === id; });
      if (!s) throw new Error("no such space");
      var trail = P.trail.filter(function (t) { return t.space_id === id; }).sort(function (a, b) { return a.step_no - b.step_no; });
      var ovs = (P._overrules || []).filter(function (o) { return o.space_id === id; });
      var first = trail.find(function (t) { return t.step === "overrule"; });
      return {
        space: Object.assign({}, s), trail: trail, overrules: ovs, observations: [],
        model_state: first ? (parseDetail(first).before || s.state) : s.state,
        camera: (P.cameras || []).find(function (c) { return c.camera_id === s.camera_id; }) || null,
        evidence_uri: s.evidence_uri, mark: markFrom(trail),
        query_explain: q ? { q: q, how: "PREVIEW: no ranking ran", score: null } : null
      };
    }
    return get("api/receipt/" + encodeURIComponent(id) + qs({ q: q }));
  };

  // POST api/overrule {space_id, new_state, note, by} -> {overrule_id, before:{state}, after:{state}, space}
  K.overrule = async function (body) {
    if (K.isPreview()) {
      var s = P.spaces.find(function (x) { return x.space_id === body.space_id; });
      var before = s.state, at = nowIso(), oid = "pv_ovr_" + Math.random().toString(16).slice(2, 10);
      P._overrules = P._overrules || [];
      P._overrules.push({ overrule_id: oid, space_id: s.space_id, by: body.by, new_state: body.new_state, note: body.note, at: at, receipts_changed: [s.space_id] });
      var n = P.trail.filter(function (t) { return t.space_id === s.space_id; }).length + 1;
      P.trail.push({ trail_id: s.space_id + "_ovr" + n, space_id: s.space_id, step_no: n, step: "overrule", at: at,
        detail_json: JSON.stringify({ before: before, after: body.new_state, by: body.by, note: body.note, overrule_id: oid }) });
      s.state = body.new_state;
      return { overrule_id: oid, space_id: s.space_id, before: { state: before }, after: { state: s.state }, space: Object.assign({}, s), preview: true };
    }
    return post("api/overrule", body);
  };

  // Coverage. Optional GET api/coverage -> {cameras:[kerb_cameras row], passes:[{original_video, source, at, line:[[lat,lon],...]}]}
  // Without it, coverage is derived from the receipts on screen.
  K.coverage = async function (rows) {
    if (K.isPreview()) return P.coverage;
    try { return await get("api/coverage", 6000); }
    catch (e) {
      var passes = {}, cams = {};
      (rows || []).forEach(function (s) {
        if (s.lat == null || s.lon == null) return;
        if (s.source === "dashcam") (passes[s.original_video || "?"] = passes[s.original_video || "?"] || []).push(s);
        else (cams[s.camera_id || "?"] = cams[s.camera_id || "?"] || []).push(s);
      });
      return {
        derived: true,
        cameras: [],
        streetcam_stretches: Object.keys(cams).map(function (k) {
          return { camera_id: k, line: cams[k].sort(function (a, b) { return a.lat - b.lat; }).map(function (s) { return [s.lat, s.lon]; }) };
        }),
        passes: Object.keys(passes).map(function (k) {
          return { original_video: k, line: passes[k].sort(function (a, b) { return (a.t_sec || 0) - (b.t_sec || 0); }).map(function (s) { return [s.lat, s.lon]; }) };
        })
      };
    }
  };

  // Place lookup. Optional GET api/geocode?q -> {lat, lon, label}. Fallback: match a street name in the receipts.
  K.geocode = async function (place) {
    var m = String(place).match(/(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)/);
    if (m) return { lat: +m[1], lon: +m[2], label: m[1] + ", " + m[2] };
    if (!K.isPreview()) {
      try { var g = await get("api/geocode" + qs({ q: place }), 6000); if (g && g.lat != null) return g; } catch (e) { /* fall through */ }
    }
    var pool = K.isPreview() ? P.spaces : ((await K.spaces({ limit: 5000 })).spaces || []);
    var words = String(place).toLowerCase().replace(/\b(ave|avenue|st|street|rd|road|west|east|w|e)\b\.?/g, " ").split(/\s+/).filter(function (w) { return w.length > 2; });
    var hits = pool.filter(function (s) {
      var st = String(s.street || "").toLowerCase();
      return s.lat != null && words.length && words.every(function (w) { return st.indexOf(w) >= 0; });
    });
    if (!hits.length) return null;
    var lat = hits.reduce(function (a, s) { return a + s.lat; }, 0) / hits.length;
    var lon = hits.reduce(function (a, s) { return a + s.lon; }, 0) / hits.length;
    return { lat: lat, lon: lon, label: hits[0].street + " (from receipts)" };
  };

  // Evidence. http(s) URIs are used directly; s3:// and file paths go through GET api/evidence?uri.
  // Clip window: receipt.evidence_start/evidence_end or item.clip_start/clip_end (seconds into the file) -> #t=start,end
  K.media = function (uri, start, end) {
    if (!uri) return null;
    var isVideo = /\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(uri);
    var url = /^https?:\/\//i.test(uri) ? uri : "api/evidence?uri=" + encodeURIComponent(uri);
    if (isVideo && start != null) url += "#t=" + (+start).toFixed(1) + (end != null ? "," + (+end).toFixed(1) : "");
    return { kind: isVideo ? "video" : "image", url: url, uri: uri };
  };

  // ------------------------------------------------------------------ labelling: batches
  function pvLabels() { try { return JSON.parse(store(PV_KEY) || "[]"); } catch (e) { return []; } }
  function pvSaveLabels(l) { store(PV_KEY, JSON.stringify(l)); }
  var PV_REASONS = ["driveway", "junction", "crossing", "hydrant", "bus stop", "lane", "too short", "other"];
  K.reasons = function (fromServer) { return (fromServer && fromServer.length) ? fromServer : PV_REASONS; };

  function normItem(it) {
    var mark = it.mark || (it.box ? { box: it.box, frame_size: it.frame_size } : it.point ? { point: it.point, frame_size: it.frame_size } : null);
    return {
      item_id: it.item_id || it.case_id || it.space_id,
      space_id: it.space_id || null,
      evidence_uri: it.evidence_url || it.evidence_uri || null,
      clip_start: it.clip_start != null ? it.clip_start : it.t_start,
      clip_end: it.clip_end != null ? it.clip_end : it.t_end,
      keyframe: it.keyframe != null ? it.keyframe : (it.t_in_clip != null ? it.t_in_clip : (it.evidence_start_s != null ? it.evidence_start_s : null)),
      mark: mark,
      model_state: it.model_state || null,
      street: it.street || null, side: it.side || null, source: it.source || null, split: it.split || null,
      preview: !!it.preview
    };
  }

  // POST api/batches/claim {labeller, split?, kind?} -> {batch_id, items:[...], reasons?}
  K.claimBatch = async function (labeller, split, kind) {
    if (K.isPreview()) {
      var done = pvLabels().filter(function (l) { return l.labeller === labeller; }).map(function (l) { return l.item_id; });
      var pool = P.spaces.map(function (s) {
        var trail = P.trail.filter(function (t) { return t.space_id === s.space_id; });
        return normItem({ item_id: "pvitem_" + s.space_id, space_id: s.space_id, evidence_uri: s.evidence_uri, mark: markFrom(trail),
          model_state: s.state, street: s.street, side: s.side, source: s.source, split: split || "dev", preview: true });
      });
      var fresh = pool.filter(function (i) { return done.indexOf(i.item_id) < 0; });
      var items = (fresh.length ? fresh : pool).slice(0, 6);
      return { batch_id: "pv_batch_" + Date.now(), items: items, reasons: PV_REASONS, preview: true };
    }
    var d = await post("api/batches/claim", { labeller: labeller, split: split || undefined, kind: kind || undefined });
    d.items = (d.items || []).map(normItem);
    return d;
  };

  // POST api/labels {item_id, labeller, label, reason?, note?, ms_taken} -> {label_id?, agrees?, model_state?}
  K.postLabel = async function (body) {
    if (K.isPreview()) {
      var l = pvLabels();
      l.push(Object.assign({ at: nowIso() }, body));
      pvSaveLabels(l);
      return { ok: true, preview: true };
    }
    return post("api/labels", body);
  };

  // POST api/batches/{id}/release -> {}
  K.releaseBatch = async function (id) {
    if (K.isPreview() || !id) return { ok: true };
    return post("api/batches/" + encodeURIComponent(id) + "/release", {});
  };

  // GET api/labels/summary -> {by_labeller:{name:{labels, agree, compared}}, total_labels}
  K.labelSummary = async function () {
    if (K.isPreview()) {
      var by = {};
      latestPerItem(pvLabels()).forEach(function (l) {
        var b = by[l.labeller] = by[l.labeller] || { labels: 0, agree: 0, compared: 0 };
        b.labels++;
        if (l.model_state) { b.compared++; if (l.model_state === l.label) b.agree++; }
      });
      return { by_labeller: by, preview: true };
    }
    return get("api/labels/summary", 8000);
  };

  function latestPerItem(labels) {
    var m = {};
    labels.forEach(function (l) { m[l.labeller + "|" + l.item_id] = l; });
    return Object.keys(m).map(function (k) { return m[k]; });
  }

  // GET api/board -> {labellers:[{labeller, done, in_progress, items_per_min, agree, compared, agreement}],
  //                   progress:{dev:{done,total}, heldout:{done,total}}, needs_third_look, disagreements:[{reason,count}], generated_at}
  K.board = async function () {
    if (K.isPreview()) {
      var labels = latestPerItem(pvLabels());
      var per = {}, dis = {};
      labels.forEach(function (l) {
        var p = per[l.labeller] = per[l.labeller] || { labeller: l.labeller, done: 0, in_progress: 0, agree: 0, compared: 0, ms: 0 };
        p.done++; p.ms += l.ms_taken || 0;
        if (l.model_state) {
          p.compared++;
          if (l.model_state === l.label) p.agree++;
          else { var k = "model " + l.model_state + " → " + (l.reason || l.label); dis[k] = (dis[k] || 0) + 1; }
        }
      });
      var list = Object.keys(per).map(function (k) {
        var p = per[k];
        p.items_per_min = p.ms ? +(p.done / (p.ms / 60000)).toFixed(1) : null;
        p.agreement = p.compared ? p.agree / p.compared : null;
        return p;
      });
      var byItem = {};
      labels.forEach(function (l) { (byItem[l.item_id] = byItem[l.item_id] || []).push(l.label); });
      var third = Object.keys(byItem).filter(function (k) { var a = byItem[k]; return a.length >= 2 && a.some(function (x) { return x !== a[0]; }); }).length;
      var devDone = Object.keys(byItem).length;
      return {
        preview: true, labellers: list,
        progress: { dev: { done: devDone, total: P.spaces.length }, heldout: { done: 0, total: 0 } },
        needs_third_look: third,
        disagreements: Object.keys(dis).map(function (k) { return { reason: k, count: dis[k] }; }).sort(function (a, b) { return b.count - a.count; }).slice(0, 5),
        generated_at: nowIso()
      };
    }
    return get("api/board", 8000);
  };

  // ------------------------------------------------------------------ live feed + replay
  // GET api/live -> {newest_clip:{clip_id, original_video, captured_at, arrived_at}, clips_last_hour?}
  // Without it, the newest last_seen among the receipts on screen is shown instead.
  K.live = async function (rows) {
    function fromRows() {
      var newest = (rows || []).reduce(function (m, s) { return s.last_seen && (!m || s.last_seen > m) ? s.last_seen : m; }, null);
      return { derived: true, newest_clip: newest ? { captured_at: newest, arrived_at: null } : null };
    }
    if (K.isPreview()) { var r = fromRows(); r.preview = true; return r; }
    try { return await get("api/live", 5000); } catch (e) { return fromRows(); }
  };

  // Drives to replay. Optional GET api/replay/drives -> {drives:[{drive_id, label, clips}]}; else derived from dashcam receipts.
  K.replayDrives = async function (rows) {
    function fromRows(list) {
      var seen = {};
      (list || []).forEach(function (s) { if (s.source === "dashcam" && s.original_video) seen[s.original_video] = (seen[s.original_video] || 0) + 1; });
      return Object.keys(seen).map(function (k) { return { drive_id: k, label: k, clips: null }; });
    }
    if (K.isPreview()) return fromRows(P.spaces);
    try { var d = await get("api/replay/drives", 5000); if (d && d.drives) return d.drives; } catch (e) { /* derive */ }
    return fromRows(rows);
  };

  var SIM = null; // preview replay simulation
  // POST api/replay/start {drive_id, speed} -> {replay_id, clips_total}
  K.replayStart = async function (driveId, speed) {
    if (K.isPreview()) {
      var list = P.spaces.filter(function (s) { return s.original_video === driveId && s.lat != null; })
        .sort(function (a, b) { return (a.t_sec || 0) - (b.t_sec || 0); });
      SIM = { id: "pv_replay", drive: driveId, speed: speed, t0: Date.now(), list: list };
      return { replay_id: SIM.id, clips_total: list.length, preview: true };
    }
    return post("api/replay/start", { drive_id: driveId, speed: speed });
  };
  // GET api/replay/status?replay_id -> {active, drive_id, speed, clips_done, clips_total, clip_time, spaces:[kerb_spaces rows so far]}
  K.replayStatus = async function (id) {
    if (K.isPreview()) {
      if (!SIM) return { active: false, spaces: [] };
      var per = 3000 / SIM.speed;
      var n = Math.min(SIM.list.length, 1 + Math.floor((Date.now() - SIM.t0) / per));
      var shown = SIM.list.slice(0, n).map(function (s) { return Object.assign({}, s); });
      return { active: n < SIM.list.length, drive_id: SIM.drive, speed: SIM.speed, clips_done: n, clips_total: SIM.list.length,
        clip_time: shown.length ? shown[shown.length - 1].last_seen : null, spaces: shown, preview: true };
    }
    return get("api/replay/status" + qs({ replay_id: id }), 6000);
  };
  // POST api/replay/stop {replay_id}
  K.replayStop = async function (id) {
    if (K.isPreview()) { SIM = null; return { ok: true }; }
    try { return await post("api/replay/stop", { replay_id: id }); } catch (e) { return { ok: false }; }
  };

  // ------------------------------------------------------------------ driver feedback
  // POST api/feedback {space_id, vote:'up'|'down', reason?:'taken'|'not_legal'|'driveway'|'other', note?}
  //   -> {feedback_id, new_state, receipt_changed:false}
  K.FEEDBACK_REASONS = [["taken", "Taken"], ["not_legal", "Not a legal space"], ["driveway", "Driveway"], ["other", "Other"]];
  K.feedback = async function (body) {
    if (K.isPreview()) { (P._feedback = P._feedback || []).push(Object.assign({ at: nowIso() }, body)); return { ok: true, preview: true }; }
    return post("api/feedback", body);
  };

  // ------------------------------------------------------------------ on-stage upload flow
  // POST api/upload multipart {file, gps?, sample_id?} -> {job_id}
  // GET  api/upload/{job_id} -> {job_id, state: uploading|uploaded|indexing|processing|done|error,
  //        steps:[{name, status, at, detail}], filename, segments_found,
  //        receipts_by_state:{free, occupied, not_space, cannot_tell}, center:{lat, lon}|null, message}
  // GET  api/upload/latest-done -> {job_id}            (optional: the prepared fallback)
  // GET  api/samples -> {samples:[{sample_id, label}]} (optional: the "Use a sample clip" list)
  var PV_JOB_VIDEO = "set01_video_0002";
  var UPLOAD_STEPS = ["Upload to VAST", "Indexed by VAST (YOLO + Cosmos)", "Kerb analysis", "Saved to VastDB"];
  K.UPLOAD_STEPS = UPLOAD_STEPS;
  var PVJOBS = {};

  K.samples = async function () {
    if (K.isPreview()) return [{ sample_id: "pv_sample_1", label: "PREVIEW sample: Toronto drive, Augusta Ave", preview: true }];
    try {
      var d = await get("api/samples", 5000);
      return Array.isArray(d) ? d : (d && d.samples) || [];
    } catch (e) { return []; }
  };

  // onProgress(fraction 0..1) reports the browser-to-server upload; resolves {job_id}
  K.upload = function (file, gps, sampleId, onProgress) {
    if (K.isPreview()) {
      var id = "pv_job_" + Date.now().toString(36);
      PVJOBS[id] = { t0: Date.now(), filename: file ? file.name : (sampleId ? "PREVIEW sample clip" : "clip.mp4"),
        fail: new URLSearchParams(location.search).get("fail") === "1" };
      var f = 0;
      return new Promise(function (resolve) {
        var h = setInterval(function () {
          f = Math.min(1, f + 0.25);
          if (onProgress) onProgress(f);
          if (f >= 1) { clearInterval(h); resolve({ job_id: id, preview: true }); }
        }, 250);
      });
    }
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      if (file) fd.append("file", file);
      if (gps) fd.append("gps", gps);
      if (sampleId) fd.append("sample_id", sampleId);
      var x = new XMLHttpRequest();
      x.open("POST", "api/upload");
      x.responseType = "json";
      if (x.upload && onProgress) x.upload.onprogress = function (e) { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      x.onload = function () {
        var d = x.response;
        if (typeof d === "string") { try { d = JSON.parse(d); } catch (e) { d = null; } }
        if (x.status >= 200 && x.status < 300 && d && d.job_id) resolve(d);
        else { var err = new Error((d && (d.detail || d.message)) || ("Upload failed (HTTP " + x.status + ")")); err.status = x.status; reject(err); }
      };
      x.onerror = function () { reject(new Error("Upload failed: network error")); };
      x.send(fd);
    });
  };

  function pvJob(id) {
    var j = PVJOBS[id];
    if (!j) { j = PVJOBS[id] = { t0: Date.now() - 60000, filename: "PREVIEW clip.mp4", fail: false }; }
    var el = (Date.now() - j.t0) / 1000, per = 3;           // one step every 3 s in preview
    var n = Math.floor(el / per);                            // steps finished
    var states = ["uploaded", "indexing", "processing", "processing", "done"];
    var steps = UPLOAD_STEPS.map(function (name, i) {
      var at = new Date(j.t0 + (i + 1) * per * 1000).toISOString().replace(/\.\d+Z$/, "Z");
      if (j.fail && i === 1 && n >= 1) return { name: name, status: "error", at: at, detail: "PREVIEW: simulated failure" };
      if (j.fail && i > 1 && n >= 1) return { name: name, status: "pending", at: null, detail: "" };
      return { name: name, status: i < n ? "done" : i === n ? "running" : "pending", at: i < n ? at : null,
        detail: i < n ? "PREVIEW" : "" };
    });
    var rows = P.spaces.filter(function (s) { return s.original_video === PV_JOB_VIDEO && s.lat != null; });
    var done = n >= UPLOAD_STEPS.length && !j.fail;
    var by = { free: 0, occupied: 0, not_space: 0, cannot_tell: 0 };
    rows.forEach(function (s) { by[s.state] = (by[s.state] || 0) + 1; });
    var lat = rows.reduce(function (a, s) { return a + s.lat; }, 0) / (rows.length || 1);
    var lon = rows.reduce(function (a, s) { return a + s.lon; }, 0) / (rows.length || 1);
    return {
      job_id: id, preview: true, filename: j.filename,
      state: j.fail && n >= 1 ? "error" : states[Math.min(n, states.length - 1)],
      steps: steps,
      segments_found: n >= 2 && !j.fail ? rows.length : null,
      receipts_by_state: done ? by : null,
      center: done && rows.length ? { lat: lat, lon: lon } : null,
      message: j.fail && n >= 1 ? "PREVIEW: indexing failed (simulated with ?fail=1)" : (done ? "PREVIEW job finished" : "")
    };
  }

  K.uploadStatus = async function (id) {
    if (K.isPreview() || /^pv_job_/.test(id)) { if (!P) await loadPreview(); return pvJob(id); }
    return get("api/upload/" + encodeURIComponent(id), 8000);
  };

  K.latestDone = async function () {
    if (K.isPreview()) return { job_id: "pv_job_prepared", preview: true };
    try { var d = await get("api/upload/latest-done", 5000); return d && d.job_id ? d : null; }
    catch (e) { return null; }
  };

  // ------------------------------------------------------------------ measured
  // GET api/measured -> {rows:[{measure, split:'dev'|'heldout', light:'day'|'night', value, k, n, unit:'ratio'|'m'}],
  //                      search_compare:[{q, before:{label, results:[{space_id, state, street, score}]}, after:{...}}], source?, generated_at?}
  K.measured = async function () {
    if (K.isPreview()) return P.measured;
    try { return await get("api/measured", 10000); }
    catch (e) { if (e.status === 404) return null; throw e; }
  };

  // ------------------------------------------------------------------ shared UI bits
  var STATES = {
    free: { label: "Free", glyph: "F" },
    occupied: { label: "Occupied", glyph: "O" },
    not_space: { label: "Not a space", glyph: "×" },
    cannot_tell: { label: "Cannot tell", glyph: "?" }
  };
  var UI = {
    STATES: STATES,
    glyph: function (state, extra) {
      var s = STATES[state] || { glyph: "–", label: state || "unknown" };
      return '<span class="glyph s-' + esc(state) + " " + (extra || "") + '" aria-hidden="true">' + s.glyph + "</span>";
    },
    stateLabel: function (state) { return (STATES[state] || { label: state || "unknown" }).label; },
    sourceLabel: function (src) { return src === "streetcam" ? "Street camera" : src === "dashcam" ? "Dashcam" : (src || "unknown source"); },
    sideLabel: function (side) { return side === "L" ? "left side" : side === "R" ? "right side" : side === "view" ? "camera view" : "side unknown"; },
    ago: function (iso) {
      if (!iso) return "never";
      var t = Date.parse(iso);
      if (isNaN(t)) return iso;
      var s = Math.round((Date.now() - t) / 1000);
      if (s < 0) return "just now";
      if (s < 90) return s + " s ago";
      if (s < 5400) return Math.round(s / 60) + " min ago";
      if (s < 129600) return Math.round(s / 3600) + " h ago";
      return Math.round(s / 86400) + " days ago";
    },
    banner: function () {
      var b = document.getElementById("banner");
      if (!b) return;
      if (K.isPreview()) { b.hidden = false; b.innerHTML = "<b>Preview data</b><span>" + esc(K.reason) + "</span>"; }
      else b.hidden = true;
    },
    keepPreviewInLinks: function () {
      if (!WANT_PREVIEW) return;
      document.querySelectorAll("a[data-keep]").forEach(function (a) {
        var h = a.getAttribute("href");
        if (h.indexOf("preview=1") < 0) a.setAttribute("href", h + (h.indexOf("?") >= 0 ? "&" : "?") + "preview=1");
      });
    },
    theme: function () {
      var root = document.documentElement;
      var saved = store("kerb.theme");
      if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
      var btn = document.getElementById("theme");
      function current() {
        var t = root.getAttribute("data-theme");
        if (t) return t;
        return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      function paint() { if (btn) btn.textContent = current() === "dark" ? "Light" : "Dark"; }
      paint();
      if (btn) btn.addEventListener("click", function () {
        var next = current() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        store("kerb.theme", next);
        paint();
        document.dispatchEvent(new CustomEvent("kerb-theme", { detail: next }));
      });
      return current;
    },
    // Keyframe for a receipt: seconds into the evidence file where the box was measured.
    // Dashcam (dc_): gate step evidence_start_s / t (seconds into the 5 s segment file).
    // pie-receipts (pr_) and others: t_sec, which is also seconds into the segment when evidence_uri is a segment file.
    keyframe: function (d, s) {
      s = s || (d && d.space) || {};
      var trail = (d && d.trail) || [];
      for (var i = 0; i < trail.length; i++) {
        var det = parseDetail(trail[i]);
        if (det.evidence_start_s != null && isFinite(+det.evidence_start_s)) return +det.evidence_start_s;
        if (trail[i].step === "gate" && det.t != null && isFinite(+det.t)) return +det.t;
      }
      var uri = (d && d.evidence_uri) || s.evidence_uri || "";
      var t = s.t_sec != null ? +s.t_sec : null;
      if (t != null && /_segment_/.test(uri) && t >= 0 && t <= 5.5) return t;
      if (t != null && uri && uri === s.original_video) return t;
      if (d && d.evidence_start != null) return +d.evidence_start;
      return t != null && t >= 0 ? t : 0;
    },
    // What the box marks, from the geometry step ("gap", "car") or the state.
    markWord: function (d, s) {
      var trail = (d && d.trail) || [];
      for (var i = 0; i < trail.length; i++) {
        var det = parseDetail(trail[i]);
        if (trail[i].step === "geometry" && det.kind) return det.kind === "gap" ? "gap" : det.kind === "car" ? "parked car" : String(det.kind);
      }
      s = s || (d && d.space) || {};
      return s.state === "occupied" ? "parked car" : s.state === "free" ? "gap" : "kerb stretch";
    },
    // Evidence box. A video loads PAUSED at the keyframe; the box is drawn on a canvas over the rendered video
    // content (object-fit: contain letterboxing included), scaled from the frame size the box was measured in
    // (mark.frame_size, default 1920x1080). "Play clip" plays around the keyframe; the box shows only within
    // +/-0.15 s of it, and pausing returns to the keyframe.
    // opts: {keyframe, word, autoplay (label page: play once after the first draw), controls}
    media: function (el, uri, mark, start, end, opts) {
      opts = opts || {};
      var kf = opts.keyframe != null ? +opts.keyframe : (start != null ? +start : 0);
      var m = K.isPreview() ? null : K.media(uri, null, null);
      var fs = (mark && mark.frame_size && mark.frame_size.length === 2) ? mark.frame_size : [1920, 1080];
      var word = opts.word || "gap";
      function clock(t) {
        t = Math.max(0, t || 0);
        var h = Math.floor(t / 3600), mi = Math.floor(t / 60) % 60, s = t - h * 3600 - mi * 60;
        return String(h).padStart(2, "0") + ":" + String(mi).padStart(2, "0") + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
      }
      var inner;
      if (!m) {
        inner = '<div class="ph"><div>' + (K.isPreview() ? "Preview: no footage loaded" : "No evidence frame") +
          (uri ? '<span class="mono">' + esc(uri) + "</span>" : "") + "</div></div>";
      } else if (m.kind === "video") {
        inner = '<video src="' + esc(m.url) + '" muted playsinline preload="auto" disablepictureinpicture></video>';
      } else {
        inner = '<img src="' + esc(m.url) + '" alt="Evidence frame">';
      }
      el.innerHTML = inner + '<canvas class="markcv" aria-hidden="true"></canvas>' +
        '<div class="mcap">' + (mark ? "Box shows the " + esc(word) + " at " + clock(kf) : "No box recorded for this receipt") + "</div>" +
        (m && m.kind === "video" ? '<button type="button" class="mplay" aria-label="Play clip">▶ Play clip</button>' : "");
      var cv = el.querySelector("canvas.markcv");
      var v = el.querySelector("video"), img = el.querySelector("img"), btn = el.querySelector(".mplay");
      var ready = !m || (!v && !img), playEnd = null, raf = 0;

      function contentRect() {
        var W = el.clientWidth, H = el.clientHeight, iw, ih;
        if (v) { iw = v.videoWidth; ih = v.videoHeight; }
        else if (img) { iw = img.naturalWidth; ih = img.naturalHeight; }
        if (!iw || !ih) { iw = fs[0]; ih = fs[1]; }
        var sc = Math.min(W / iw, H / ih), dw = iw * sc, dh = ih * sc;
        return { x: (W - dw) / 2, y: (H - dh) / 2, w: dw, h: dh, W: W, H: H };
      }
      function draw(show) {
        var r = contentRect(), dpr = window.devicePixelRatio || 1;
        cv.width = Math.round(r.W * dpr); cv.height = Math.round(r.H * dpr);
        cv.style.width = r.W + "px"; cv.style.height = r.H + "px";
        var g = cv.getContext("2d");
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, r.W, r.H);
        if (!mark || !show || !ready) return;
        var sx = r.w / fs[0], sy = r.h / fs[1], lw = Math.max(2.5, r.w / 220);
        g.lineWidth = lw;
        if (mark.box) {
          var b = mark.box, x = r.x + b[0] * sx, y = r.y + b[1] * sy, w = (b[2] - b[0]) * sx, h = (b[3] - b[1]) * sy;
          g.strokeStyle = "rgba(0,0,0,.6)"; g.lineWidth = lw + 2; g.strokeRect(x, y, w, h);
          g.strokeStyle = "#00e5ff"; g.lineWidth = lw; g.strokeRect(x, y, w, h);
        } else if (mark.point) {
          var px = r.x + mark.point[0] * sx, py = r.y + mark.point[1] * sy, rad = Math.max(10, r.w / 40);
          g.strokeStyle = "#00e5ff"; g.beginPath(); g.arc(px, py, rad, 0, Math.PI * 2); g.stroke();
          g.fillStyle = "#00e5ff"; g.beginPath(); g.arc(px, py, lw, 0, Math.PI * 2); g.fill();
        }
      }
      function near() { return v ? Math.abs(v.currentTime - kf) <= 0.15 : true; }
      function tick() {
        if (!v || v.paused) return;
        if (playEnd != null && v.currentTime >= playEnd) { v.pause(); return; }
        draw(near());
        raf = requestAnimationFrame(tick);
      }
      var autoDone = false;
      function arrived() {
        ready = true; draw(true);
        if (opts.autoplay && !autoDone) { autoDone = true; setTimeout(function () { api.play(); }, 700); }
      }
      function toKeyframe() {
        if (!v) { draw(true); return; }
        var dur = isFinite(v.duration) ? v.duration : null;
        var t = dur != null ? Math.min(Math.max(0, kf), Math.max(0, dur - 0.04)) : Math.max(0, kf);
        if (Math.abs(v.currentTime - t) < 0.01) { arrived(); return; }
        v.addEventListener("seeked", function once() { v.removeEventListener("seeked", once); arrived(); });
        try { v.currentTime = t; } catch (e) { /* ignore */ }
      }
      var api = {
        play: function () {
          if (!v) return;
          var dur = isFinite(v.duration) ? v.duration : kf + 3;
          var from = start != null && start <= kf ? +start : Math.max(0, kf - 2);
          playEnd = end != null && end > kf ? Math.min(+end, dur) : Math.min(dur, kf + 3);
          draw(false);
          try { v.currentTime = from; } catch (e) { /* ignore */ }
          var p = v.play();
          if (p && p.catch) p.catch(function () { /* autoplay refused */ });
        },
        redraw: function () { draw(!v || v.paused ? true : near()); }
      };
      el._kerbMedia = api;
      if (v) {
        v.addEventListener("loadedmetadata", toKeyframe);
        v.addEventListener("play", function () { if (btn) btn.textContent = "❚❚ Pause"; cancelAnimationFrame(raf); raf = requestAnimationFrame(tick); });
        v.addEventListener("pause", function () { if (btn) btn.textContent = "▶ Play clip"; cancelAnimationFrame(raf); toKeyframe(); });
        v.addEventListener("ended", function () { if (btn) btn.textContent = "▶ Play clip"; toKeyframe(); });
        if (btn) btn.addEventListener("click", function () { if (v.paused) api.play(); else v.pause(); });
      }
      if (img) img.addEventListener("load", function () { ready = true; draw(true); });
      var media = v || img;
      if (media) media.addEventListener("error", function () {
        ready = false;
        var ph = document.createElement("div");
        ph.className = "ph";
        ph.innerHTML = "<div>Evidence could not be loaded<span class=\"mono\">" + esc(uri) + "</span></div>";
        media.replaceWith(ph);
        if (btn) btn.remove();
        draw(false);
      });
      if (typeof ResizeObserver !== "undefined") {
        var ro = new ResizeObserver(function () { if (!el.isConnected) { ro.disconnect(); return; } api.redraw(); });
        ro.observe(el);
      } else window.addEventListener("resize", api.redraw);
      window.addEventListener("orientationchange", function () { setTimeout(api.redraw, 250); });
      draw(true);
      return v;
    },
    replay: function (el) {
      if (el && el._kerbMedia) el._kerbMedia.play();
    }
  };

  window.KerbAPI = K;
  window.KerbUI = UI;
})();
