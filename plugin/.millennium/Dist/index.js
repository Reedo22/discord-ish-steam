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

  // SteamID64 of the friend whose chat is open (matched by display name).
  function friendSteamID64(doc) {
    try {
      var name = chatFriendName(doc).toLowerCase();
      var fs = window.g_FriendsUIApp.m_FriendStore;
      var f = fs.all_friends.find(function (x) {
        var p = x.m_persona || {};
        return [x.m_strNickname, p.m_strPlayerName].some(function (n) {
          return n && ("" + n).toLowerCase() === name;
        });
      });
      return (f && f.m_persona && f.m_persona.m_steamid) ? f.m_persona.m_steamid.ConvertTo64BitString() : null;
    } catch (e) { return null; }
  }
  var IS_WIN = /win/i.test((window.navigator && (navigator.platform || navigator.userAgent)) || "");
  // Monitor geometries (x11grab WxH+X+Y) for this rig — used by the Remote Play capture.
  var MONITORS = { primary: "3840x2160+0+0", secondary: "3840x2160+3840+0" };

  // --- Remote Play Together screen share (no RemotePlayWhatever) -------------
  // Host Spacewar (480) so an RPT-eligible group is created, then invite the
  // friend with Steam's own RPT API. The group is created async and the JS store
  // never tracks it, so we catch the GroupCreated callback and invite with the
  // raw groupID immediately (idle groups disband within seconds otherwise).
  var SPACEWAR = "480";
  var RPCAP = "/home/reedo/steam-reskin/rp-capture.py";   // our capture/control server (Linux path)
  var RPCTL = "http://127.0.0.1:48591";                    // its localhost control API (both OSes)
  // Windows: the plugin can't exec, so the hijacked-480 launch runs this PowerShell
  // launcher, which starts the capture server (rp-capture.py). No RemotePlayWhatever —
  // the invite is sent natively from JS, same as Linux. (installer rewrites the path).
  var WIN_POWERSHELL = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  var WIN_LAUNCHER = "C:\\Users\\reedo\\discord-ish-steam\\rp-capture-launch.ps1";
  function rpRaw() { return window.SteamClient && window.SteamClient.RemotePlay; }

  // PROVEN Remote Play Together share flow.
  //
  // We do NOT hijack Spacewar's launch anymore. The old approach pointed appid 480 at
  // a fake ffplay/gstreamer "game" so RPT would stream that window — but on Linux RPT
  // captures the game via its GL-presentation hook (gameoverlayrenderer/glXSwapBuffers),
  // which never grabs ffplay/glimagesink output, so the guest always saw BLACK. The fix
  // is to run the REAL Spacewar (480) under Proton as an RPT anchor (a genuine GL game
  // Steam can capture), then switch on whole-desktop streaming and invite the friend.
  //
  // Hard-won gotchas baked in below (each cost hours):
  //  - RunGame on an ALREADY-running app does NOT fire GroupCreated. To get a fresh
  //    group we must TerminateApp first, wait, then RunGame.
  //  - The groupID only arrives via the GroupCreated callback (the JS store never
  //    tracks it). Invite immediately — an idle group disbands within seconds.
  //  - NEVER minimize/hide the anchor window. A minimized window presents no frames
  //    and the stream silently never starts (the old rp-hide-spacewar.sh broke this).
  //  - VIEWER-side prereqs (see install.sh): the receiving machine's Steam
  //    streaming_client (appid 202355) needs libvpx.so.6 present system-wide (it links
  //    it; Ubuntu/Pop 24.04 ship libvpx9 only -> instant crash), and must NOT have
  //    Millennium LD_PRELOADed into it (libmillennium_hhx64.so makes it exit in ~1s).
  function shareRP(doc) {
    try {
      var sid = friendSteamID64(doc);
      if (!sid) { console.warn("[ds] RP: no friend for this chat"); return; }
      var RP = rpRaw();
      if (!RP) { console.warn("[ds] RP: SteamClient.RemotePlay missing"); return; }
      var A = window.SteamClient.Apps;
      window.__ds_share_mode = "remoteplay";

      // Fast path: the anchor was pre-warmed when the call started (prewarmAnchor) —
      // the group already exists, so just enable desktop streaming + invite. Instant.
      if (window.__ds_anchor_warm && window.__ds_rpt_groupid != null) {
        try { RP.SetStreamingDesktopToRemotePlayTogetherEnabled(window.__ds_rpt_groupid, true); } catch (e) {}
        try { RP.CreateInviteAndSession(window.__ds_rpt_groupid, sid, false); } catch (e) { console.warn("[ds] invite", e); }
        return;
      }

      // Cold path (no pre-warm): launch the anchor now.
      // Linux: ensure 480 runs the REAL game under Proton (the bundled binary is the
      // Windows .exe; without a compat tool it won't launch and no group is created).
      if (!IS_WIN) { try { A.SpecifyCompatTool(SPACEWAR, "proton_experimental"); } catch (e) {} }

      var done = false, reg = null;
      var finish = function () { if (reg) { try { reg.unregister(); } catch (e) {} reg = null; } };
      reg = RP.RegisterForGroupCreated(function (groupID, hostSteam, gameid) {
        if (done || String(gameid) !== SPACEWAR) return;        // ignore unrelated groups
        done = true;
        window.__ds_rpt_groupid = groupID;                       // so "Stop sharing" can CloseGroup
        // Stream the WHOLE desktop, not just the Spacewar window — the friend sees your
        // actual screen. Spacewar is only the anchor that makes the RPT group exist.
        try { RP.SetStreamingDesktopToRemotePlayTogetherEnabled(groupID, true); } catch (e) {}
        try { RP.CreateInviteAndSession(groupID, sid, false); } catch (e) { console.warn("[ds] invite", e); }
        finish();
      });
      setTimeout(function () { if (!done) { finish(); console.warn("[ds] RP: Spacewar never created an RPT group (installed? Proton set?)"); } }, 30000);

      // Force a FRESH group: terminate any running 480, wait for it to die, relaunch.
      try { A.TerminateApp(SPACEWAR, false); } catch (e) {}
      setTimeout(function () { try { A.RunGame(SPACEWAR, "", -1, 100); } catch (e) { console.warn("[ds] launch Spacewar", e); } }, 3000);
    } catch (e) { console.warn("[ds] shareRP", e); }
  }
  function stopShareNative() {
    var RP = rpRaw(), A = window.SteamClient.Apps;
    // CloseGroup's arity varies across Steam builds (some want (groupID), some 0 args);
    // try both, then terminate the anchor game which disbands the group regardless.
    try { if (RP && RP.CloseGroup) { try { RP.CloseGroup(window.__ds_rpt_groupid); } catch (e1) { try { RP.CloseGroup(); } catch (e2) {} } } } catch (e) {}
    window.__ds_rpt_groupid = null;
    window.__ds_anchor_warm = false; window.__ds_anchor_warming = false;
    try { A.TerminateApp(SPACEWAR, false); } catch (e) {}       // ends the anchor -> stream stops
    window.__ds_share_mode = null;
  }

  // Pre-warm the share anchor when a call starts so "Share" is near-instant — the slow
  // part is launching Spacewar under Proton. Launch it in the background and stash its
  // RPT groupID; shareRP's fast path then just enables desktop streaming + invites.
  // NOTE: while warmed, Steam shows you "Playing Spacewar" for the whole call — set your
  // Steam "Game details" privacy to Private to hide that (no per-game/API way exists).
  function prewarmAnchor() {
    if (window.__ds_anchor_warm || window.__ds_anchor_warming || window.__ds_share_mode === "remoteplay") return;
    var RP = rpRaw(); if (!RP) return;
    var A = window.SteamClient.Apps;
    window.__ds_anchor_warming = true;
    if (!IS_WIN) { try { A.SpecifyCompatTool(SPACEWAR, "proton_experimental"); } catch (e) {} }
    var reg = RP.RegisterForGroupCreated(function (groupID, hostSteam, gameid) {
      if (String(gameid) !== SPACEWAR) return;
      window.__ds_rpt_groupid = groupID;
      window.__ds_anchor_warm = true; window.__ds_anchor_warming = false;
      try { reg.unregister(); } catch (e) {}
    });
    try { A.TerminateApp(SPACEWAR, false); } catch (e) {}
    setTimeout(function () { try { A.RunGame(SPACEWAR, "", -1, 100); } catch (e) {} }, 3000);
  }
  // Live control of the running capture server (switch source / resolution without
  // dropping the RPT session). Reachable because Chromium lets https pages fetch
  // http://127.0.0.1 (localhost is "potentially trustworthy").
  function rpSources(cb) {
    fetch(RPCTL + "/sources", { cache: "no-store" }).then(function (r) { return r.json(); }).then(cb).catch(function () { cb(null); });
  }
  function rpRes() { var c = window.__ds_capOpts || {}; return (c.scale && c.scale !== "none") ? c.scale : "none"; }
  function rpSetSource(geom) {   // a monitor region
    fetch(RPCTL + "/set?geom=" + encodeURIComponent(geom) + "&res=" + encodeURIComponent(rpRes()), { cache: "no-store" }).catch(function () {});
  }
  function rpSetWindow(id) {       // a specific app window (id = X11 xid on Linux, title on Windows)
    fetch(RPCTL + "/set?win=" + encodeURIComponent(id) + "&res=" + encodeURIComponent(rpRes()), { cache: "no-store" }).catch(function () {});
  }
  function rpSetRes(res) {
    fetch(RPCTL + "/res?v=" + encodeURIComponent(res), { cache: "no-store" }).catch(function () {});
  }

  // --- Watcher side: dock the incoming Remote Play stream into the call UI ------
  // The RPT video is drawn by Steam's native streaming client, so it can't live
  // inside a DOM element. But the streaming client config exposes windowed + window
  // position/size (verified), so we force the stream out of fullscreen and park its
  // native window exactly over a "screen share" tile in the call stage — re-applying
  // each tick so it stays put ("as a third person", click the tile to enlarge).
  var __ds_w = { expanded: false, last: "", tick: 0 };
  function rpClientCfg() {
    try { return window.RemotePlayStore_SteamUI && window.RemotePlayStore_SteamUI.m_clientConfig; } catch (e) { return null; }
  }
  // Are we (the watcher) currently receiving a Remote Play stream?
  function isWatchingStream() {
    try {
      var s = window.RemotePlayStore_SteamUI; if (!s) return false;
      var cs = s.m_remoteClientStreams;
      var n = cs ? (Array.isArray(cs) ? cs.length : (cs.size != null ? cs.size : Object.keys(cs).length)) : 0;
      if (n > 0) return true;
      if (s.m_settings && s.m_settings.unStreamingSessionID) return true;
      if (s.BIsStreamingRemotePlayTogetherGame && s.BIsStreamingRemotePlayTogetherGame()) return true;
    } catch (e) {}
    return false;
  }
  // Force the native stream window windowed + positioned over `el` (screen coords).
  // Throttled: only re-pushes the config when the target rect actually moves (or
  // every ~2s) so we're not spamming SetStreamingClientConfig at 150ms.
  function dockStreamInto(el, doc) {
    var cc = rpClientCfg(); if (!cc || !el) return;
    try {
      var view = doc.defaultView || window;
      var r = el.getBoundingClientRect();
      var dpr = view.devicePixelRatio || 1;
      var x = Math.round((view.screenX + r.left) * dpr);
      var y = Math.round((view.screenY + r.top) * dpr);
      var w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
      if (w < 120 || h < 90) return;
      var sig = [x, y, w, h].join(",");
      __ds_w.tick++;
      if (sig === __ds_w.last && __ds_w.tick % 14 !== 0) return;   // unchanged + not the periodic re-assert
      __ds_w.last = sig;
      var RP = rpRaw(); if (!RP) return;
      cc.set_windowed(true);
      cc.set_window_position_x(x); cc.set_window_position_y(y);
      cc.set_window_width(w); cc.set_window_height(h);
      RP.SetStreamingClientConfig(cc);
      RP.SetStreamingClientConfigEnabled(true);   // best-effort; placement may apply even if the gate doesn't stick
    } catch (e) {}
  }

  function chatTweaks(doc) {
    // The single header bar is Steam's OWN tab strip (.chatTabList) — CSS styles it to
    // show only the active conversation (the friend's real pfp + name) and it stays the
    // native window-drag bar. We deliberately do NOT build a custom header bar: doing so
    // duplicated the bar, broke window dragging, and pulled the wrong (current-user)
    // avatar. The screen-share toggle lives in the in-call control bar (buildControls).
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
    // screen-share control + popover (sits with the call controls, before Leave).
    // Clicking the 🖥 opens a small panel: status, quality, and a start/stop button —
    // the button itself glows red while you're sharing. Whole-desktop Remote Play.
    var shareG = el(doc, "div", "ds-ctrl-group");
    var shareB = el(doc, "button", "ds-btn ds-share-ctrl");
    shareB.title = "Screen share"; shareB.textContent = "🖥";
    var spop = el(doc, "div", "ds-voice-settings"); spop.style.display = "none";
    var stitle = el(doc, "div", "ds-vs-title"); stitle.textContent = "Screen share"; spop.appendChild(stitle);
    var sstatus = el(doc, "div", "ds-vs-label ds-stream-status"); spop.appendChild(sstatus);
    // quality preset (best-effort — Steam exposes SetClientStreamingQuality)
    var qrow = el(doc, "div", "ds-vs-row");
    var qlbl = el(doc, "span", "ds-vs-label"); qlbl.textContent = "Quality";
    var qsel = el(doc, "select", "ds-vs-select");
    [["Automatic", 0], ["Fast", 1], ["Balanced", 2], ["Beautiful", 3]].forEach(function (o) {
      var op = doc.createElement("option"); op.value = o[1]; op.textContent = o[0]; qsel.appendChild(op);
    });
    qsel.addEventListener("change", function () { try { var R = rpRaw(); if (R && R.SetClientStreamingQuality) R.SetClientStreamingQuality(+qsel.value); } catch (e) {} });
    qrow.appendChild(qlbl); qrow.appendChild(qsel); spop.appendChild(qrow);
    var sgo = el(doc, "button", "ds-stream-go"); spop.appendChild(sgo);
    var srefresh = function () {
      var sharing = window.__ds_share_mode === "remoteplay";
      shareB.classList.toggle("sharing", sharing);
      sgo.textContent = sharing ? "Stop sharing" : ("Share my screen to " + (chatFriendName(doc) || "friend"));
      sgo.classList.toggle("ds-stream-stop", sharing);
      sstatus.textContent = sharing
        ? "Sharing your screen — they must accept the Remote Play invite."
        : "Streams your whole screen to this friend over Remote Play.";
    };
    sgo.addEventListener("click", function () {
      if (window.__ds_share_mode === "remoteplay") stopShareNative(); else shareRP(doc);
      setTimeout(srefresh, 50);
    });
    shareB.addEventListener("click", function (e) {
      e.stopPropagation();
      [].forEach.call(stage.querySelectorAll(".ds-voice-settings"), function (pp) { if (pp !== spop) pp.style.display = "none"; });
      var show = spop.style.display !== "block";
      spop.style.display = show ? "block" : "none";
      if (show) srefresh();
    });
    shareG.appendChild(shareB); shareG.appendChild(spop);
    bar.appendChild(shareG);

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

    // Pre-warm the share anchor as soon as a call starts so "Share" is near-instant.
    if (inCall) { try { prewarmAnchor(); } catch (e) {} }
    // When the call ends, tear everything down: close the RPT group + terminate the
    // Spacewar anchor (whether we were sharing OR just pre-warmed) so nothing lingers.
    if (!inCall && (window.__ds_share_mode === "remoteplay" || window.__ds_anchor_warm || window.__ds_anchor_warming)) {
      try { stopShareNative(); } catch (e) {}
    }

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

    // Watcher: when we're receiving a Remote Play stream, show a "screen share"
    // tile (the third person) and park the native stream window over it — over the
    // whole tile area when expanded. Re-docked each tick so it stays in the call UI.
    var shareTile = tiles.querySelector(".ds-share-tile");
    if (isWatchingStream()) {
      if (!shareTile) {
        shareTile = el(doc, "div", "ds-tile ds-share-tile");
        var lbl = el(doc, "div", "ds-name"); lbl.textContent = "🖥 Screen";
        var hint = el(doc, "div", "ds-share-hint"); hint.textContent = "click to enlarge";
        shareTile.appendChild(lbl); shareTile.appendChild(hint);
        shareTile.addEventListener("click", function () {
          __ds_w.expanded = !__ds_w.expanded;
          __ds_w.last = "";   // force an immediate re-dock at the new size
          stage.classList.toggle("ds-share-expanded", __ds_w.expanded);
        });
        tiles.appendChild(shareTile);
      }
      dockStreamInto(__ds_w.expanded ? tiles : shareTile, doc);
    } else if (shareTile) {
      shareTile.remove();
      __ds_w.expanded = false; __ds_w.last = "";
      stage.classList.remove("ds-share-expanded");
    }

    // sync control icons from Steam's real SVGs (also reflects mute/deafen state)
    stage.querySelectorAll(".ds-btn").forEach(function (b) {
      var sel = b.dataset.src;
      if (!sel) return;   // share button has no Steam source — keep its 🖥 glyph
      var orig = doc.querySelector(".activeVoiceButtons " + sel) || doc.querySelector(sel);
      var svg = orig && orig.querySelector("svg");
      if (svg && b.dataset.icon !== svg.outerHTML) {
        b.dataset.icon = svg.outerHTML;
        b.innerHTML = svg.outerHTML;
      }
    });
    // reflect live share state on the share control (red while sharing)
    var sc = stage.querySelector(".ds-share-ctrl");
    if (sc) sc.classList.toggle("sharing", window.__ds_share_mode === "remoteplay");
  }

  // Auto-update (no Python): pull the latest reskin CSS straight from the repo and
  // inject it into the friends window. Replaces the old Python backend's git-pull +
  // copy-to-quickcss. config/quick.css still provides the instant/offline baseline.
  var CSS_URL = "https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/theme/friends.custom.css";
  var _cssText = null;
  function fetchCSS() {
    try {
      fetch(CSS_URL, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (t) { if (t) { _cssText = t; friendsDocs().forEach(injectCSS); } })
        .catch(function () {});
    } catch (e) {}
  }
  function injectCSS(doc) {
    if (!_cssText) return;
    var st = doc.getElementById("discordish-css");
    if (!st) {
      st = doc.createElement("style");
      st.id = "discordish-css";
      (doc.head || doc.documentElement).appendChild(st);
    }
    if (st.textContent !== _cssText) st.textContent = _cssText;
  }

  function tick() {
    friendsDocs().forEach(function (doc) {
      try { injectCSS(doc); } catch (e) {}
      try { chatTweaks(doc); } catch (e) {}
      try { callStage(doc); } catch (e) {}
    });
  }

  function init() {
    // Track any RPT group id (first callback arg) so "Stop sharing" can CloseGroup()
    // it — a fallback alongside shareScreenNative's own per-share capture.
    try {
      if (!window.__ds_rpt_reg && window.SteamClient && window.SteamClient.RemotePlay && window.SteamClient.RemotePlay.RegisterForGroupCreated) {
        window.__ds_rpt_reg = window.SteamClient.RemotePlay.RegisterForGroupCreated(function (groupId) { window.__ds_rpt_groupid = groupId; });
      }
    } catch (e) {}
    fetchCSS();                                 // refresh CSS from the repo on boot
    setInterval(fetchCSS, 6 * 60 * 60 * 1000);  // and periodically during long sessions
    setInterval(tick, 150);                     // snappier mute/deafen + speaking updates
    tick();
  }

  // Backend-free self-update: newer Millennium dropped Python backends (it uses Lua
  // now), so this plugin ships useBackend:false and updates itself from the repo.
  // CSS is refreshed by fetchCSS(); here we also fetch the latest index.js and, if its
  // VERSION is newer than ours, run that instead of this bundled copy (strip the ES
  // `export default` first — eval rejects module syntax). init() runs only after this
  // resolves, so we never double-initialise; falls back to bundled code if offline.
  var VERSION = 21;
  var JS_URL = "https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/plugin/.millennium/Dist/index.js";
  if (!window.__DISCORDISH_BOOTED__) {
    window.__DISCORDISH_BOOTED__ = true;
    try {
      fetch(JS_URL, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (src) {
          var m = src && src.match(/var VERSION = (\d+)/);
          if (m && +m[1] > VERSION) {
            window.__DISCORDISH_BOOTED__ = false;            // let the newer copy boot itself
            (0, eval)(src.replace(/export\s+default[\s\S]*$/, ""));
          } else { init(); }
        })
        .catch(function () { init(); });
    } catch (e) { init(); }
  }
})();

export default function () {}
