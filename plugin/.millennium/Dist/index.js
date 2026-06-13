// Discord-ish Chat Helper — Millennium frontend module (runs in SharedJSContext).
// Hand-authored (no build step). Reaches the friends popup via g_PopupManager and
// applies the two DOM tweaks CSS can't do:
//   1) move the voice call control into the .chatHeader bar (top of chat pane)
//   2) set a "Message <friend>…" placeholder on the composer
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

  function apply(doc) {
    try {
      // 1) voice control -> header bar
      doc.querySelectorAll(".chatWindow").forEach(function (win) {
        var header = win.querySelector(".chatHeader");
        var voice = win.querySelector(".ChatMessageEntryVoice");
        if (header && voice && !header.contains(voice)) header.appendChild(voice);
      });
      // 2) placeholder
      var name = ((doc.title || "").split(" - ")[1] || "").replace(/ \+ \d+ Chats?$/, "");
      doc.querySelectorAll(".chatEntry textarea").forEach(function (ta) {
        if (!ta.placeholder) ta.placeholder = name ? "Message " + name + "…" : "Message…";
      });
    } catch (e) {}
  }

  function tick() {
    friendsDocs().forEach(apply);
  }

  // Poll: cheap, and naturally handles popup open/close, chat switches, re-renders.
  setInterval(tick, 500);
  tick();
})();

export default function () {}
