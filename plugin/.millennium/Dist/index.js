// Discord-ish Chat Helper — Millennium frontend module (runs in SharedJSContext).
// Hand-authored (no build step). Reaches the friends popup via g_PopupManager and
// applies DOM changes CSS can't do:
//   1) move the voice call control into the .chatHeader bar
//   2) set a "Message <friend>…" placeholder on the composer
//   3) build a Discord-style center-stage call screen with a minimize/expand toggle
(function () {
  window.__DISCORDISH_LOADED__ = (window.__DISCORDISH_LOADED__ || 0) + 1;

  function friendsDocs() {
    var pm = window.g_PopupManager;
    if (!pm || typeof pm.GetPopups !== "function") return [];
    var docs = [];
    try {
      pm.GetPopups().forEach(function (p) {
        var doc =
          (p.m_popup && p.m_popup.document) ||
          p.document ||
          (p.window && p.window.document);
        if (doc && /Friends List/.test(doc.title || "")) docs.push(doc);
      });
    } catch (e) {}
    return docs;
  }

  function chatTweaks(doc) {
    // voice control -> header bar
    doc.querySelectorAll(".chatWindow").forEach(function (win) {
      var header = win.querySelector(".chatHeader");
      var voice = win.querySelector(".ChatMessageEntryVoice");
      if (header && voice && !header.contains(voice)) header.appendChild(voice);
    });
    // placeholder
    var name = ((doc.title || "").split(" - ")[1] || "").replace(/ \+ \d+ Chats?$/, "");
    doc.querySelectorAll(".chatEntry textarea").forEach(function (ta) {
      if (!ta.placeholder) ta.placeholder = name ? "Message " + name + "…" : "Message…";
    });
  }

  function callStage(doc) {
    // Voice elements live in a details area whose location shifts with the view,
    // so query doc-wide; host the stage in the currently-visible chat's main area.
    var participants = doc.querySelector(".VoiceChannelParticipants");
    var controls = doc.querySelector(".activeVoiceButtons");
    var wins = [].slice.call(doc.querySelectorAll(".chatWindow"));
    var win = wins.filter(function (w) { return w.getBoundingClientRect().width > 0; })[0];
    var main = win && win.querySelector(".ChatHistoryContainer");
    var stage = doc.querySelector(".discordish-stage");
    var inCall = !!participants; // participant list exists only during a call

    {
      if (inCall && main) {
        if (!stage) {
          stage = doc.createElement("div");
          stage.className = "discordish-stage";
          var btn = doc.createElement("button");
          btn.className = "discordish-min-btn";
          btn.title = "Minimize / expand call";
          btn.textContent = "—";
          btn.addEventListener("click", function () {
            stage.classList.toggle("minimized");
          });
          var tiles = doc.createElement("div");
          tiles.className = "discordish-tiles";
          stage.appendChild(btn);
          stage.appendChild(tiles);
          main.appendChild(stage);
        }
        var tilesEl = stage.querySelector(".discordish-tiles");
        if (participants.parentElement !== tilesEl) tilesEl.appendChild(participants);
        if (controls && controls.parentElement !== stage) stage.appendChild(controls);
      } else if (stage && !inCall) {
        stage.remove(); // call ended -> tear the stage down so chat is visible
      }
    }
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
