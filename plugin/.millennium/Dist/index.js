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

  // (Single-monitor capture via server-config custom_display_device was removed: on
  //  Linux the streaming backend clears the value for every format tried AND enabling
  //  the server-config gate crashed the share. Whole-desktop only on Linux.)

  // Remote Play Together share flow.
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

      // CRITICAL: enabling desktop mode isn't enough — once the guest's remote client
      // actually connects we must call StartDesktopStream(clientId) to switch the stream
      // from the Spacewar GAME window to the DESKTOP. Without this the friend only sees
      // Spacewar. Register once; it fires whenever a remote client starts during a share.
      if (!window.__ds_rcs_reg && RP.RegisterForRemoteClientStarted) {
        window.__ds_rcs_reg = RP.RegisterForRemoteClientStarted(function (groupID, steam, gameid, clientId) {
          if (window.__ds_share_mode !== "remoteplay") return;
          console.log("[ds] remote client started -> StartDesktopStream", clientId);
          try { RP.StartDesktopStream(clientId); } catch (e) { console.warn("[ds] StartDesktopStream", e); }
        });
      }

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
        // Stream the WHOLE desktop — the friend sees your screen. Spacewar is only the
        // anchor that makes the RPT group exist.
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

  // === WebRTC screen share (replaces Steam Remote Play) =====================
  // The host's capture/encode/serve is done natively by rp-webrtc.py (ffmpeg + NVENC/
  // VAAPI/QSV/x264 auto-detected -> MediaMTX -> WebRTC/WHEP); CEF can't capture but CAN
  // render an incoming WebRTC stream in a <video>. Host: POST /start -> WHEP url. Viewer:
  // WHEP client -> <video> in the call tile. URL hand-off between peers is signaled out
  // of band (test: passed in; later: Steam chat / a share link).
  var WCTL = "http://127.0.0.1:48592";          // local daemon control API
  function wStartShare(src) {
    window.__ds_share_mode = "webrtc";
    // src = "win:0x.." (per-app, occlusion-proof) | "geom:WxH+X+Y" or bare "WxH+X+Y" (monitor)
    var qs = "";
    if (src && src.indexOf("win:") === 0) qs = "?win=" + encodeURIComponent(src.slice(4));
    else if (src) qs = "?geom=" + encodeURIComponent(src.indexOf("geom:") === 0 ? src.slice(5) : src);
    return fetch(WCTL + "/start" + qs, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        window.__ds_share_url = j && j.whep;                 // LAN/public url for the viewer
        window.__ds_share_ice = (j && j.ice) || null;        // TURN/STUN servers for the viewer
        window.__ds_share_ws = (j && j.ws) || null;
        window.__ds_share_local = "http://127.0.0.1:8889/screen/whep";   // loopback for self-preview
        window.__ds_share_info = j; return j;
      })
      .catch(function (e) { console.warn("[ds] wStartShare", e); return null; });
  }
  function wStopShare() {
    window.__ds_share_mode = null; window.__ds_share_url = null;
    fetch(WCTL + "/stop", { cache: "no-store" }).catch(function () {});
    if (window.__ds_pc) { try { window.__ds_pc.close(); } catch (e) {} window.__ds_pc = null; }
  }
  // Viewer: WHEP-negotiate `whepUrl` and render into <video> `v`. If the url is plain
  // http to a non-localhost host, route signaling through our local daemon proxy
  // (/whep?target=) so the secure-context page can reach it (mixed-content workaround).
  function wConnectShare(whepUrl, v, tries) {
    if (tries == null) tries = 12;          // the stream can take a couple seconds to go
    try {                                    // live (esp. per-app gst pipe) — keep retrying
      if (v.__pc) { try { v.__pc.close(); } catch (e) {} }
      // Use the ICE servers the host sent (Cloudflare TURN relay enables off-LAN viewers
      // behind hard NATs); fall back to plain STUN for LAN / older hosts.
      var iceServers = (window.__ds_view_ice && window.__ds_view_ice.length)
        ? window.__ds_view_ice : [{ urls: "stun:stun.l.google.com:19302" }];
      var pc = new RTCPeerConnection({ iceServers: iceServers });
      v.__pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.ontrack = function (e) {
        if (v.__wsLive) return;   // WS fallback already rendering; don't let WebRTC hijack srcObject (it wins over src)
        v.srcObject = e.streams[0];
        if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
        if (v.play) v.play().catch(function () {});
      };
      // Remote viewer with no TURN relay: ICE can't connect -> fall back to the WS tunnel
      // immediately instead of waiting out the safety timer.
      pc.oniceconnectionstatechange = function () {
        var s = pc.iceConnectionState;
        if ((s === "failed" || s === "disconnected") && v.__pc === pc && v.__dsGoWs) v.__dsGoWs();
      };
      // re-attempt while the source isn't producing yet; bail if a newer connect superseded us
      var retry = function (why) {
        try { pc.close(); } catch (e) {}
        if (tries > 0 && v.__pc === pc) {
          setTimeout(function () { if (v.__pc === pc && !(v.srcObject && v.srcObject.active)) wConnectShare(whepUrl, v, tries - 1); }, 800);
        } else { console.warn("[ds] wConnectShare gave up", why); }
      };
      pc.createOffer().then(function (o) { return pc.setLocalDescription(o); }).then(function () {
        return new Promise(function (r) {
          if (pc.iceGatheringState === "complete") return r();
          pc.onicegatheringstatechange = function () { if (pc.iceGatheringState === "complete") r(); };
          setTimeout(r, 2000);
        });
      }).then(function () {
        var localish = /^https:/.test(whepUrl) || /^https?:\/\/(127\.0\.0\.1|localhost)\b/.test(whepUrl);
        var url = localish ? whepUrl : (WCTL + "/whep?target=" + encodeURIComponent(whepUrl));
        return fetch(url, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription.sdp });
      }).then(function (res) { if (!res.ok) throw new Error("whep " + res.status); return res.text(); })
        .then(function (ans) {
          if (!ans || ans.indexOf("v=0") !== 0) throw new Error("empty answer");
          return pc.setRemoteDescription({ type: "answer", sdp: ans });
        })
        .catch(function (e) { retry(e && e.message || e); });
    } catch (e) { console.warn("[ds] wConnectShare", e); }
  }
  // exposed so the test harness (and later, chat-signaling) can start a viewer:
  window.__dsConnectShare = function (url) { window.__ds_view_url = url; };
  // Try WebRTC (wins on LAN); the moment ICE fails or ~2.5s pass with no frame, switch to
  // the WS/fMP4 tunnel. Remote viewers (no TURN) thus skip straight to WS, no black wait.
  function wConnectAuto(url, v) {
    if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
    if (v.__ws) { try { v.__ws.close(); } catch (e) {} v.__ws = null; }   // drop a stale WS before reconnecting
    v.__wsLive = false;
    var ws = window.__ds_view_ws;
    // Single fallback into the WS/fMP4 tunnel path. Idempotent: fires on whichever of
    // (WebRTC ICE failure) or (safety timer w/ no frame) happens first.
    var goWs = function () {
      if (v.__wentWs || !ws) return;
      v.__wentWs = true;
      if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
      // tear WebRTC down — nulling __pc also stops wConnectShare's retry loop (it guards on v.__pc === pc)
      try { if (v.__pc) { v.__pc.close(); v.__pc = null; } } catch (e) {}
      wConnectWsMse(ws, v);
    };
    v.__wentWs = false;
    v.__dsGoWs = goWs;            // wConnectShare invokes this the moment ICE fails (remote w/o TURN)
    wConnectShare(url, v);
    if (ws) {
      // No Cloudflare TURN => remote WebRTC has no relay and never connects. ICE failure
      // usually trips goWs in ~1-2s; this timer just backstops a "stuck in checking" peer.
      // videoWidth stays 0 until a real frame decodes (catches "negotiated but black" too).
      v.__dsAutoTimer = setTimeout(function () {
        v.__dsAutoTimer = null;
        if (!v.videoWidth) goWs();
      }, 2500);
    }
  }
  // Viewer (universal fallback): play fragmented-MP4 streamed over a WebSocket via MSE.
  // Works for any remote viewer through the cloudflared tunnel (no WebRTC/UDP needed).
  function wConnectWsMse(wsUrl, v) {
    try {
      if (!window.MediaSource) { console.warn("[ds] MSE unavailable"); return; }
      if (v.__ws) { try { v.__ws.close(); } catch (e) {} v.__ws = null; }
      // CRITICAL: srcObject takes precedence over src in the HTML media spec, so if a
      // prior (dead, frameless) WebRTC track left srcObject set, the MSE <video> stays
      // black forever. Clear it before handing the element to MediaSource.
      try { v.srcObject = null; } catch (e) {}
      if (v.src) { try { URL.revokeObjectURL(v.src); } catch (e) {} }
      var ms = new MediaSource();
      v.src = URL.createObjectURL(ms);
      v.addEventListener("loadeddata", function () { v.__wsLive = true; }, { once: true });
      var sb = null, queue = [];
      function pump() {
        if (!sb || sb.updating || !queue.length) return;
        var chunk = queue[0];
        try { sb.appendBuffer(chunk); queue.shift(); }
        catch (e) {
          if (e && e.name === "QuotaExceededError" && v.buffered.length) {
            try { sb.remove(0, Math.max(0, v.currentTime - 2)); } catch (e2) {}
            // updateend (from remove) re-fires pump; leave the chunk queued to retry
          } else { queue.shift(); }
        }
      }
      ms.addEventListener("sourceopen", function () {
        try {
          sb = ms.addSourceBuffer('video/mp4; codecs="avc1.640034"');   // High@5.2, covers up to 4K
          sb.mode = "sequence";
          sb.addEventListener("updateend", pump);
        } catch (e) { console.warn("[ds] addSourceBuffer", e); return; }
        var ws = new WebSocket(wsUrl);
        ws.binaryType = "arraybuffer";
        v.__ws = ws;
        ws.onmessage = function (ev) {
          queue.push(new Uint8Array(ev.data));
          if (queue.length > 240) queue.splice(0, queue.length - 240);   // bound memory
          pump();
          try {                                       // ride the live edge (keep latency low)
            if (v.buffered.length) {
              var end = v.buffered.end(v.buffered.length - 1);
              if (end - v.currentTime > 1.2) v.currentTime = end - 0.2;
            }
          } catch (e) {}
          if (v.paused && v.play) v.play().catch(function () {});
        };
        ws.onerror = function (e) { console.warn("[ds] ws err", e); };
      });
    } catch (e) { console.warn("[ds] wConnectWsMse", e); }
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
      cb.addEventListener("change", function () {
        var s = vcStore(); if (!s) return;
        s[setter](cb.checked);
        // apply to the LIVE mic — NC/echo/AGC ("voice isolation" in Steam's UI) only take
        // effect after the mic pipeline re-inits. ReinitMicSettings does exactly that
        // (lighter than restarting the whole voice chat, which didn't apply it).
        try { if (window.SteamClient.Settings.ReinitMicSettings) SteamClient.Settings.ReinitMicSettings(); } catch (e) {}
        try { if (s.SetupNoiseGateOnMic) s.SetupNoiseGateOnMic(); } catch (e) {}
      });
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
    shareB.title = "Screen share";
    // Discord-style screen-share glyph (monitor with an upward share arrow)
    shareB.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
      + '<path fill="currentColor" d="M4 2.5h16A2.5 2.5 0 0 1 22.5 5v10a2.5 2.5 0 0 1-2.5 2.5h-4.7l.9 2.3H17a1 1 0 1 1 0 2H7a1 1 0 1 1 0-2h1.3l.9-2.3H4A2.5 2.5 0 0 1 1.5 15V5A2.5 2.5 0 0 1 4 2.5Zm8 3.18-3.9 3.9A.9.9 0 0 0 8.74 11H10v2.36a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1V11h1.26a.9.9 0 0 0 .64-1.42L12 5.68Z"/>'
      + '</svg>';
    var spop = el(doc, "div", "ds-voice-settings"); spop.style.display = "none";
    var stitle = el(doc, "div", "ds-vs-title"); stitle.textContent = "Screen share"; spop.appendChild(stitle);
    // source picker: a monitor OR a specific app window (per-app share). Monitors capture a
    // screen region (x11grab); windows capture the window's own buffer by XID (gstreamer,
    // occlusion-proof). The selected token "geom:WxH+X+Y" or "win:0x.." goes into __ds_share_geom.
    var ssrcRow = el(doc, "div", "ds-vs-row");
    var ssrcLbl = el(doc, "span", "ds-vs-label"); ssrcLbl.textContent = "Source";
    var ssrc = el(doc, "select", "ds-vs-select");
    ssrc.addEventListener("change", function () { window.__ds_share_geom = ssrc.value; });
    ssrcRow.appendChild(ssrcLbl); ssrcRow.appendChild(ssrc); spop.appendChild(ssrcRow);
    var fillSources = function () {
      fetch(WCTL + "/sources", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
        ssrc.textContent = "";
        (j.monitors || []).forEach(function (m, i) {
          var o = doc.createElement("option"); o.value = "geom:" + m.geom;
          o.textContent = "Monitor " + (i + 1) + (m.primary ? " ★" : "") + " (" + m.name + ")"; ssrc.appendChild(o);
        });
        (j.windows || []).forEach(function (wn) {
          var o = doc.createElement("option"); o.value = "win:" + wn.id;
          o.textContent = "🪟 " + (wn.title.length > 32 ? wn.title.slice(0, 32) + "…" : wn.title); ssrc.appendChild(o);
        });
        if (window.__ds_share_geom) ssrc.value = window.__ds_share_geom;
        if (!ssrc.value && ssrc.options.length) ssrc.value = ssrc.options[0].value;
        window.__ds_share_geom = ssrc.value || window.__ds_share_geom;
      }).catch(function () {});
    };
    var sstatus = el(doc, "div", "ds-vs-label ds-stream-status"); spop.appendChild(sstatus);
    // self-preview: your own stream looped back via 127.0.0.1 (mixed-content-exempt, no
    // call/friend needed) so you can verify the share is live just by clicking Share.
    var sprev = el(doc, "video"); sprev.className = "ds-share-preview";
    sprev.autoplay = true; sprev.muted = true; sprev.playsInline = true; sprev.style.display = "none";
    spop.appendChild(sprev);
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
      var sharing = window.__ds_share_mode === "webrtc";
      shareB.classList.toggle("sharing", sharing);
      sgo.textContent = sharing ? "Stop sharing" : ("Share my screen to " + (chatFriendName(doc) || "friend"));
      sgo.classList.toggle("ds-stream-stop", sharing);
      sstatus.textContent = sharing
        ? ("Sharing live (" + ((window.__ds_share_info && window.__ds_share_info.encoder) || "?") + ") — preview below.")
        : "Streams a monitor to your friend — WebRTC, low latency, in-call.";
      // self-preview loopback so you can confirm the share works solo
      if (sharing) {
        sprev.style.display = "block";
        if (sprev.dataset.live !== "1" && window.__ds_share_local) { sprev.dataset.live = "1"; wConnectShare(window.__ds_share_local, sprev); }
      } else {
        sprev.style.display = "none"; sprev.dataset.live = "";
        if (sprev.__pc) { try { sprev.__pc.close(); } catch (e) {} sprev.__pc = null; }
      }
    };
    sgo.addEventListener("click", function () {
      if (window.__ds_share_mode === "webrtc") {
        try { sendSignal(doc, "stop"); } catch (e) {}     // tell the viewer to disconnect
        wStopShare(); setTimeout(srefresh, 60);
      } else {
        wStartShare(window.__ds_share_geom).then(function () {
          if (window.__ds_share_url) { try { sendSignal(doc, sigPayload(window.__ds_share_url, window.__ds_share_ice, window.__ds_share_ws)); } catch (e) {} }  // auto-signal the viewer (url + ICE)
          srefresh();
        });
      }
    });
    shareB.addEventListener("click", function (e) {
      e.stopPropagation();
      [].forEach.call(stage.querySelectorAll(".ds-voice-settings"), function (pp) { if (pp !== spop) pp.style.display = "none"; });
      var show = spop.style.display !== "block";
      spop.style.display = show ? "block" : "none";
      if (show) { fillSources(); srefresh(); }
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

    // When the call ends, stop any share we're hosting + drop any stream we're viewing.
    if (!inCall) {
      if (window.__ds_share_mode === "webrtc") { try { wStopShare(); } catch (e) {} }
      if (window.__ds_view_url) { window.__ds_view_url = null; if (window.__ds_pc) { try { window.__ds_pc.close(); } catch (e) {} window.__ds_pc = null; } }
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

    // Viewer: if we've been handed a share URL (signaled via chat, or set for testing),
    // show a "screen share" tile (the third person) with the live WebRTC <video> right
    // in it. Click to enlarge. This is real DOM video — no native window docking.
    var shareTile = tiles.querySelector(".ds-share-tile");
    if (window.__ds_view_url) {
      if (!shareTile) {
        shareTile = el(doc, "div", "ds-tile ds-share-tile");
        var vid = el(doc, "video"); vid.className = "ds-share-video";
        vid.autoplay = true; vid.muted = true; vid.playsInline = true;
        var lbl = el(doc, "div", "ds-name"); lbl.textContent = "🖥 Screen";
        shareTile.appendChild(vid); shareTile.appendChild(lbl);
        shareTile.addEventListener("click", function () {
          __ds_w.expanded = !__ds_w.expanded;
          stage.classList.toggle("ds-share-expanded", __ds_w.expanded);
        });
        tiles.appendChild(shareTile);
        shareTile.dataset.url = window.__ds_view_url;
        wConnectAuto(window.__ds_view_url, vid);
      } else if (shareTile.dataset.url !== window.__ds_view_url) {
        shareTile.dataset.url = window.__ds_view_url;
        wConnectAuto(window.__ds_view_url, shareTile.querySelector("video"));
      }
    } else if (shareTile) {
      var ov = shareTile.querySelector("video");
      if (ov) {
        if (ov.__dsAutoTimer) { clearTimeout(ov.__dsAutoTimer); ov.__dsAutoTimer = null; }
        if (ov.__ws) { try { ov.__ws.close(); } catch (e) {} ov.__ws = null; }
        if (ov.__pc) { try { ov.__pc.close(); } catch (e) {} ov.__pc = null; }
        try { ov.src = ""; ov.srcObject = null; ov.load(); } catch (e) {}
      }
      shareTile.remove();
      __ds_w.expanded = false;
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
    if (sc) sc.classList.toggle("sharing", window.__ds_share_mode === "webrtc");
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

  // === auto-signaling over Steam chat ======================================
  // The host sends "ds-screenshare::<url>" (or ::stop) to the friend over chat; the
  // viewer polls friend chats for it and sets window.__ds_view_url so the call tile
  // connects with no pasting. We hide these marker messages from the chat history.
  var SIG = "ds-screenshare::";
  window.__ds_sig_seen = window.__ds_sig_seen || {};   // per-friend last-processed ordinal
  // Payload = "<whep-url>" optionally followed by "|ice=<base64(JSON iceServers)>" so the
  // viewer gets the host's Cloudflare TURN credentials (needed to relay across NATs).
  function sigPayload(url, ice, ws) {
    var p = url;
    if (ice && ice.length) { try { p += "|ice=" + btoa(JSON.stringify(ice)); } catch (e) {} }
    if (ws) p += "|ws=" + encodeURIComponent(ws);
    return p;
  }
  function parseSignal(payload) {
    var out = { url: payload, ice: null, ws: null };
    var iceI = payload.indexOf("|ice=");
    var wsI = payload.indexOf("|ws=");
    var cut = Math.min(iceI < 0 ? payload.length : iceI, wsI < 0 ? payload.length : wsI);
    out.url = payload.slice(0, cut);
    if (iceI >= 0) {
      var end = (wsI > iceI) ? wsI : payload.length;
      try { out.ice = JSON.parse(atob(payload.slice(iceI + 5, end))); } catch (e) {}
    }
    if (wsI >= 0) {
      var wsEnd = (iceI > wsI) ? iceI : payload.length;
      try { out.ws = decodeURIComponent(payload.slice(wsI + 4, wsEnd)); } catch (e) {}
    }
    return out;
  }
  function friendAcctForDoc(doc) {
    try {
      var name = chatFriendName(doc).toLowerCase();
      var f = (window.g_FriendsUIApp.m_FriendStore.all_friends || []).find(function (x) {
        var p = x.m_persona || {};
        return [x.m_strNickname, p.m_strPlayerName].some(function (n) { return n && ("" + n).toLowerCase() === name; });
      });
      return f ? f.m_unAccountID : null;
    } catch (e) { return null; }
  }
  function sendSignal(doc, payload) {
    try {
      var acct = friendAcctForDoc(doc); if (acct == null) return;
      var fc = window.g_FriendsUIApp.m_ChatStore.GetFriendChat(acct);
      if (fc && fc.SendChatMessageInternal) fc.SendChatMessageInternal(SIG + payload);
    } catch (e) { console.warn("[ds] sendSignal", e); }
  }
  function pollSignals() {
    try {
      var app = window.g_FriendsUIApp, fs = app.m_FriendStore, cs = app.m_ChatStore;
      (fs.all_friends || []).forEach(function (f) {
        var fc = cs.GetFriendChat(f.m_unAccountID);
        var msgs = fc && fc.m_rgChatMessages;
        if (!msgs || !msgs.length) return;
        for (var i = msgs.length - 1; i >= 0 && i >= msgs.length - 6; i--) {
          var m = msgs[i];
          if (!m.strMessageInternal || m.strMessageInternal.indexOf(SIG) !== 0) continue;
          if (m.unAccountID !== f.m_unAccountID) continue;   // only act on what the FRIEND sent
          var ord = m.unOrdinal || i;
          if ((window.__ds_sig_seen[f.m_unAccountID] || 0) >= ord) break;   // already handled
          window.__ds_sig_seen[f.m_unAccountID] = ord;
          var payload = m.strMessageInternal.slice(SIG.length);
          if (payload === "stop") { window.__ds_view_url = null; window.__ds_view_ice = null; window.__ds_view_ws = null; }
          else { var p = parseSignal(payload); window.__ds_view_url = p.url; window.__ds_view_ice = p.ice; window.__ds_view_ws = p.ws; }
          break;
        }
      });
    } catch (e) {}
  }
  function hideSignalMessages(doc) {
    try {
      doc.querySelectorAll(".msg, [class*=Message]").forEach(function (el) {
        if (el.dataset.dsHidden) return;
        if ((el.textContent || "").indexOf(SIG) >= 0) { el.style.display = "none"; el.dataset.dsHidden = "1"; }
      });
    } catch (e) {}
  }

  // === Discord-style incoming/outgoing 1:1 call screen ======================
  // Driven by Steam's NATIVE ringing UI (.OneOnOneVoiceRoomControls in a .WaitingFor*
  // state). We hide that menu and mirror it as a Discord card in the call-stage area,
  // proxy-clicking its REAL buttons — .inviteButtonJoinVoice (accept) /
  // .inviteButtonDeclineVoice (decline) — because they actually work, unlike the store
  // method (JoinVoiceChatOrAsk... CALLS THE PERSON BACK instead of accepting).
  function ringUI(doc) {
    try {
      var win = [].slice.call(doc.querySelectorAll(".chatWindow")).filter(function (w) { return w.getBoundingClientRect().width > 0; })[0];
      var native = win && win.querySelector(".OneOnOneVoiceRoomControls");
      var ringing = native && /Waiting/.test(native.className || "");
      var existing = doc.querySelector(".ds-ring");
      if (!ringing) {                                   // not ringing -> tear down + un-hide native
        if (existing) existing.remove();
        if (native) native.classList.remove("ds-native-hidden");
        return;
      }
      var main = win.querySelector(".ChatHistoryContainer");
      if (!main) { if (existing) existing.remove(); return; }
      // incoming vs outgoing: the voice store's m_bInitiatedOneOnOneCall is true when WE
      // started the call (outgoing -> show Cancel only). The native join button is present
      // for outgoing too, so the old DOM check wrongly showed Accept/Decline to the caller.
      var incoming;
      try {
        var st = window.g_FriendsUIApp && window.g_FriendsUIApp.m_VoiceChatStore
                 && window.g_FriendsUIApp.m_VoiceChatStore.m_VoiceCallState;
        if (st && typeof st.m_bInitiatedOneOnOneCall === "boolean") incoming = !st.m_bInitiatedOneOnOneCall;
      } catch (e) {}
      if (incoming === undefined) incoming = !!native.querySelector(".inviteButtonJoinVoice");
      // Read name + avatar straight from the native ring (always correct, unlike the chat title)
      var nameEl = native.querySelector(".nOdcT-MoOaXGePXLyPe0H, [class*=VoiceStatusLab]");
      var name = (nameEl ? nameEl.textContent : "").replace(/\s*would like[\s\S]*$/i, "").trim() || chatFriendName(doc) || "Friend";
      var avatar = "";
      var imgs = native.querySelectorAll("img");
      for (var ii = 0; ii < imgs.length; ii++) { if (imgs[ii].src && imgs[ii].src.indexOf("data:") !== 0) { avatar = imgs[ii].src.replace("_medium", "_full"); break; } }
      if (!avatar) {   // native ring carries no <img> — resolve the avatar from the friend store via the call's target accountID
        try {
          var tAcct = st && st.m_targetAccountID;
          var fs = window.g_FriendsUIApp && window.g_FriendsUIApp.m_FriendStore;
          var fr = (fs && tAcct && fs.GetFriend) ? fs.GetFriend(tAcct) : null;
          if (!fr && fs && tAcct) fr = fs.all_friends.find(function (x) {
            var p = x.m_persona || {};
            return p.m_unAccountID === tAcct || p.accountid === tAcct ||
                   (p.m_steamid && p.m_steamid.GetAccountID && p.m_steamid.GetAccountID() === tAcct);
          });
          var per = fr && fr.m_persona;
          if (per) avatar = per.avatar_url_full || per.avatar_url_medium || avatar;
        } catch (e) {}
      }
      native.classList.add("ds-native-hidden");         // hide Steam's menu (kept clickable for proxy)
      var ring = main.querySelector(".ds-ring");
      if (existing && existing !== ring) existing.remove();
      if (!ring) {
        ring = el(doc, "div", "ds-ring");
        var av = el(doc, "div", "ds-ring-av");
        var nm = el(doc, "div", "ds-ring-name");
        var st = el(doc, "div", "ds-ring-sub");
        var btns = el(doc, "div", "ds-ring-btns");
        var acc = el(doc, "button", "ds-ring-accept"); acc.textContent = "✓ Accept";
        var dec = el(doc, "button", "ds-ring-decline"); dec.textContent = "✕ Decline";
        acc.addEventListener("click", function () {
          ring.__accepting = true; st.textContent = "Connecting…"; btns.style.display = "none";
          var b = native.querySelector(".inviteButtonJoinVoice"); if (b) b.click();   // proxy Steam's accept
        });
        dec.addEventListener("click", function () {
          // Outgoing (host): no native cancel button to proxy — end via the voice store's
          // user-action method OnUserEndVoiceChat() (the exact call Steam's own cancel runs).
          // Incoming: proxy Steam's decline button. Decide at click time from the store.
          try {
            var vcs = window.g_FriendsUIApp && window.g_FriendsUIApp.m_VoiceChatStore;
            var initiated = vcs && vcs.m_VoiceCallState && vcs.m_VoiceCallState.m_bInitiatedOneOnOneCall;
            if (initiated && vcs) {
              if (vcs.OnUserEndVoiceChat) vcs.OnUserEndVoiceChat();
              else if (vcs.OnUserLeaveOneOnOneVoiceChat) vcs.OnUserLeaveOneOnOneVoiceChat();
              else if (vcs.EndVoiceChatInternal) vcs.EndVoiceChatInternal(false);
            } else {
              var b = native.querySelector(".inviteButtonDeclineVoice"); if (b) b.click();
            }
          } catch (e) {
            try { var bb = native.querySelector(".inviteButtonDeclineVoice"); if (bb) bb.click(); } catch (e2) {}
          }
          ring.remove();
        });
        btns.appendChild(acc); btns.appendChild(dec);
        ring.appendChild(av); ring.appendChild(nm); ring.appendChild(st); ring.appendChild(btns);
        main.appendChild(ring);
      }
      ring.querySelector(".ds-ring-av").style.backgroundImage = avatar ? ("url(" + avatar + ")") : "";
      ring.querySelector(".ds-ring-name").textContent = name;
      if (!ring.__accepting) {   // don't clobber the "Connecting…" feedback
        ring.querySelector(".ds-ring-sub").textContent = incoming ? "Incoming call" : "Calling…";
        ring.querySelector(".ds-ring-accept").style.display = incoming ? "" : "none";
        ring.querySelector(".ds-ring-decline").textContent = incoming ? "✕ Decline" : "✕ Cancel";
      }
    } catch (e) {}
  }

  // One-click to open/switch a chat. Steam opens a conversation on DOUBLE-click of a
  // roster row; we want single-click (Discord-style). Synthetic events don't work (Steam
  // ignores untrusted clicks), so we call Steam's real open API directly:
  // g_FriendsUIApp.ShowFriendChatDialog(browserContext, accountId) — the same call the
  // native row's double-click runs. The row's accountId is read from its React fiber.
  function rowAccountId(node) {
    try {
      var key = Object.keys(node).find(function (k) {
        return k.indexOf("__reactFiber$") === 0 || k.indexOf("__reactInternalInstance$") === 0;
      });
      var fib = key ? node[key] : null;
      for (var hops = 0; fib && hops < 40; hops++, fib = fib.return) {
        var p = fib.memoizedProps;
        if (!p) continue;
        var cands = [p, p.friend, p.persona, p.user, p.friendAndPlaytime && p.friendAndPlaytime.friend];
        for (var i = 0; i < cands.length; i++) {
          var c = cands[i]; if (!c) continue;
          if (typeof c.accountid === "number") return c.accountid;
          if (typeof c.m_unAccountID === "number") return c.m_unAccountID;
          if (c.persona && typeof c.persona.accountid === "number") return c.persona.accountid;
          var sid = c.m_steamid || (c.persona && c.persona.m_steamid);
          if (sid && sid.GetAccountID) return sid.GetAccountID();
        }
      }
    } catch (e) {}
    return null;
  }

  function oneClickOpen(doc) {
    if (doc.__ds_oneclick) return;
    doc.__ds_oneclick = true;
    doc.addEventListener("click", function (e) {
      try {
        var t = e.target;
        if (!t || !t.closest) return;
        if (t.closest("button, [role=button], a, input, .contextMenuButton")) return;
        var row = t.closest(".friendlistListContainer .friend");
        if (!row || row.closest(".discordish-stage")) return;
        var acct = rowAccountId(row);
        var app = window.g_FriendsUIApp;
        if (!acct || !app) return;
        var ctx = (app.GetDefaultBrowserContext && app.GetDefaultBrowserContext())
               || (app.UIStore && app.UIStore.GetDefaultBrowserContext && app.UIStore.GetDefaultBrowserContext());
        if (app.ShowFriendChatDialog) app.ShowFriendChatDialog(ctx, acct, true, true);
        else if (app.UIStore && app.UIStore.ShowFriendChatDialog) app.UIStore.ShowFriendChatDialog(ctx, acct, true, true);
      } catch (err) {}
    }, false);
  }

  var __ds_tickn = 0;
  function tick() {
    __ds_tickn++;
    if (__ds_tickn % 7 === 0) { try { pollSignals(); } catch (e) {} }   // ~every 1s
    friendsDocs().forEach(function (doc) {
      try { injectCSS(doc); } catch (e) {}
      try { oneClickOpen(doc); } catch (e) {}
      try { chatTweaks(doc); } catch (e) {}
      try { ringUI(doc); } catch (e) {}
      try { callStage(doc); } catch (e) {}
      try { hideSignalMessages(doc); } catch (e) {}
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
    // Idempotent intervals: clear any from a prior init() (e.g. a live re-inject) so
    // tick loops never stack — duplicate loops race over share/signal state and break it.
    try { (window.__ds_intervals || []).forEach(clearInterval); } catch (e) {}
    window.__ds_intervals = [];
    fetchCSS();                                 // refresh CSS from the repo on boot
    window.__ds_intervals.push(setInterval(fetchCSS, 6 * 60 * 60 * 1000));  // periodic during long sessions
    window.__ds_intervals.push(setInterval(tick, 150));                     // snappier mute/deafen + speaking
    tick();
  }

  // Backend-free self-update: newer Millennium dropped Python backends (it uses Lua
  // now), so this plugin ships useBackend:false and updates itself from the repo.
  // CSS is refreshed by fetchCSS(); here we also fetch the latest index.js and, if its
  // VERSION is newer than ours, run that instead of this bundled copy (strip the
  // trailing ES module statement first — eval rejects module syntax). init() runs only
  // after this resolves, so we never double-initialise; falls back to bundled if offline.
  var VERSION = 47;
  try { window.__ds_VERSION = VERSION; } catch (e) {}
  var JS_URL = "https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/plugin/.millennium/Dist/index.js";
  if (!window.__DISCORDISH_BOOTED__) {
    window.__DISCORDISH_BOOTED__ = true;
    // ALWAYS boot the bundled copy first so the plugin can never be left disabled by a
    // bad/slow update. init() is idempotent (clears its own intervals), so a newer remote
    // copy can safely re-init over it. Update is best-effort and fully isolated.
    try { init(); } catch (e) { try { console.warn("[ds] init", e); } catch (e2) {} }
    try {
      fetch(JS_URL, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (src) {
          var m = src && src.match(/var VERSION = (\d+)/);
          if (m && +m[1] > VERSION) {
            window.__DISCORDISH_BOOTED__ = false;            // let the newer copy's boot run its init()
            try { (0, eval)(src.replace(/^export\s+default[\s\S]*$/m, "")); }
            catch (e) { window.__DISCORDISH_BOOTED__ = true; try { console.warn("[ds] self-update eval failed, staying on bundled v" + VERSION, e); } catch (e2) {} }
          }
        })
        .catch(function () {});
    } catch (e) {}
  }
})();

export default function () {}
