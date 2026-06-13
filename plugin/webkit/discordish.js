// Discord-ish Chat Helper — injected into Steam webkit pages by Millennium.
// Two DOM tweaks CSS can't do:
//   1) move the voice call control into the .chatHeader bar (top of chat pane)
//   2) give the message box a "Message <friend>…" placeholder
// Robust: runs regardless of when injected; observes the DOM so it applies once
// the friends/chat UI renders and survives chat switches / re-renders.
(function () {
  // load marker (for diagnosing whether injection reached this page)
  window.__DISCORDISH_LOADED__ = (window.__DISCORDISH_LOADED__ || 0) + 1;

  function friendName() {
    var t = (document.title || "").split(" - ");
    if (t.length > 1 && t[1]) return t[1].replace(/ \+ \d+ Chats?$/, "");
    var el = document.querySelector(".chatTabList .nOdcT-MoOaXGePXLyPe0H");
    return el ? el.textContent : "";
  }

  function apply() {
    // 1) voice -> header bar (per visible chat window)
    document.querySelectorAll(".chatWindow").forEach(function (win) {
      var header = win.querySelector(".chatHeader");
      var voice = win.querySelector(".ChatMessageEntryVoice");
      if (header && voice && !header.contains(voice)) header.appendChild(voice);
    });
    // 2) placeholder
    var name = friendName();
    document.querySelectorAll(".chatEntry textarea").forEach(function (ta) {
      if (!ta.placeholder) ta.placeholder = name ? "Message " + name + "…" : "Message…";
    });
  }

  // Attach unconditionally — apply() no-ops on pages without chat elements,
  // and the observer catches the friends UI whenever it renders.
  function boot() {
    try { apply(); } catch (e) {}
    var obs = new MutationObserver(function () { try { apply(); } catch (e) {} });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }
  if (document.documentElement) boot();
  else document.addEventListener("DOMContentLoaded", boot);
})();
