// Discord-ish Chat Helper — runs in Steam's webkit pages via Millennium.
// Two DOM tweaks CSS can't do:
//   1) move the voice call control into the .chatHeader bar (top of chat pane)
//   2) give the message box a "Message <friend>…" placeholder
// Idempotent + observes DOM so it survives chat switches / re-renders.
(function () {
  function friendName() {
    // window title is "Friends List - <friend>"; fall back to active tab label
    var t = (document.title || "").split(" - ");
    if (t.length > 1 && t[1]) return t[1];
    var el = document.querySelector(".chatTabList .nOdcT-MoOaXGePXLyPe0H");
    return el ? el.textContent : "";
  }

  function apply() {
    // 1) voice -> header bar
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

  function start() {
    apply();
    var obs = new MutationObserver(function () { apply(); });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }

  // Only bother on the friends/chat window.
  if (document.querySelector(".friendsListContainer, .chatWindow") ||
      /Friends List/.test(document.title)) {
    if (document.readyState === "loading")
      document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})();
