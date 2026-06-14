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

  function chatFriendName(doc) {
    return ((doc.title || "").split(" - ")[1] || "").replace(/ \+ \d+ Chats?$/, "");
  }

  // Find the friend object for the currently-open chat (matched by display name).
  function chatFriend(doc) {
    try {
      var name = chatFriendName(doc).toLowerCase();
      var fs = window.g_FriendsUIApp.m_FriendStore;
      return fs.all_friends.find(function (x) {
        var p = x.m_persona || {};
        return [x.m_strNickname, p.m_strPlayerName].some(function (n) {
          return n && ("" + n).toLowerCase() === name;
        });
      });
    } catch (e) { return null; }
  }

  // Invite the chat friend to watch your broadcast (Go Live). This is the
  // fallback path — Remote Play screen sharing below is the primary option.
  function inviteToWatch(doc) {
    try {
      var f = chatFriend(doc);
      if (!f || !f.m_persona || !f.m_persona.m_steamid) return;
      window.SteamClient.Broadcast.InviteToWatch(f.m_persona.m_steamid.ConvertTo64BitString());
    } catch (e) {}
  }

  // --- Screen share via Remote Play Together (primary) -----------------------
  // Steam only streams a *game*, and Remote Play Together lets a friend watch/
  // join one without owning it. So we host a throwaway RPT-capable game
  // (Spacewar, appid 480 — same trick RemotePlayWhatever uses), flip its group
  // to "stream the whole desktop", and invite the chat friend. Much lower latency
  // than broadcast (no capture-window re-encode); the friend sees your real
  // screen, not Spacewar.
  //
  // Two things that bit us (verified live): (1) on Linux Spacewar ships only a
  // Windows .exe, so it must run under Proton or it exits instantly; (2) an idle
  // RPT group (created on launch, nobody invited) auto-disbands within seconds,
  // and the JS RemotePlayStore never tracks it — so we must catch GroupCreated
  // and invite *immediately* via the raw SteamClient.RemotePlay API + groupID.
  var SPACEWAR = "480";
  var IS_WIN = /win/i.test((window.navigator && (navigator.platform || navigator.userAgent)) || "");
  function rpStore() { return window.g_FriendsUIApp && window.g_FriendsUIApp.RemotePlayStore; }
  function rpRaw() { return window.SteamClient && window.SteamClient.RemotePlay; }
  function ensureSpacewarProton() {
    // Spacewar is Windows-only; force a Proton so it stays running on Linux.
    if (IS_WIN || window.__ds_spacewar_proton) return;
    try { window.SteamClient.Apps.SpecifyCompatTool(480, "proton_experimental"); window.__ds_spacewar_proton = true; } catch (e) {}
  }
  function shareScreenRP(doc) {
    try {
      var friend = chatFriend(doc);
      if (!friend || !friend.m_persona || !friend.m_persona.m_steamid) { console.warn("[ds] screen-share: no friend for this chat"); return; }
      var steam64 = friend.m_persona.m_steamid.ConvertTo64BitString();
      var RP = rpRaw();
      if (!RP) { console.warn("[ds] screen-share: SteamClient.RemotePlay missing"); return; }

      // Arm BEFORE launch: the group only lives for a few idle seconds, so we
      // invite the instant Spacewar's launch creates it.
      var done = false, reg = null;
      var finish = function () { if (reg) { try { reg.unregister(); } catch (e) {} reg = null; } };
      reg = RP.RegisterForGroupCreated(function (groupID, hostSteam, gameid) {
        if (done || String(gameid) !== SPACEWAR) return;   // ignore unrelated groups
        done = true;
        window.__ds_rp_group = groupID;
        try { RP.SetStreamingDesktopToRemotePlayTogetherEnabled(groupID, true); } catch (e) { console.warn("[ds] desktop-stream toggle", e); }
        try { RP.CreateInviteAndSession(groupID, steam64, false); } catch (e) { console.warn("[ds] invite", e); }
        finish();
      });
      setTimeout(function () { if (!done) { finish(); console.warn("[ds] screen-share: Spacewar never created an RPT group (installed? Proton set?)"); } }, 30000);

      ensureSpacewarProton();
      try { window.SteamClient.Apps.RunGame(SPACEWAR, "", -1, 100); } catch (e) { console.warn("[ds] launch Spacewar", e); }
    } catch (e) { console.warn("[ds] shareScreenRP", e); }
  }
  function stopShareRP() {
    try { var s = rpStore(); if (s && s.CancelAllInvitesAndSessions) s.CancelAllInvitesAndSessions(); } catch (e) {}
    try { window.SteamClient.Apps.TerminateApp(SPACEWAR, false); } catch (e) {}
    window.__ds_rp_group = null;
  }

  function chatTweaks(doc) {
    doc.querySelectorAll(".chatWindow").forEach(function (win) {
      var header = win.querySelector(".chatHeader");
      var voice = win.querySelector(".ChatMessageEntryVoice");
      if (header && voice && !header.contains(voice)) header.appendChild(voice);
      // screen-share / stream menu in the chat header
      if (header && !header.querySelector(".ds-share-wrap")) {
        var wrap = el(doc, "div", "ds-share-wrap");
        var sb = el(doc, "button", "ds-share");
        sb.title = "Stream — quality + invite to watch your broadcast";
        sb.textContent = "🖥";
        var menu = el(doc, "div", "ds-stream-menu");
        menu.style.display = "none";

        var setSetting = function (k, v) { try { window.SteamClient.Settings.SetSetting(k, v); } catch (e) {} };
        var streamSelect = function (label, opts, onPick) {
          var row = el(doc, "div", "ds-vs-row");
          var sp = el(doc, "span", "ds-vs-label"); sp.textContent = label;
          var sel = el(doc, "select", "ds-vs-select");
          opts.forEach(function (o, i) { var op = doc.createElement("option"); op.value = i; op.textContent = o[0]; sel.appendChild(op); });
          sel.addEventListener("change", function () { onPick(opts[+sel.value][1]); });
          row.appendChild(sp); row.appendChild(sel); menu.appendChild(row);
        };

        // Primary: share your whole screen to the chat friend over Remote Play.
        var st = el(doc, "div", "ds-vs-title"); st.textContent = "Screen share"; menu.appendChild(st);
        var share = el(doc, "button", "ds-stream-go"); share.textContent = "Share my screen";
        share.title = "Stream your desktop to this friend via Remote Play (low latency)";
        share.addEventListener("click", function () { shareScreenRP(doc); menu.style.display = "none"; });
        menu.appendChild(share);
        var stop = el(doc, "button", "ds-stream-stop"); stop.textContent = "Stop sharing";
        stop.addEventListener("click", function () { stopShareRP(); });
        menu.appendChild(stop);

        // Fallback: classic broadcast (Go Live) + watch invite, with quality knobs.
        var t = el(doc, "div", "ds-vs-title"); t.textContent = "Broadcast (fallback)"; menu.appendChild(t);
        streamSelect("Resolution", [["720p", [1280, 720]], ["1080p", [1920, 1080]], ["1440p", [2560, 1440]], ["4K", [3840, 2160]]], function (wh) {
          setSetting("broadcast_output_width", wh[0]); setSetting("broadcast_output_height", wh[1]);
        });
        streamSelect("Bitrate", [["Low", 2500], ["Medium", 5000], ["High", 8000], ["Max", 15000]], function (kbps) {
          setSetting("broadcast_bitrate", kbps);
        });
        var go = el(doc, "button", "ds-stream-go"); go.textContent = "Invite to watch broadcast";
        go.addEventListener("click", function () { inviteToWatch(doc); menu.style.display = "none"; });
        menu.appendChild(go);

        sb.addEventListener("click", function () { menu.style.display = menu.style.display === "none" ? "block" : "none"; });
        wrap.appendChild(sb); wrap.appendChild(menu); header.appendChild(wrap);
      }
    });
    var name = chatFriendName(doc);
    doc.querySelectorAll(".chatEntry textarea").forEach(function (ta) {
      if (!ta.placeholder) ta.placeholder = name ? "Message " + name + "…" : "Message…";
    });
  }

  function el(doc, tag, cls) { var e = doc.createElement(tag); if (cls) e.className = cls; return e; }

  function vcStore() {
    var a = window.g_FriendsUIApp;
    return a && a.m_VoiceChatStore;
  }

  function buildControls(doc, stage) {
    var bar = el(doc, "div", "ds-controls");
    var refreshers = [];

    function addToggle(pop, label, getter, setter) {
      var row = el(doc, "label", "ds-vs-row");
      var sp = el(doc, "span", "ds-vs-label"); sp.textContent = label;
      var cb = doc.createElement("input"); cb.type = "checkbox"; cb.className = "ds-vs-toggle";
      cb.addEventListener("change", function () { var s = vcStore(); if (s) s[setter](cb.checked); });
      refreshers.push(function () { var s = vcStore(); if (s) cb.checked = !!s[getter](); });
      row.appendChild(sp); row.appendChild(cb); pop.appendChild(row);
    }
    function addSlider(pop, label, getter, setter) {
      var row = el(doc, "div", "ds-vs-row");
      var sp = el(doc, "span", "ds-vs-label"); sp.textContent = label;
      var sl = doc.createElement("input"); sl.type = "range"; sl.min = 0; sl.max = 100; sl.className = "ds-vs-slider";
      sl.addEventListener("input", function () {
        var s = vcStore(); if (!s) return;
        s[setter](s.ConvertSliderToGainValue ? s.ConvertSliderToGainValue(+sl.value) : +sl.value);
      });
      refreshers.push(function () {
        var s = vcStore(); if (!s) return;
        var g = s[getter]();
        sl.value = s.ConvertGainValueToSliderValue ? s.ConvertGainValueToSliderValue(g) : g;
      });
      row.appendChild(sp); row.appendChild(sl); pop.appendChild(row);
    }
    function addSelect(pop, label, getter, setter, kind) {
      var row = el(doc, "div", "ds-vs-row");
      var sp = el(doc, "span", "ds-vs-label"); sp.textContent = label;
      var sel = doc.createElement("select"); sel.className = "ds-vs-select";
      sel.addEventListener("change", function () { var s = vcStore(); if (s) s[setter](sel.value); });
      refreshers.push(function () {
        var s = vcStore(); if (!s) return;
        var cur = s[getter]();
        navigator.mediaDevices.enumerateDevices().then(function (devs) {
          sel.textContent = "";
          devs.filter(function (d) { return d.kind === kind; }).forEach(function (d) {
            var o = doc.createElement("option");
            o.value = d.deviceId; o.textContent = d.label || d.deviceId;
            if (d.deviceId === cur) o.selected = true;
            sel.appendChild(o);
          });
        }).catch(function () {});
      });
      row.appendChild(sp); row.appendChild(sel); pop.appendChild(row);
    }

    // a control = action button (mute/deafen/leave) + optional settings dropdown
    function ctrlGroup(key, label, srcSel, buildPop) {
      var group = el(doc, "div", "ds-ctrl-group");
      var b = el(doc, "button", "ds-btn ds-" + key);
      b.title = label; b.dataset.src = srcSel;
      b.addEventListener("click", function () {
        var orig = doc.querySelector(".activeVoiceButtons " + srcSel) || doc.querySelector(srcSel);
        if (orig) orig.click();
      });
      group.appendChild(b);
      if (buildPop) {
        var pop = el(doc, "div", "ds-voice-settings");
        pop.style.display = "none";
        buildPop(pop);
        group.appendChild(pop);
        var caret = el(doc, "button", "ds-caret"); caret.textContent = "˅"; caret.title = label + " settings";
        caret.addEventListener("click", function (e) {
          e.stopPropagation();
          [].forEach.call(stage.querySelectorAll(".ds-voice-settings"), function (p) { if (p !== pop) p.style.display = "none"; });
          var show = pop.style.display !== "block";
          pop.style.display = show ? "block" : "none";
          if (show) refreshers.forEach(function (f) { try { f(); } catch (e) {} });
        });
        group.appendChild(caret);
      }
      bar.appendChild(group);
    }

    ctrlGroup("mic", "Mute", ".ToggleMicrophoneButton", function (pop) {
      var t = el(doc, "div", "ds-vs-title"); t.textContent = "Microphone"; pop.appendChild(t);
      addSelect(pop, "Device", "GetSelectedMic", "SetSelectedMic", "audioinput");
      addSlider(pop, "Input Volume", "GetVoiceInputGain", "SetVoiceInputGain");
      addToggle(pop, "Noise Cancellation", "GetUseNoiseCancellation", "SetUseNoiseCancellation");
      addToggle(pop, "Echo Cancellation", "GetUseEchoCancellation", "SetUseEchoCancellation");
      addToggle(pop, "Auto Gain Control", "GetUseAutoGainControl", "SetUseAutoGainControl");
    });
    ctrlGroup("out", "Deafen", ".ToggleVoiceOutputButton", function (pop) {
      var t = el(doc, "div", "ds-vs-title"); t.textContent = "Speaker"; pop.appendChild(t);
      addSelect(pop, "Device", "GetSelectedOutputDevice", "SetSelectedOutput", "audiooutput");
      addSlider(pop, "Output Volume", "GetVoiceOutputGain", "SetVoiceOutputGain");
    });
    ctrlGroup("leave", "Leave", ".chatEndVoiceChat", null);

    stage.appendChild(bar);
  }

  function isVisible(el) {
    if (!el) return false;
    var b = el.getBoundingClientRect();
    return b.width > 0 && b.height > 0;
  }

  function activeOneOnOneAcct() {
    var s = vcStore();
    try { return s && s.GetActiveOneOnOneVoiceChatAccountID ? s.GetActiveOneOnOneVoiceChatAccountID() : 0; }
    catch (e) { return 0; }
  }

  function callStage(doc) {
    // participant source: 1:1 call members OR group voice-channel participants
    // (both use the same .friend tile structure inside)
    var src = doc.querySelector(".OneOnOneVoiceMembers") || doc.querySelector(".VoiceChannelParticipants");
    var hasControls = !!doc.querySelector(".activeVoiceButtons");
    var inCall = !!src && hasControls;

    var wins = [].slice.call(doc.querySelectorAll(".chatWindow"));
    var win = wins.filter(function (w) { return w.getBoundingClientRect().width > 0; })[0];
    var main = win && win.querySelector(".ChatHistoryContainer");

    // Show the stage ONLY in the chat that owns the active call:
    //  - group call: visible window is the named group
    //  - 1:1 call: visible DM's friend == the active 1:1 voice friend
    var viewingCall = false;
    if (win) {
      if (win.classList.contains("namedGroup")) {
        viewingCall = true;
      } else {
        var acct = activeOneOnOneAcct();
        if (acct) {
          var name = chatFriendName(doc).toLowerCase();
          var fs = window.g_FriendsUIApp && window.g_FriendsUIApp.m_FriendStore;
          var f = fs && fs.all_friends.find(function (x) {
            var p = x.m_persona || {};
            return [x.m_strNickname, p.m_strPlayerName].some(function (n) { return n && ("" + n).toLowerCase() === name; });
          });
          if (f && f.m_unAccountID === acct) viewingCall = true;
        }
      }
    }
    var shouldShow = inCall && viewingCall && !!main;

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

    // mirror participant tiles from Steam's hidden list.
    var tiles = stage.querySelector(".discordish-tiles");
    var src_friends = [].slice.call(src.querySelectorAll(".friend"));
    // STRUCTURAL signature only (names + mute) — NOT speaking, which toggles
    // constantly and would rebuild (flicker) the whole tile set.
    var sig = src_friends.map(function (f) {
      var nameEl = f.querySelector(".nOdcT-MoOaXGePXLyPe0H");
      return (nameEl ? nameEl.textContent : "?") +
             (f.querySelector(".voiceStatusMic.disabled") ? "m" : "");
    }).join("|");
    if (tiles.dataset.sig !== sig) {
      tiles.dataset.sig = sig;
      tiles.textContent = "";
      src_friends.forEach(function (f) {
        var nameEl = f.querySelector(".nOdcT-MoOaXGePXLyPe0H");
        var img = f.querySelector("img.avatar");
        var tile = el(doc, "div", "ds-tile");
        var av = el(doc, "div", "ds-avatar");
        if (img && img.src) av.style.backgroundImage = "url(" + img.src + ")";
        // NOTE: no per-tile mute badge — ".voiceStatusMic.disabled" is present
        // even when unmuted, so it's not a reliable mute indicator.
        var nm = el(doc, "div", "ds-name");
        nm.textContent = nameEl ? nameEl.textContent : "";
        tile.appendChild(av);
        tile.appendChild(nm);
        tiles.appendChild(tile);
      });
    }
    // update speaking state IN PLACE every tick (no rebuild)
    var tileEls = tiles.children;
    for (var i = 0; i < tileEls.length && i < src_friends.length; i++) {
      tileEls[i].classList.toggle("speaking", src_friends[i].classList.contains("speaking"));
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

  setInterval(tick, 150);  // snappier mute/deafen + speaking updates
  tick();
})();

export default function () {}
