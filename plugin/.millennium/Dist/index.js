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

  // Invite the friend you're chatting with to watch your broadcast (screen share).
  // You start broadcasting (Go Live); this one click sends them the watch invite.
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
  // Send the right "join" request for whichever mode the user started:
  //  - broadcast  -> Steam's watch invite
  //  - remoteplay -> (re)launch the capture with the invite so RemotePlayWhatever
  //                  (which runs inside the capture process) sends the RPT request.
  function inviteToWatch(doc) {
    try {
      var sid = friendSteamID64(doc);
      if (!sid) return;
      if (window.__ds_share_mode === "remoteplay") {
        var o = window.__ds_capOpts || {};
        streamScreen({ screen: o.screen || "primary", scale: o.scale || "1920x1080", hidden: o.hidden, invite: sid });
      } else {
        window.SteamClient.Broadcast.InviteToWatch(sid);
      }
    } catch (e) {}
  }

  // Launch the screen-capture mirror as a non-Steam game so Steam can broadcast
  // it (= streaming a monitor/app; configured in ~/.config/discordish-capture.conf).
  // Capture is OS-specific. Linux = working; Windows = scaffold to finalize in a
  // Windows session (real repo path + powershell invocation + ffmpeg/ffplay on PATH).
  var IS_WIN = /win/i.test((window.navigator && (navigator.platform || navigator.userAgent)) || "");
  // Windows can't run a .ps1 as a Steam shortcut target, so the shortcut launches
  // powershell.exe and passes the script via -File. The installer rewrites
  // CAPTURE_SCRIPT below to the real repo-clone path. Linux runs the .sh directly.
  var POWERSHELL = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
  var CAPTURE_SCRIPT = IS_WIN
    ? "C:\\Users\\reedo\\discord-ish-steam\\stream-capture.ps1"  // installer rewrites to real clone path
    : "/home/reedo/steam-reskin/stream-capture.sh";
  var CAPTURE_EXE = IS_WIN ? POWERSHELL : CAPTURE_SCRIPT;
  // Linux x11grab geometry (WxH+X+Y) for the dev's rig. Windows auto-detects monitor
  // geometry at runtime in stream-capture.ps1, so no hardcoded pixels are needed there.
  var MONITORS = { primary: "3840x2160+0+0", secondary: "3840x2160+3840+0" };
  // Stop the broadcast from recording the mic — otherwise it grabs the mic device
  // away from voice chat (friends stop hearing you when the capture window is focused).
  // Your voice still goes over voice chat; only the broadcast's own mic track is off.
  function disableBroadcastMic() {
    try { window.SteamClient.Settings.SetSetting("broadcast_record_microphone", false); } catch (e) {}
  }
  function streamScreen(opts) {
    // opts: { screen:'primary'|'secondary', scale:'1920x1080'|'2560x1440'|'none', hidden:bool }
    try {
      disableBroadcastMic();
      var apps = window.SteamClient.Apps;
      var store = window.appStore;
      var scale = opts.scale || "1920x1080";
      var sel = opts.screen || "primary";
      // Windows: pass the monitor selector; the .ps1 auto-detects geometry. See stream-capture.ps1.
      // Linux: x11grab geometry from MONITORS + ffplay window placement (viewLeft).
      var viewLeft = sel === "primary" ? 3840 : 0;
      if (opts.hidden) viewLeft = 9000; // off the visible desktop (Linux only)
      var launchOpts = IS_WIN
        ? '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + CAPTURE_SCRIPT + '" ' + sel + " " + scale + (opts.invite ? " " + opts.invite : "")
        : (MONITORS[sel] || MONITORS.primary) + " " + scale + " " + viewLeft + " 0" + (opts.invite ? " " + opts.invite : "");
      var launch = function (appid) {
        try { apps.SetShortcutLaunchOptions(appid, launchOpts); } catch (e) {}
        setTimeout(function () {
          var ov = store.GetAppOverviewByAppID(appid);
          if (ov && ov.GetGameID) apps.RunGame(ov.GetGameID(), "", -1, 100);
        }, 350);
      };
      if (window.__ds_capture_appid && store.GetAppOverviewByAppID(window.__ds_capture_appid)) {
        launch(window.__ds_capture_appid);
      } else {
        apps.AddShortcut("Screen Stream", CAPTURE_EXE, "", CAPTURE_EXE).then(function (appid) {
          window.__ds_capture_appid = appid;
          launch(appid);
        });
      }
    } catch (e) {}
  }
  function stopStreamScreen() {
    try {
      var apps = window.SteamClient.Apps;
      // End any Remote Play Together session we started (hosted under Spacewar / AppID 480) —
      // it's owned by Steam, so terminating our capture process alone won't stop it.
      try {
        var rpid = window.__ds_rpt_groupid;
        if (rpid != null && window.SteamClient.RemotePlay && window.SteamClient.RemotePlay.CloseGroup) {
          window.SteamClient.RemotePlay.CloseGroup(rpid);
        }
      } catch (e) {}
      window.__ds_rpt_groupid = null;
      // Terminate the running capture (the borderless ffplay can't be closed otherwise).
      var appid = window.__ds_capture_appid;
      if (appid) {
        try {
          var ov = window.appStore && window.appStore.GetAppOverviewByAppID(appid);
          if (ov && ov.GetGameID) apps.TerminateApp(ov.GetGameID(), false);
        } catch (e) {}
        try { apps.RemoveShortcut(appid); } catch (e) {}
        window.__ds_capture_appid = 0;
      }
      window.__ds_share_mode = null;
    } catch (e) {}
  }

  // --- Native Remote Play Together (no RemotePlayWhatever, no capture window) --
  // Uses Steam's OWN RPT API — the exact calls Steam's "Remote Play Together"
  // button makes. Host Spacewar (480) so an RPT-eligible group is created, flip
  // it to stream the whole desktop, and invite the friend. On Linux Spacewar
  // ships only a Windows .exe, so force Proton or it exits instantly and the
  // idle group disbands before we can invite. The group is created async, and
  // RemotePlayStore.GetGroupForHostedGameID never tracks it, so we catch the
  // GroupCreated callback and invite with the raw groupID immediately.
  var SPACEWAR = "480";
  var RPCAP = "/home/reedo/steam-reskin/rp-capture.py";   // our capture/control server
  var RPCTL = "http://127.0.0.1:48591";                    // its localhost control API
  function rpRaw() { return window.SteamClient && window.SteamClient.RemotePlay; }

  // Low-latency single-monitor (or single-app) Remote Play share. We hijack
  // Spacewar's (480) launch so "launching Spacewar" actually runs our capture
  // server (rp-capture.py) instead of the game — Steam still tracks it as 480, so
  // it's RPT-eligible and Remote Play streams that window. The server stays alive
  // for the whole share and lets us switch monitor/app/resolution live (it just
  // restarts its ffplay child) WITHOUT dropping the RPT session. %command% (the
  // real game) is handed to bash -c as ignored args, so the game never runs.
  function shareRP(doc) {
    try {
      var sid = friendSteamID64(doc);
      if (!sid) { console.warn("[ds] RP: no friend for this chat"); return; }
      if (IS_WIN) { console.warn("[ds] RP one-monitor capture is Linux-only for now"); return; }
      var RP = rpRaw(), A = window.SteamClient.Apps;
      if (!RP) { console.warn("[ds] RP: SteamClient.RemotePlay missing"); return; }
      var cap = window.__ds_capOpts || { screen: "primary", scale: "1920x1080" };
      var geom = MONITORS[cap.screen] || MONITORS.primary;
      var res = (cap.scale && cap.scale !== "none") ? cap.scale : "none";
      var hijack = "bash -c 'exec python3 " + RPCAP + " " + geom + " " + res + "' %command%";
      try { A.ClearProton(480); } catch (e) {}                 // run our native server, not the Windows .exe
      try { A.SetAppLaunchOptions(480, hijack); } catch (e) {}

      var done = false, reg = null;
      var finish = function () { if (reg) { try { reg.unregister(); } catch (e) {} reg = null; } };
      reg = RP.RegisterForGroupCreated(function (groupID, hostSteam, gameid) {
        if (done || String(gameid) !== SPACEWAR) return;       // ignore unrelated groups
        done = true;
        window.__ds_rpt_groupid = groupID;                     // so "Stop sharing" can CloseGroup
        try { RP.CreateInviteAndSession(groupID, sid, false); } catch (e) { console.warn("[ds] invite", e); }
        finish();
      });
      setTimeout(function () { if (!done) { finish(); console.warn("[ds] RP: Spacewar never created an RPT group (installed? launch-opts ok?)"); } }, 30000);
      window.__ds_share_mode = "remoteplay";
      try { A.RunGame(SPACEWAR, "", -1, 100); } catch (e) { console.warn("[ds] launch Spacewar", e); }
    } catch (e) { console.warn("[ds] shareRP", e); }
  }
  function stopShareNative() {
    var RP = rpRaw();
    try { if (window.__ds_rpt_groupid != null && RP && RP.CloseGroup) RP.CloseGroup(window.__ds_rpt_groupid); } catch (e) {}
    window.__ds_rpt_groupid = null;
    try { window.SteamClient.Apps.TerminateApp(SPACEWAR, false); } catch (e) {}    // SIGTERMs rp-capture.py (stops ffplay)
    try { window.SteamClient.Apps.SetAppLaunchOptions(480, ""); } catch (e) {}     // restore Spacewar's launch options
  }
  // Live control of the running capture server (switch source / resolution without
  // dropping the RPT session). Reachable because Chromium lets https pages fetch
  // http://127.0.0.1 (localhost is "potentially trustworthy").
  function rpSources(cb) {
    fetch(RPCTL + "/sources", { cache: "no-store" }).then(function (r) { return r.json(); }).then(cb).catch(function () { cb(null); });
  }
  function rpSetSource(geom) {
    var cap = window.__ds_capOpts || {};
    var res = (cap.scale && cap.scale !== "none") ? cap.scale : "none";
    fetch(RPCTL + "/set?geom=" + encodeURIComponent(geom) + "&res=" + encodeURIComponent(res), { cache: "no-store" }).catch(function () {});
  }
  function rpSetRes(res) {
    fetch(RPCTL + "/res?v=" + encodeURIComponent(res), { cache: "no-store" }).catch(function () {});
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

        // ---- Source & quality (used by both shares; live-switchable during RP) ----
        var st = el(doc, "div", "ds-vs-title"); st.textContent = "Source & quality"; menu.appendChild(st);
        var capOpts = { screen: "primary", scale: "1920x1080" };
        window.__ds_capOpts = capOpts;
        streamSelect("Monitor", [["Primary", "primary"], ["Secondary", "secondary"]], function (v) {
          capOpts.screen = v;
          if (window.__ds_share_mode === "remoteplay") rpSetSource(MONITORS[v] || MONITORS.primary);   // live switch
        });
        streamSelect("Resolution", [["1080p", "1920x1080"], ["1440p", "2560x1440"], ["4K (native)", "none"]], function (v) {
          capOpts.scale = v;
          if (window.__ds_share_mode === "remoteplay") rpSetRes(v === "none" ? "none" : v);             // live switch
        });

        var nm = chatFriendName(doc) || "friend";
        var status = el(doc, "div", "ds-vs-label ds-stream-status");
        var setStatus = function (t) { status.textContent = t; };

        // Primary: low-latency, single monitor/app via our capture server.
        var rp = el(doc, "button", "ds-stream-go"); rp.textContent = "Remote Play " + nm + " · low latency";
        rp.title = "Streams the picked monitor (switchable live below) over Remote Play Together — low latency. They must accept the invite.";
        rp.addEventListener("click", function () {
          shareRP(doc);
          setStatus("Sent " + nm + " a Remote Play invite — they must accept. Switch source/quality below anytime.");
        });
        menu.appendChild(rp);

        // Live source switcher — monitors + app windows, from the running server.
        var srcWrap = el(doc, "div", "ds-src-list"); srcWrap.style.display = "none"; menu.appendChild(srcWrap);
        var srcBtn = el(doc, "button", "ds-stream-go"); srcBtn.textContent = "Switch monitor / app ▾";
        srcBtn.title = "While Remote Play is running: switch which monitor or app window is shared, live (no re-invite).";
        srcBtn.addEventListener("click", function () {
          if (srcWrap.style.display !== "none") { srcWrap.style.display = "none"; return; }
          srcWrap.textContent = "Loading sources…"; srcWrap.style.display = "block";
          rpSources(function (s) {
            srcWrap.textContent = "";
            if (!s) { srcWrap.textContent = "Capture server not running — start Remote Play first."; return; }
            (s.monitors || []).forEach(function (m) {
              var b = el(doc, "button", "ds-src-item"); b.textContent = "🖥 " + m.name + (m.primary ? " (primary)" : "");
              b.addEventListener("click", function () { rpSetSource(m.geom); setStatus("Now sharing monitor " + m.name + "."); });
              srcWrap.appendChild(b);
            });
            (s.windows || []).forEach(function (w) {
              var b = el(doc, "button", "ds-src-item"); b.textContent = "🪟 " + (w.title.length > 32 ? w.title.slice(0, 32) + "…" : w.title);
              b.addEventListener("click", function () { rpSetSource(w.w + "x" + w.h + "+" + w.x + "+" + w.y); setStatus("Now sharing app: " + w.title); });
              srcWrap.appendChild(b);
            });
            if (!(s.windows || []).length) {
              var note = el(doc, "div", "ds-vs-label"); note.textContent = "(app list needs wmctrl — run install.sh)"; srcWrap.appendChild(note);
            }
          });
        });
        menu.appendChild(srcBtn);

        // Fallback: Broadcast — works for any friend / any platform, but ~7s delay.
        var t = el(doc, "div", "ds-vs-title"); t.textContent = "Broadcast (fallback, ~7s)"; menu.appendChild(t);
        streamSelect("Bitrate", [["Low", 2500], ["Medium", 5000], ["High", 8000], ["Max", 15000]], function (kbps) {
          setSetting("broadcast_bitrate", kbps);
        });
        var bc = el(doc, "button", "ds-stream-go"); bc.textContent = "Broadcast " + nm + " · 1 monitor, ~7s";
        bc.title = "Broadcasts just the monitor picked above. Works for any friend, but ~7s delay.";
        bc.addEventListener("click", function () {
          window.__ds_share_mode = "broadcast";
          var sid = friendSteamID64(doc);
          streamScreen({ screen: capOpts.screen, scale: capOpts.scale });
          if (sid) { try { window.SteamClient.Broadcast.InviteToWatch(sid); } catch (e) {} }
          setStatus("Sent " + nm + " a BROADCAST watch request — monitor " + capOpts.screen + ", ~7s delay.");
        });
        menu.appendChild(bc);

        var stop = el(doc, "button", "ds-stream-stop"); stop.textContent = "Stop sharing";
        stop.addEventListener("click", function () { stopShareNative(); stopStreamScreen(); setStatus("Stopped sharing."); });
        menu.appendChild(stop);
        menu.appendChild(status);

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
  var VERSION = 8;
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
