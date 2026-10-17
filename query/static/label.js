/* Kerb Map: labelling. One item at a time, keyboard first, optimistic saves with retry. */
(function () {
  "use strict";
  var A = window.KerbAPI, UI = window.KerbUI, esc = A.esc;
  var $ = function (id) { return document.getElementById(id); };
  var QS = new URLSearchParams(location.search);
  var KIND = QS.get("mode") || "";          // e.g. third_look (from the board)
  var SPLIT = QS.get("split") || "";
  var QKEY = "kerb.label.queue";
  var LABELS = { free: "FREE", occupied: "OCCUPIED", not_space: "NOT A SPACE", cannot_tell: "CAN'T TELL" };

  UI.theme();
  var name = A.store("kerb.name") || "";
  var batch = null, items = [], idx = 0, results = [], history = [];
  var shownAt = 0, batchStart = 0, pendingNote = "", reasons = [];
  var sessionCount = 0, baseCount = 0, streak = 0;
  var queue = [];
  try { queue = JSON.parse(A.store(QKEY) || "[]") || []; } catch (e) { queue = []; }

  function toast(text) {
    var t = $("toast");
    t.textContent = text; t.classList.add("on");
    clearTimeout(toast._h); toast._h = setTimeout(function () { t.classList.remove("on"); }, 900);
  }
  function saveQueue() { A.store(QKEY, JSON.stringify(queue)); }
  function paintPending() {
    var p = $("pending");
    p.hidden = !queue.length;
    p.textContent = queue.length + " unsent";
  }
  function paintMe() {
    $("me").textContent = name ? name + " · " + (baseCount + sessionCount) + (streak > 2 ? " · streak " + streak : "") : "";
  }

  // ------------------------------------------------------------ background saving
  var pumping = false, delay = 1500;
  async function pump() {
    if (pumping) return;
    pumping = true;
    while (queue.length) {
      var job = queue[0];
      try {
        await A.postLabel(job.body);
        queue.shift(); saveQueue(); delay = 1500;
      } catch (e) {
        if (e.status && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429) {
          queue.shift(); saveQueue(); toast("Rejected: " + e.message);
          continue;
        }
        paintPending();
        pumping = false;
        setTimeout(pump, delay);
        delay = Math.min(delay * 2, 20000);
        return;
      }
      paintPending();
    }
    paintPending();
    pumping = false;
  }

  // ------------------------------------------------------------ batch flow
  async function claim() {
    $("doneCard").hidden = true;
    $("model").innerHTML = '<span class="muted">Claiming a batch…</span>';
    try {
      batch = await A.claimBatch(name, SPLIT || undefined, KIND || undefined);
    } catch (e) {
      $("model").innerHTML = '<span class="err">' + esc(e.message) + "</span>";
      return;
    }
    items = batch.items || [];
    reasons = A.reasons(batch.reasons);
    $("reasons").innerHTML = reasons.map(function (r, i) {
      return '<button type="button" data-r="' + esc(r) + '">' + esc(r) + "<kbd>" + (i + 1) + "</kbd></button>";
    }).join("");
    idx = 0; results = []; history = [];
    batchStart = Date.now();
    if (!items.length) { finish(true); return; }
    show();
  }

  function show() {
    closeReasons(); closeNote(true);
    var it = items[idx];
    $("pbar").style.width = (idx / items.length * 100) + "%";
    $("ptext").textContent = idx + "/" + items.length;
    UI.media($("ev"), it.evidence_uri, it.mark, it.clip_start, it.clip_end, { autoplay: true, keyframe: it.keyframe != null ? it.keyframe : it.clip_start });
    $("model").innerHTML = it.model_state ? "Model " + UI.glyph(it.model_state) + esc(UI.stateLabel(it.model_state)) : "";
    $("where").textContent = [it.street, it.side ? UI.sideLabel(it.side) : "", it.source ? UI.sourceLabel(it.source) : ""].filter(Boolean).join(" · ");
    $("itemTag").innerHTML = it.preview ? '<span class="tag-preview">PREVIEW ITEM</span>' : "";
    shownAt = Date.now();
  }

  function decide(label, reason) {
    if (!items.length || idx >= items.length || !$("doneCard").hidden || !$("nameCard").hidden) return;
    var it = items[idx];
    var body = { item_id: it.item_id, labeller: name, label: label, ms_taken: Date.now() - shownAt };
    if (reason) body.reason = reason;
    if (pendingNote) body.note = pendingNote;
    if (it.preview) body.model_state = it.model_state; // preview store only, to compute agreement locally
    var key = it.item_id + "|" + Date.now();
    queue.push({ key: key, item_id: it.item_id, body: body }); saveQueue(); pump();
    results[idx] = { label: label, agrees: it.model_state ? it.model_state === label : null, key: key };
    history.push(idx);
    sessionCount++; streak++; paintMe();
    pendingNote = "";
    var btn = document.querySelector('.big4 button[data-l="' + label + '"]');
    if (btn) { btn.classList.add("flash"); setTimeout(function () { btn.classList.remove("flash"); }, 160); }
    toast(LABELS[label] + (reason ? " · " + reason : ""));
    advance();
  }

  function advance() {
    idx++;
    if (idx >= items.length) finish(false); else show();
  }

  function skip() {
    if (idx >= items.length) return;
    results[idx] = { skipped: true };
    history.push(idx);
    streak = 0; paintMe();
    toast("Skipped");
    advance();
  }

  function undo() {
    if (!history.length) return;
    $("doneCard").hidden = true;
    var prev = history.pop();
    var r = results[prev];
    if (r && r.key) {
      var qi = queue.findIndex(function (j) { return j.key === r.key; });
      if (qi > 0 || (qi === 0 && !pumping)) { queue.splice(qi, 1); saveQueue(); paintPending(); }
      // If it was already sent, the next label for this item is appended and the latest one counts.
      sessionCount = Math.max(0, sessionCount - 1);
    }
    streak = 0; paintMe();
    results[prev] = null;
    idx = prev;
    toast("Undo");
    show();
  }

  function finish(empty) {
    $("pbar").style.width = "100%";
    $("ptext").textContent = items.length + "/" + items.length;
    var done = results.filter(function (r) { return r && r.label; });
    var cmp = done.filter(function (r) { return r.agrees !== null; });
    var agree = cmp.filter(function (r) { return r.agrees; }).length;
    $("dItems").textContent = done.length;
    $("dMin").textContent = ((Date.now() - batchStart) / 60000).toFixed(1);
    $("dAgree").textContent = cmp.length ? Math.round(agree / cmp.length * 100) + "%" : "–";
    document.querySelector("#doneCard h2").textContent = empty ? "Nothing to label" : "Batch done";
    $("nextBatch").textContent = empty ? "Try again" : "Next batch";
    $("doneCard").hidden = false;
    $("nextBatch").focus();
    if (batch && batch.batch_id) A.releaseBatch(batch.batch_id).catch(function () {});
    batch = null;
  }

  // ------------------------------------------------------------ reason picker + note
  function openReasons() { $("reasons").hidden = false; $("big4").hidden = true; }
  function closeReasons() { $("reasons").hidden = true; $("big4").hidden = false; }
  function openNote() { $("notebox").hidden = false; $("note").value = pendingNote; $("note").focus(); }
  function closeNote(clear) { $("notebox").hidden = true; if (clear) $("note").value = ""; if (document.activeElement === $("note")) $("note").blur(); }

  $("reasons").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-r]");
    if (b) decide("not_space", b.getAttribute("data-r"));
  });
  $("big4").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-l]");
    if (!b) return;
    var l = b.getAttribute("data-l");
    if (l === "not_space") openReasons(); else decide(l);
  });
  $("notebox").addEventListener("submit", function (e) {
    e.preventDefault();
    pendingNote = $("note").value.trim();
    closeNote(false);
    if (pendingNote) toast("Note kept");
  });
  $("kSkip").addEventListener("click", skip);
  $("kUndo").addEventListener("click", undo);
  $("kReplay").addEventListener("click", function () { UI.replay($("ev")); });
  $("kNote").addEventListener("click", openNote);
  $("nextBatch").addEventListener("click", claim);

  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") {
      if (e.key === "Escape") { closeNote(false); e.preventDefault(); }
      return;
    }
    if (!$("doneCard").hidden) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); claim(); }
      else if (e.key.toLowerCase() === "u") undo();
      return;
    }
    var k = e.key.toLowerCase();
    if (!$("reasons").hidden) {
      if (/^[1-9]$/.test(k) && reasons[+k - 1]) { e.preventDefault(); decide("not_space", reasons[+k - 1]); }
      else if (k === "escape" || k === "n") { e.preventDefault(); closeReasons(); }
      return;
    }
    var map = { f: "free", o: "occupied", c: "cannot_tell" };
    if (map[k]) { e.preventDefault(); decide(map[k]); }
    else if (k === "n") { e.preventDefault(); openReasons(); }
    else if (k === "s") { e.preventDefault(); skip(); }
    else if (k === "u") { e.preventDefault(); undo(); }
    else if (k === "t") { e.preventDefault(); openNote(); }
    else if (k === " ") { e.preventDefault(); UI.replay($("ev")); }
  });

  window.addEventListener("beforeunload", function () {
    if (batch && batch.batch_id && idx < items.length) A.releaseBatch(batch.batch_id).catch(function () {});
  });

  // ------------------------------------------------------------ start
  async function start() {
    paintMe();
    try {
      var s = await A.labelSummary();
      var mine = s && s.by_labeller && s.by_labeller[name];
      baseCount = mine ? (mine.labels || 0) : 0;
    } catch (e) { baseCount = 0; }
    paintMe();
    pump();
    claim();
  }

  A.init().then(function () {
    paintPending();
    if (!name) {
      $("nameCard").hidden = false;
      $("name").focus();
      $("nameForm").addEventListener("submit", function (e) {
        e.preventDefault();
        name = $("name").value.trim();
        if (!name) return;
        A.store("kerb.name", name);
        $("nameCard").hidden = true;
        start();
      });
    } else start();
  });
})();
