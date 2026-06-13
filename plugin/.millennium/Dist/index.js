// Discord-ish Chat Helper — Millennium frontend module (runs in SharedJSContext).
// Hand-authored (no build step). Reaches the friends popup via g_PopupManager.
//   1) move the voice control into the .chatHeader bar
//   2) "Message <friend>…" placeholder
//   3) Discord-style center-stage call screen (minimize/expand) — MIRRORS Steam's
//      voice UI (clone tiles + proxy-click controls) instead of moving its
//      React-owned nodes, which would duplicate them.
(function () {
  window.__DISCORDISH_LOADED__ = (window.__DISCORDISH_LOADED__ || 0) + 1;

  function friendsDocs() {
    var pm = window.g_PopupManager;
    if (!pm || typeof pm.GetPopups !== "function") return [];
    var docs = [];
    try {
      pm.GetPopups().forEach(function (p) {
        var doc = (p.m_popup && p.m_popup.document) || p.document || (p.window && p.window.document);
        if (doc && /Friends List/.test(doc.title || "")) docs.push(doc);
      });
    } catch (e) {}
    return docs;
  }

  function chatTweaks(doc) {
    doc.querySelectorAll(".chatWindow").forEach(function (win) {
      var header = win.querySelector(".chatHeader");
      var voice = win.querySelector(".ChatMessageEntryVoice");
      if (header && voice && !header.contains(voice)) header.appendChild(voice);
    });
    var name = ((doc.title || "").split(" - ")[1] || "").replace(/ \+ \d+ Chats?$/, "");
    doc.querySelectorAll(".chatEntry textarea").forEach(function (ta) {
      if (!ta.placeholder) ta.placeholder = name ? "Message " + name + "…" : "Message…";
    });
  }

  function el(doc, tag, cls) { var e = doc.createElement(tag); if (cls) e.className = cls; return e; }

  function buildControls(doc, stage) {
    var bar = el(doc, "div", "ds-controls");
    [["mic", "Mute", ".ToggleMicrophoneButton"],
     ["out", "Deafen", ".ToggleVoiceOutputButton"],
     ["leave", "Leave", ".chatEndVoiceChat"]].forEach(function (spec) {
      var b = el(doc, "button", "ds-btn ds-" + spec[0]);
      b.title = spec[1];
      b.dataset.src = spec[2];
      b.addEventListener("click", function () {
        var orig = doc.querySelector(".activeVoiceButtons " + spec[2]) || doc.querySelector(spec[2]);
        if (orig) orig.click();
      });
      bar.appendChild(b);
    });
    stage.appendChild(bar);
  }

  function isVisible(el) {
    if (!el) return false;
    var b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0;
  }

  function callStage(doc) {
    var src = doc.querySelector(".VoiceChannelParticipants");
    var hasControls = !!doc.querySelector(".activeVoiceButtons");
    var inCall = !!src && hasControls;

    // Only show the stage when the VISIBLE chat is a named group (.namedGroup) —
    // group voice channels live there; 1:1 DMs lack it. Steam's voice UI is
    // global, so this is how we make the stage "disappear when in other chats".
    var wins = [].slice.call(doc.querySelectorAll(".chatWindow"));
    var win = wins.filter(function (w) { return w.getBoundingClientRect().width > 0; })[0];
    var viewingGroup = !!win && win.classList.contains("namedGroup");
    var main = win && win.querySelector(".ChatHistoryContainer");
    var shouldShow = inCall && viewingGroup && !!main;

    // Cleanup: drop every stage that isn't the intended one (fixes "stuck on the
    // first DM" + stale duplicates across windows).
    doc.querySelectorAll(".discordish-stage").forEach(function (s) {
      if (!shouldShow || s.parentElement !== main) s.remove();
    });
    if (!inCall) doc.documentElement.classList.remove("discordish-incall");
    if (!shouldShow) return;
    doc.documentElement.classList.add("discordish-incall"); // CSS hides Steam's originals

    var stage = main.querySelector(".discordish-stage");
    if (!stage) {
      stage = el(doc, "div", "discordish-stage");
      var btn = el(doc, "button", "discordish-min-btn");
      btn.title = "Minimize / expand call";
      btn.textContent = "—";
      btn.addEventListener("click", function () { stage.classList.toggle("minimized"); });
      stage.appendChild(btn);
      stage.appendChild(el(doc, "div", "discordish-tiles"));
      buildControls(doc, stage);
      main.appendChild(stage);
    }

    // mirror participant tiles (rebuild from Steam's hidden list)
    var tiles = stage.querySelector(".discordish-tiles");
    var sig = [];
    var src_friends = [].slice.call(src.querySelectorAll(".friend"));
    src_friends.forEach(function (f) {
      var nameEl = f.querySelector(".nOdcT-MoOaXGePXLyPe0H");
      sig.push((nameEl ? nameEl.textContent : "?") + (f.classList.contains("speaking") ? "*" : "") +
               (f.querySelector(".voiceStatusMic.disabled") ? "m" : ""));
    });
    var sigStr = sig.join("|");
    if (tiles.dataset.sig !== sigStr) {
      tiles.dataset.sig = sigStr;
      tiles.textContent = "";
      src_friends.forEach(function (f) {
        var nameEl = f.querySelector(".nOdcT-MoOaXGePXLyPe0H");
        var img = f.querySelector("img.avatar");
        var tile = el(doc, "div", "ds-tile" + (f.classList.contains("speaking") ? " speaking" : ""));
        var av = el(doc, "div", "ds-avatar");
        if (img && img.src) av.style.backgroundImage = "url(" + img.src + ")";
        if (f.querySelector(".voiceStatusMic.disabled")) tile.appendChild(el(doc, "div", "ds-muted"));
        var nm = el(doc, "div", "ds-name");
        nm.textContent = nameEl ? nameEl.textContent : "";
        tile.appendChild(av);
        tile.appendChild(nm);
        tiles.appendChild(tile);
      });
    }

    // sync control icons from Steam's real SVGs (also reflects mute/deafen state)
    stage.querySelectorAll(".ds-btn").forEach(function (b) {
      var sel = b.dataset.src;
      var orig = doc.querySelector(".activeVoiceButtons " + sel) || doc.querySelector(sel);
      var svg = orig && orig.querySelector("svg");
      if (svg && b.dataset.icon !== svg.outerHTML) {
        b.dataset.icon = svg.outerHTML;
        b.innerHTML = svg.outerHTML;
      }
    });
  }

  function tick() {
    friendsDocs().forEach(function (doc) {
      try { chatTweaks(doc); } catch (e) {}
      try { callStage(doc); } catch (e) {}
    });
  }

  setInterval(tick, 500);
  tick();
})();

export default function () {}
