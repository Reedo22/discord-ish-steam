// Discord-ish Chat Helper — Millennium frontend module (runs in SharedJSContext).
// Hand-authored (no build step). Reaches the friends popup via g_PopupManager.
//   1) move the voice control into the .chatHeader bar
//   2) "Message <friend>…" placeholder
//   3) Discord-style center-stage call screen (minimize/expand) — MIRRORS Steam's
//      voice UI (clone tiles + proxy-click controls) instead of moving its
//      React-owned nodes, which would duplicate them.
(function () {
  window.__DISCORDISH_LOADED__ = (window.__DISCORDISH_LOADED__ || 0) + 1;

  // ===== Our own STRONG noise cancellation (RNNoise/WASM) — portable, no virtual device =====
  // Steam captures the mic via CEF WebRTC getUserMedia. Its own NC only ever set the DEAD
  // goog*/moz* constraint flags (removed from modern Chromium = no-op), which is why the toggle
  // "does basically nothing". Rather than rely on CEF's mild built-in NS, we run RNNoise (the
  // engine Krisp-style NC is benchmarked on) as an AudioWorklet inserted into the mic stream at
  // the same getUserMedia interception point: get the raw mic -> WebAudio graph
  // (MediaStreamSource -> RNNoise worklet -> MediaStreamDestination) -> hand the PROCESSED stream
  // to Steam, which sends it over its peer connection. Pure JS/WASM in the voice store's context
  // -> identical on Windows/Linux/Mac, zero audio devices. Fail-safe: ANY error -> raw mic, so
  // voice never breaks. Toggle via localStorage 'ds_rnnoise' ('0' = off, falls back to fixing
  // Steam's native NS via standard constraints).
  (function patchMic() {
    if (window.__ds_nc_patched) return;
    var md = navigator.mediaDevices;
    if (!md || !md.getUserMedia) return;
    var orig = md.getUserMedia.bind(md);

    var RNN_NAME = "@sapphi-red/web-noise-suppressor/rnnoise";
    // vendored in our repo (primary; same fetch path as the live CSS) + CDN fallback.
    var SRC = [
      { worklet: "https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/plugin/.millennium/Dist/rnnoise/workletProcessor.js",
        wasm:    "https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/plugin/.millennium/Dist/rnnoise/rnnoise.wasm" },
      { worklet: "https://cdn.jsdelivr.net/npm/@sapphi-red/web-noise-suppressor@0.3.5/dist/rnnoise/workletProcessor.js",
        wasm:    "https://cdn.jsdelivr.net/npm/@sapphi-red/web-noise-suppressor@0.3.5/dist/rnnoise.wasm" }
    ];
    function rnnEnabled() { try { return localStorage.getItem("ds_rnnoise") !== "0"; } catch (e) { return true; } }

    // Map Steam's legacy {optional:[...]} audio constraint to a clean MODERN constraint. Steam
    // only emits the dead goog*/moz* NS/AGC spellings + {sourceId:<mic>} (legacy device key the
    // modern API ignores). We extract intent + remap sourceId->deviceId so the selected mic and
    // the user's EC/AGC/NS choices survive.
    function normAudio(a) {
      if (!a || a === true) return a;
      var want = {};
      var scan = function (o) {
        if (!o || typeof o !== "object") return;
        Object.keys(o).forEach(function (k) {
          var lk = k.toLowerCase(), v = o[k];
          if (lk.indexOf("noisesup") >= 0) want.noiseSuppression = !!v;
          else if (lk.indexOf("echocancellation") >= 0) want.echoCancellation = !!v;
          else if (lk.indexOf("autogaincontrol") >= 0) want.autoGainControl = !!v;
          else if (lk === "sourceid" || lk === "deviceid") { if (v) want.deviceId = v; }
        });
      };
      scan(a); scan(a.mandatory);
      if (Array.isArray(a.optional)) a.optional.forEach(scan);
      ["noiseSuppression", "echoCancellation", "autoGainControl", "deviceId", "sampleRate", "channelCount"]
        .forEach(function (k) { if (a[k] !== undefined && want[k] === undefined) want[k] = a[k]; });
      return want;
    }

    // Load the worklet module + wasm ONCE (cached promise). Tries each source in order; fetches
    // the worklet as text -> Blob URL (addModule needs a same-origin/CORS-clean URL).
    function rnnReady() {
      if (window.__ds_rnn_ready) return window.__ds_rnn_ready;
      window.__ds_rnn_ready = (async function () {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC || typeof AudioWorkletNode === "undefined") throw new Error("no AudioWorklet");
        var ctx = window.__ds_rnn_ctx || new AC({ sampleRate: 48000 });   // RNNoise is 48k
        window.__ds_rnn_ctx = ctx;
        var lastErr;
        for (var i = 0; i < SRC.length; i++) {
          try {
            var js = await (await fetch(SRC[i].worklet, { cache: "force-cache" })).text();
            var url = URL.createObjectURL(new Blob([js], { type: "text/javascript" }));
            await ctx.audioWorklet.addModule(url);
            try { URL.revokeObjectURL(url); } catch (e) {}
            var wasm = await (await fetch(SRC[i].wasm, { cache: "force-cache" })).arrayBuffer();
            if (!(wasm && wasm.byteLength > 1000)) throw new Error("bad wasm");
            console.log("[ds] RNNoise loaded (source #" + (i + 1) + ")");
            return { ctx: ctx, wasm: wasm };
          } catch (e) { lastErr = e; console.warn("[ds] RNNoise source #" + (i + 1) + " failed", e); }
        }
        throw lastErr || new Error("no RNNoise source reachable");
      })();
      return window.__ds_rnn_ready;
    }

    // keep at most one active graph; tear down the previous on a new capture / track end.
    function teardown() {
      var g = window.__ds_rnn_active; if (!g) return; window.__ds_rnn_active = null;
      try { g.src.disconnect(); } catch (e) {}
      try { g.node.port.postMessage("destroy"); } catch (e) {}
      try { g.node.disconnect(); } catch (e) {}
      try { g.raw.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    }

    async function processStream(raw) {
      var r = await rnnReady();
      var ctx = r.ctx;
      if (ctx.state === "suspended") { try { await ctx.resume(); } catch (e) {} }
      teardown();
      var src = ctx.createMediaStreamSource(raw);
      var node = new AudioWorkletNode(ctx, RNN_NAME, {
        processorOptions: { wasmBinary: r.wasm, maxChannels: 1 },
        channelCount: 1, channelCountMode: "explicit",
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]
      });
      var dest = ctx.createMediaStreamDestination();
      src.connect(node); node.connect(dest);
      window.__ds_rnn_active = { ctx: ctx, src: src, node: node, dest: dest, raw: raw };
      var out = new MediaStream();
      dest.stream.getAudioTracks().forEach(function (t) { out.addTrack(t); });
      raw.getVideoTracks().forEach(function (t) { out.addTrack(t); });
      out.getAudioTracks().forEach(function (t) { t.addEventListener("ended", teardown); });
      return out;
    }

    // Build the raw-capture constraints: when RNNoise is on, turn OFF the browser's own NS so the
    // two don't fight (double-processing = artifacts); keep EC/AGC. When off, normAudio's standard
    // keys at least make Steam's native NS actually engage.
    function rawConstraints(c) {
      var a = normAudio(c.audio);
      if (a && typeof a === "object" && rnnEnabled()) a.noiseSuppression = false;
      var cc = {}; for (var k in c) cc[k] = c[k]; cc.audio = a; return cc;
    }

    async function capture(c) {
      var wantAudio = !!(c && c.audio);
      var cc = wantAudio ? rawConstraints(c) : c;
      var raw = await orig(cc);
      if (wantAudio && rnnEnabled()) {
        try { return await processStream(raw); }
        catch (e) { console.warn("[ds] RNNoise unavailable -> raw mic (Steam native NS)", e); return raw; }
      }
      return raw;
    }

    md.getUserMedia = function (c) { return capture(c); };
    // Steam uses the LEGACY callback form: navigator.getUserMedia({...}, ok, err), assigned via
    // `navigator.getUserMedia || webkitGetUserMedia || ...` at call time -> seeding both wins it.
    var legacy = function (c, ok, err) { return capture(c).then(ok, err); };
    try { navigator.getUserMedia = legacy; } catch (e) {}
    try { navigator.webkitGetUserMedia = legacy; } catch (e) {}
    // expose a quick toggle for the console / future UI
    window.dsNoiseCancellation = function (on) { try { localStorage.setItem("ds_rnnoise", on ? "1" : "0"); } catch (e) {} return rnnEnabled(); };
    window.__ds_nc_patched = true;
    console.log("[ds] mic NC patch active; RNNoise " + (rnnEnabled() ? "ON (strong)" : "off -> native NS fix"));
  })();

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

  // === WebRTC screen share (replaces Steam Remote Play) =====================
  // The host's capture/encode/serve is done natively by rp-webrtc.py (ffmpeg + NVENC/
  // VAAPI/QSV/x264 auto-detected -> MediaMTX -> WebRTC/WHEP); CEF can't capture but CAN
  // render an incoming WebRTC stream in a <video>. Host: POST /start -> WHEP url. Viewer:
  // WHEP client -> <video> in the call tile. URL hand-off between peers is signaled out
  // of band (test: passed in; later: Steam chat / a share link).
  var WCTL = "http://127.0.0.1:48592";          // local daemon control API
  // share encode prefs (resolution/bitrate dropdowns), persisted across sessions
  window.__ds_share_opts = window.__ds_share_opts || (function () {
    // Defaults sized for a direct (UPnP/own-domain) path with AV1: 1440p60 @ 8 Mbps measured
    // ~= H.264 at 14+ Mbps. 40 Mbps-class home uploads carry this for 2-3 viewers.
    var d = { h: "1440", br: "8M", fps: "60", audio: "on", codec: "auto" };
    try {
      var s = JSON.parse(localStorage.getItem("ds_share_opts"));
      if (s) ["h", "br", "fps", "audio", "codec"].forEach(function (k) { if (s[k]) d[k] = s[k]; });   // fps/audio were saved but never restored
    } catch (e) {}
    return d;
  })();
  // Shown in the share popover when something goes wrong, instead of failing silently.
  var NO_DAEMON = "Screen-share helper isn't running. It starts with Steam — wait a few seconds and try again.";
  // Resolves to the daemon's /start reply when the share is live, or null on failure (with
  // window.__ds_share_error set to a human-readable reason). Never leaves share_mode set for
  // a share that didn't start — that used to signal a dead URL and give the viewer a black tile.
  function wStartShare(src) {
    // One start at a time, across BOTH loaded copies of this plugin (bundled + self-updated
    // share `window`): a second click or a duplicate handler used to fire a second /start that
    // killed the first share mid-setup.
    if (window.__ds_share_starting && window.__ds_share_promise) return window.__ds_share_promise;
    window.__ds_share_mode = "webrtc";
    window.__ds_share_starting = true;
    window.__ds_share_error = null;
    // src = "win:0x.." (per-app, occlusion-proof) | "geom:WxH+X+Y" or bare "WxH+X+Y" (monitor)
    var params = [];
    if (src && src.indexOf("win:") === 0) params.push("win=" + encodeURIComponent(src.slice(4)));
    else if (src && src.indexOf("cam:") === 0) params.push("cam=" + encodeURIComponent(src.slice(4)));
    else if (src) params.push("geom=" + encodeURIComponent(src.indexOf("geom:") === 0 ? src.slice(5) : src));
    // resolution/bitrate from the share-panel dropdowns (live encode overrides)
    var o = window.__ds_share_opts || {};
    params.push("h=" + encodeURIComponent(o.h && o.h !== "auto" ? o.h : "auto"));
    if (o.br) params.push("br=" + encodeURIComponent(o.br));
    if (o.fps) params.push("fps=" + encodeURIComponent(o.fps));
    params.push("audio=" + (o.audio === "off" ? "0" : "1"));
    params.push("codec=" + encodeURIComponent(o.codec || "auto"));
    var qs = params.length ? "?" + params.join("&") : "";
    return (window.__ds_share_promise = fetch(WCTL + "/start" + qs, { cache: "no-store" })
      .then(function (r) { return r.json(); }, function () { throw new Error(NO_DAEMON); })
      .then(function (j) {
        var cancelled = window.__ds_share_mode !== "webrtc";   // Cancel/Stop clicked while /start ran
        window.__ds_share_starting = false;
        if (cancelled) {
          if (j && j.ok) fetch(WCTL + "/stop", { cache: "no-store" }).catch(function () {});
          return null;
        }
        if (!j || !j.ok) throw new Error((j && j.error) || "Share failed to start");
        window.__ds_share_url = j && j.whep;                 // LAN/public url for the viewer
        window.__ds_share_ice = (j && j.ice) || null;        // TURN/STUN servers for the viewer
        window.__ds_share_ws = (j && j.ws) || null;
        window.__ds_share_audio = !!(j && j.audio);           // did audio actually make it in?
        window.__ds_share_codec = (j && j.codec) || "h264";   // what the host actually encodes
        window.__ds_share_lan = (j && j.lan_whep) || null;    // same-house viewers (no NAT hairpin)
        window.__ds_share_acodec = (j && j.acodec) || null;   // "opus" = WebRTC carries audio too
        window.__ds_share_local = "http://127.0.0.1:8889/screen/whep";   // loopback for self-preview
        window.__ds_share_info = j;
        window.__ds_cf_gen = null;              // baseline is taken by the watcher's first poll
        window.__ds_share_dead = 0;
        startShareWatch();
        return j;
      })
      .catch(function (e) {
        console.warn("[ds] wStartShare", e);
        var cancelled = window.__ds_share_mode !== "webrtc";
        window.__ds_share_starting = false;
        window.__ds_share_mode = null; window.__ds_share_url = null;
        window.__ds_share_error = cancelled ? null : ((e && e.message) || String(e));
        return null;
      }));
  }
  // Watches a live share via /ping (every 5s, only while sharing):
  //  - Cloudflare *quick* tunnels are unmanaged and do die mid-share; the daemon's watchdog
  //    rebuilds one, but the replacement has a NEW random hostname, so the viewer would sit
  //    reconnecting to the dead one. Re-signal the fresh URL over chat (silent — see
  //    patchSilenceSignals) when the daemon's cf_gen counter moves.
  //  - The capture can end underneath us (Wayland: "stop sharing" in the system tray; or the
  //    encoder/daemon died). Two dead polls in a row -> end the share properly: tell the
  //    viewer, un-red the button, say why. Previously the UI said "Sharing live" forever.
  function startShareWatch() {
    if (window.__ds_cf_watch) return;
    window.__ds_cf_watch = setInterval(function () {
      if (window.__ds_share_mode !== "webrtc") {
        clearInterval(window.__ds_cf_watch); window.__ds_cf_watch = null; return;
      }
      if (window.__ds_share_starting) return;            // a restart (option change) is mid-flight
      fetch(WCTL + "/ping", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (window.__ds_share_mode !== "webrtc" || window.__ds_share_starting) return;
          if (j && j.running === false) {
            if (++window.__ds_share_dead >= 2) shareEnded("Share stopped — the capture ended (stopped from the system tray, or it crashed).");
            return;
          }
          window.__ds_share_dead = 0;
          if (!j || typeof j.cf_gen !== "number") return;
          if (window.__ds_cf_gen === null || window.__ds_cf_gen === undefined) {
            window.__ds_cf_gen = j.cf_gen; return;          // first poll = baseline
          }
          if (j.cf_gen === window.__ds_cf_gen) return;
          window.__ds_cf_gen = j.cf_gen;
          window.__ds_share_url = j.whep || window.__ds_share_url;
          window.__ds_share_ws = j.ws || window.__ds_share_ws;
          window.__ds_share_lan = j.lan_whep || window.__ds_share_lan;
          console.warn("[ds] tunnel rebuilt -> re-signaling viewer", j.ws);
          sendSignalToAcct(window.__ds_sig_acct, currentSig());
        })
        .catch(function () {
          if (window.__ds_share_mode === "webrtc" && !window.__ds_share_starting &&
              ++window.__ds_share_dead >= 2) shareEnded("Share stopped — the screen-share helper exited.");
        });
    }, 5000);
  }
  function currentSig() {
    return sigPayload(window.__ds_share_url, window.__ds_share_ice, window.__ds_share_ws,
                      window.__ds_share_audio, window.__ds_share_codec, window.__ds_share_lan,
                      window.__ds_share_acodec);
  }
  function shareEnded(why) {
    console.warn("[ds] " + why);
    try { sendSignalToAcct(window.__ds_sig_acct, "stop"); } catch (e) {}
    wStopShare();
    window.__ds_share_error = why;
    try { if (window.__ds_share_refresh) window.__ds_share_refresh(); } catch (e) {}
  }
  function wStopShare() {
    window.__ds_share_mode = null; window.__ds_share_url = null; window.__ds_share_starting = false;
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
      // the host's WebRTC copy carries Opus desktop audio when it signaled ac=opus
      if (v.__wantAudio && window.__ds_view_audio && window.__ds_view_acodec === "opus")
        pc.addTransceiver("audio", { direction: "recvonly" });
      pc.ontrack = function (e) {
        if (v.__wsLive) return;   // WS fallback already rendering; don't let WebRTC hijack srcObject (it wins over src)
        // video and audio can arrive as separate streams: merge into one per connection
        if (v.__pcStreamOf !== pc) { v.__pcStreamOf = pc; v.__pcStream = new MediaStream(); }
        v.__pcStream.addTrack(e.track);
        if (v.srcObject !== v.__pcStream) v.srcObject = v.__pcStream;
        if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
        // WebRTC won the race: retire any WebSocket attempt running alongside it
        v.__rtcLive = true; v.__wentWs = true;
        if (v.__wsCleanup) { try { v.__wsCleanup(); } catch (e2) {} v.__wsCleanup = null; }
        v.__wsGen = (v.__wsGen || 0) + 1;
        if (v.__ws) { try { v.__ws.close(); } catch (e3) {} v.__ws = null; }
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
      // One offer, posted to each endpoint in turn: the host's public URL, then its LAN URL
      // (a same-house viewer can't reach the public address when the router doesn't
      // hairpin). Reusing the offer skips a second ICE gathering wait.
      var endpoints = [whepUrl];
      if (v.__whepAlt && v.__whepAlt !== whepUrl) endpoints.push(v.__whepAlt);
      var post = function (i) {
        var u = endpoints[i];
        var localish = /^https:/.test(u) || /^https?:\/\/(127\.0\.0\.1|localhost)\b/.test(u);
        var url = localish ? u : (WCTL + "/whep?target=" + encodeURIComponent(u));
        return fetch(url, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription.sdp })
          .then(function (res) { if (!res.ok) throw new Error("whep " + res.status); return res.text(); })
          .then(function (ans) { if (!ans || ans.indexOf("v=0") !== 0) throw new Error("empty answer"); return ans; })
          .catch(function (e) { if (i + 1 < endpoints.length) return post(i + 1); throw e; });
      };
      pc.createOffer().then(function (o) { return pc.setLocalDescription(o); }).then(function () {
        return new Promise(function (r) {
          if (pc.iceGatheringState === "complete") return r();
          pc.onicegatheringstatechange = function () { if (pc.iceGatheringState === "complete") r(); };
          setTimeout(r, 1000);   // host candidates are instant; don't hold the whole connect for STUN
        });
      }).then(function () { return post(0); })
        .then(function (ans) { if (v.__pc === pc) return pc.setRemoteDescription({ type: "answer", sdp: ans }); })
        .catch(function (e) { retry(e && e.message || e); });
    } catch (e) { console.warn("[ds] wConnectShare", e); }
  }
  // Try WebRTC (wins on LAN); the moment ICE fails or ~2.5s pass with no frame, switch to
  // the WS/fMP4 tunnel. Remote viewers (no TURN) thus skip straight to WS, no black wait.
  function wConnectAuto(url, v) {
    if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
    // retire any WS/MSE attempt (its handlers + pending retries check this generation)
    if (v.__wsCleanup) { try { v.__wsCleanup(); } catch (e) {} v.__wsCleanup = null; }
    v.__wsGen = (v.__wsGen || 0) + 1;
    if (v.__ws) { try { v.__ws.close(); } catch (e) {} v.__ws = null; }   // drop a stale WS before reconnecting
    v.__wsLive = false;
    var ws = window.__ds_view_ws;
    // Single fallback into the WS/fMP4 tunnel path. Idempotent: fires on whichever of
    // (WebRTC ICE failure) or (safety timer w/ no frame) happens first.
    // Starts the WS path ALONGSIDE WebRTC rather than instead of it: whichever renders first
    // wins (ontrack retires the WS; the WS's first frame retires WebRTC). Killing WebRTC here
    // used to abort a LAN connect seconds before its answer arrived, leaving the viewer on a
    // WS path that can't work there.
    var goWs = function () {
      if (v.__wentWs || v.__rtcLive || !ws) return;
      v.__wentWs = true;
      if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
      wConnectWsMse(ws, v);
    };
    v.__wentWs = false;
    v.__rtcLive = false;
    v.__whepAlt = window.__ds_view_lan || null;   // the other endpoint wConnectShare may try
    v.__wantAudio = true;                          // (the self-preview never asks for audio)
    v.__dsAuto = function () { if (window.__ds_view_url === url) wConnectAuto(url, v); };
    v.__dsGoWs = goWs;            // wConnectShare invokes this the moment ICE fails (remote w/o TURN)
    wConnectShare(url, v);
    if (ws) {
      // No Cloudflare TURN => remote WebRTC has no relay and never connects. ICE failure
      // usually trips goWs in ~1-2s; this timer just backstops a "stuck in checking" peer.
      // videoWidth stays 0 until a real frame decodes (catches "negotiated but black" too).
      v.__dsAutoTimer = setTimeout(function () {
        v.__dsAutoTimer = null;
        if (!v.videoWidth) goWs();
      }, v.__whepAlt ? 5000 : 2500);   // leave room for one public->LAN WHEP retry
    }
  }
  // Viewer (universal fallback): play fragmented-MP4 streamed over a WebSocket via MSE.
  // Works for any remote viewer through the cloudflared tunnel (no WebRTC/UDP needed).
  // Reconnect backoff for the WS/fMP4 viewer. A dead endpoint (host quit, tunnel gone)
  // used to be retried every 600ms forever; back off to ~6s so a long outage costs nothing,
  // and reset on any successful media so a normal capture restart still reconnects fast.
  function wsRetryDelay(v) {
    v.__wsTries = (v.__wsTries || 0) + 1;
    return Math.min(6000, 400 * Math.pow(1.7, v.__wsTries - 1));
  }
  function wConnectWsMse(wsUrl, v) {
    try {
      if (!window.MediaSource) { console.warn("[ds] MSE unavailable"); return; }
      if (v.__rtcLive) return;                       // WebRTC is already playing this share
      if (v.__ws) { try { v.__ws.close(); } catch (e) {} v.__ws = null; }
      // CRITICAL: srcObject takes precedence over src in the HTML media spec, so if a
      // prior (dead, frameless) WebRTC track left srcObject set, the MSE <video> stays
      // black forever. Clear it before handing the element to MediaSource.
      try { v.srcObject = null; } catch (e) {}
      if (v.src) { try { URL.revokeObjectURL(v.src); } catch (e) {} }
      // This function re-runs on every reconnect, so anything bound to the ELEMENT (not to
      // the MediaSource) has to be torn down first — otherwise each attempt leaves behind a
      // listener mutating a dead closure. `gen` supersedes stale handlers that still fire.
      if (v.__wsCleanup) { try { v.__wsCleanup(); } catch (e) {} }
      var gen = (v.__wsGen = (v.__wsGen || 0) + 1);
      var ms = new MediaSource();
      v.src = URL.createObjectURL(ms);
      var sb = null, queue = [], gone = false, ws0 = null, noMedia = null;
      // A fatal MSE/decode error (corrupt fragment, codec mismatch, GPU decoder reset)
      // kills playback WITHOUT closing the WebSocket, so ws.onclose never fires and the
      // tile stays black for the rest of the call. Detect it and rebuild from scratch.
      var restart = function (why) {
        if (gone || v.__wsGen !== gen) return;
        gone = true;
        if (noMedia) { clearTimeout(noMedia); noMedia = null; }
        console.warn("[ds] MSE restart:", why);
        if (v.__ws === ws0) v.__ws = null;
        try { if (ws0) ws0.close(); } catch (e) {}
        if (!window.__ds_view_ws) return;            // intentional stop — don't loop
        // Don't get stuck on the WebSocket path: it only knows the host's PUBLIC address,
        // which a viewer in the same house can't reach when the router doesn't hairpin,
        // while WebRTC there works fine over the LAN address. After a few failed WS tries,
        // start over from WebRTC (which alternates public <-> LAN endpoints).
        if (v.__rtcLive) return;
        if ((v.__wsTries || 0) >= 3 && v.__dsAuto) { v.__wsTries = 0; setTimeout(v.__dsAuto, 500); return; }
        setTimeout(function () {
          // v.__wsGen moves on if wConnectAuto restarted WebRTC meanwhile: don't clobber it
          if (window.__ds_view_ws && !v.__ws && v.__wsGen === gen) wConnectWsMse(window.__ds_view_ws, v);
        }, wsRetryDelay(v));
      };
      // Adaptive jitter buffer: `target` = seconds we keep buffered ahead of live. Grows on
      // stalls (bad link -> smoother, higher latency), decays when stable (good link -> low
      // latency). This "forces smooth" automatically without anyone touching a setting.
      var bufMin = 0.35, bufMax = 3.0, target = 0.5, lastStall = 0, lastDecay = 0;
      var onWaiting = function () {                   // playback underran -> need cushion
        target = Math.min(bufMax, target + 0.6);
        lastStall = Date.now();
      };
      var onVidErr = function () { restart("video error " + ((v.error || {}).code)); };
      var onLoaded = function () {
        v.__wsLive = true; v.__wsTries = 0;
        // WS won the race: stop WebRTC and its retry loop (it guards on v.__pc === pc)
        if (!v.__rtcLive && v.__pc) { try { v.__pc.close(); } catch (e) {} v.__pc = null; }
        if (noMedia) { clearTimeout(noMedia); noMedia = null; }
      };
      v.addEventListener("waiting", onWaiting);
      v.addEventListener("error", onVidErr);
      v.addEventListener("loadeddata", onLoaded);
      v.__wsCleanup = function () {
        v.removeEventListener("waiting", onWaiting);
        v.removeEventListener("error", onVidErr);
        v.removeEventListener("loadeddata", onLoaded);
        if (noMedia) { clearTimeout(noMedia); noMedia = null; }
      };
      // "Connected but nothing decodes" is the classic remote failure: a dead cloudflared
      // tunnel still completes the TCP/WS handshake, and a host whose encoder died sends no
      // fragments. Neither closes the socket, so without this the tile just sits black.
      noMedia = setTimeout(function () { restart("no media after 8s"); }, 8000);
      function pump() {
        if (gone || v.__wsGen !== gen) return;
        if (!sb || sb.updating || !queue.length) return;
        if (ms.readyState !== "open") { restart("mediasource " + ms.readyState); return; }
        var chunk = queue[0];
        try { sb.appendBuffer(chunk); queue.shift(); }
        catch (e) {
          if (e && e.name === "QuotaExceededError" && v.buffered.length) {
            try { sb.remove(0, Math.max(0, v.currentTime - 2)); } catch (e2) {}
            // updateend (from remove) re-fires pump; leave the chunk queued to retry
          } else if (e && e.name === "InvalidStateError") {
            restart("appendBuffer " + e.name);       // buffer detached — not recoverable here
          } else { queue.shift(); }
        }
      }
      ms.addEventListener("sourceopen", function () {
        if (gone || v.__wsGen !== gen) return;       // superseded before the source opened
        try {
          // include the AAC track in the codec string only when the host signaled audio,
          // else MSE waits forever for an audio track that never arrives.
          // video codec as signaled by the host (AV1 Main L5.1 or H.264 High@5.2 — both cover
          // 4K60); AAC only when the host signaled audio
          var vc = window.__ds_view_codec === "av1" ? "av01.0.13M.08" : "avc1.640034";
          var ac = window.__ds_view_acodec === "opus" ? "opus" : "mp4a.40.2";
          var codec = 'video/mp4; codecs="' + vc + (window.__ds_view_audio ? "," + ac + '"' : '"');
          if (MediaSource.isTypeSupported && !MediaSource.isTypeSupported(codec))
            console.warn("[ds] this client can't decode " + codec + " — ask the host to pick H.264");
          sb = ms.addSourceBuffer(codec);
          sb.mode = "sequence";
          sb.addEventListener("updateend", pump);
          sb.addEventListener("error", function () { restart("sourcebuffer error"); });
        } catch (e) { restart("addSourceBuffer " + (e && e.message || e)); return; }
        var ws = new WebSocket(wsUrl);
        ws0 = ws;
        ws.binaryType = "arraybuffer";
        v.__ws = ws;
        ws.onmessage = function (ev) {
          if (gone || v.__wsGen !== gen) { try { ws.close(); } catch (e) {} return; }
          queue.push(new Uint8Array(ev.data));
          if (queue.length > 240) queue.splice(0, queue.length - 240);   // bound memory
          pump();
          try {                                       // ride the edge, keeping ~`target` cushion
            if (v.buffered.length) {
              var end = v.buffered.end(v.buffered.length - 1);
              var ahead = end - v.currentTime, now = Date.now();
              // decay target back toward low-latency after 10s with no stall (throttled ~3s)
              if (now - lastStall > 10000 && now - lastDecay > 3000 && target > bufMin) {
                target = Math.max(bufMin, target - 0.15); lastDecay = now;
              }
              if (ahead > target + 0.5) v.currentTime = end - target;   // fell behind -> snap to cushion
              v.playbackRate = ahead < target * 0.6 ? 0.97 : 1.0;       // running low -> ease off to rebuild
            }
          } catch (e) {}
          if (v.paused && v.play) v.play().catch(function () {});
        };
        ws.onerror = function (e) { console.warn("[ds] ws err", e); };
        ws.onclose = function () {
          if (v.__ws !== ws) return;                 // superseded by a newer connection
          // the capture may have just restarted (live resolution/bitrate change), or the
          // host dropped us for falling too far behind — either way rebuild. Guard on
          // __ds_view_ws so an intentional stop doesn't loop. Backoff, so a dead host
          // isn't hammered every 600ms for the rest of the call.
          restart("ws closed");
        };
      });
    } catch (e) { console.warn("[ds] wConnectWsMse", e); }
  }
  // share-tile UI state (in-app enlarged view)
  var __ds_w = { expanded: false };

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
    // What to share: Screen | Window | Camera tabs (only kinds that exist), then which one.
    // Window sharing used to be one entry deep inside a dropdown and people never found it.
    var skinds = el(doc, "div", "ds-src-kinds"); spop.appendChild(skinds);
    var ssrcRow = el(doc, "div", "ds-vs-row");
    var ssrcLbl = el(doc, "span", "ds-vs-label"); ssrcLbl.textContent = "Screen";
    var ssrc = el(doc, "select", "ds-vs-select");
    var shint = el(doc, "span", "ds-src-hint"); shint.style.display = "none";
    ssrc.addEventListener("change", function () { window.__ds_share_geom = ssrc.value; });
    ssrcRow.appendChild(ssrcLbl); ssrcRow.appendChild(ssrc); ssrcRow.appendChild(shint); spop.appendChild(ssrcRow);
    var KINDS = [["geom", "Screen"], ["win", "Window"], ["cam", "Camera"]];
    var srcItems = { geom: [], win: [], cam: [] };
    var srcKind = ((window.__ds_share_geom || "geom:").split(":")[0]) || "geom";
    var renderSources = function () {
      if (!srcItems[srcKind] || !srcItems[srcKind].length) srcKind = "geom";
      skinds.textContent = "";
      KINDS.forEach(function (k) {
        if (!srcItems[k[0]].length) return;
        var b = el(doc, "button", "ds-src-kind" + (k[0] === srcKind ? " active" : ""));
        b.type = "button"; b.textContent = k[1];
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          if (srcKind === k[0]) return;
          srcKind = k[0]; window.__ds_share_geom = null; renderSources();
        });
        skinds.appendChild(b);
      });
      var items = srcItems[srcKind] || [];
      ssrc.textContent = "";
      items.forEach(function (it) {
        var o = doc.createElement("option"); o.value = it[0]; o.textContent = it[1]; ssrc.appendChild(o);
      });
      if (window.__ds_share_geom && items.some(function (it) { return it[0] === window.__ds_share_geom; }))
        ssrc.value = window.__ds_share_geom;
      else if (items.length) ssrc.value = items[0][0];
      window.__ds_share_geom = ssrc.value || null;
      ssrcLbl.textContent = { geom: "Screen", win: "Window", cam: "Camera" }[srcKind];
      // Wayland: apps can't list other apps' windows, so the desktop's picker chooses.
      var viaPicker = srcKind === "win" && items.length === 1 && items[0][0] === "win:portal";
      ssrc.style.display = viaPicker ? "none" : "";
      shint.style.display = viaPicker ? "" : "none";
      shint.textContent = viaPicker ? "You'll pick it in your desktop's share dialog" : "";
      try { srefresh(); } catch (e) {}
    };
    var fillSources = function () {
      fetch(WCTL + "/sources", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
        if (sstatus.dataset.err === "daemon") { sstatus.dataset.err = ""; srefresh(); }
        var clip = function (s) { return s.length > 36 ? s.slice(0, 36) + "…" : s; };
        srcItems = {
          geom: (j.monitors || []).map(function (m, i) { return ["geom:" + m.geom, "Monitor " + (i + 1) + (m.primary ? " ★" : "") + " · " + m.name]; }),
          win: (j.windows || []).map(function (wn) { return ["win:" + wn.id, clip(wn.title)]; }),
          cam: (j.cameras || []).map(function (cm) { return ["cam:" + cm.id, clip(cm.name)]; })
        };
        renderSources();
      }).catch(function () {
        if (window.__ds_share_mode === "webrtc") return;
        sstatus.textContent = NO_DAEMON; sstatus.dataset.err = "daemon";
      });
    };
    var sstatus = el(doc, "div", "ds-vs-label ds-stream-status"); spop.appendChild(sstatus);
    // self-preview: your own stream looped back via 127.0.0.1 (mixed-content-exempt, no
    // call/friend needed) so you can verify the share is live just by clicking Share.
    var sprev = el(doc, "video"); sprev.className = "ds-share-preview";
    sprev.autoplay = true; sprev.muted = true; sprev.playsInline = true; sprev.style.display = "none";
    spop.appendChild(sprev);
    // Resolution / bitrate / fps / audio: these drive OUR daemon encode (via /start?h=&br=&fps=
    // &audio=). Changing one mid-share re-calls /start (daemon restarts ffmpeg with the new
    // params, ~1s) and RE-SIGNALS the viewer so it re-syncs — important for the audio toggle
    // (the codec string changes avc1 <-> avc1+mp4a), harmless for the rest. The viewer's WS also
    // auto-reconnects. All live, NO Steam restart. (Re-signaling is silent — see patchSilenceSignals.)
    var opts = window.__ds_share_opts;
    var addShareSel = function (label, key, items) {
      var row = el(doc, "div", "ds-vs-row");
      var lb = el(doc, "span", "ds-vs-label"); lb.textContent = label;
      var sel = el(doc, "select", "ds-vs-select");
      items.forEach(function (it) { var op = doc.createElement("option"); op.value = it[1]; op.textContent = it[0]; sel.appendChild(op); });
      sel.value = opts[key];
      sel.addEventListener("change", function () {
        opts[key] = sel.value;
        try { localStorage.setItem("ds_share_opts", JSON.stringify(opts)); } catch (e) {}
        if (window.__ds_share_mode === "webrtc") {     // apply live: restart capture + re-signal viewer
          if (sstatus) sstatus.textContent = "Applying " + label + "…";
          wStartShare(window.__ds_share_geom).then(function (j) {
            if (sprev) sprev.dataset.live = "";
            try {
              if (j) sendSignal(doc, currentSig());
              else sendSignal(doc, "stop");          // restart failed: don't leave the viewer on a dead stream
            } catch (e) {}
            try { srefresh(); } catch (e) {}
          });
        }
      });
      row.appendChild(lb); row.appendChild(sel); spop.appendChild(row);
    };
    addShareSel("Resolution", "h", [["Auto", "auto"], ["4K", "2160"], ["1440p", "1440"], ["1080p", "1080"], ["720p", "720"], ["480p", "480"]]);
    addShareSel("Bitrate", "br", [["Potato (0.5M)", "0.5M"], ["Low (1M)", "1M"], ["Smooth (1.5M)", "1.5M"], ["Balanced (3M)", "3M"], ["Sharp (6M)", "6M"], ["Great (8M)", "8M"], ["Ultra (12M)", "12M"], ["Max (20M)", "20M"]]);
    addShareSel("FPS", "fps", [["15 fps", "15"], ["20 fps", "20"], ["30 fps", "30"], ["60 fps", "60"]]);
    addShareSel("Audio", "audio", [["On", "on"], ["Off", "off"]]);
    addShareSel("Codec", "codec", [["Auto (AV1 if your GPU can)", "auto"], ["H.264 (compatibility)", "h264"]]);
    var sgo = el(doc, "button", "ds-stream-go"); spop.appendChild(sgo);
    var srefresh = function () {
      var sharing = window.__ds_share_mode === "webrtc";
      var starting = sharing && window.__ds_share_starting;
      shareB.classList.toggle("sharing", sharing);
      sgo.textContent = starting ? "Cancel" : sharing ? "Stop sharing" : ("Share my screen to " + (chatFriendName(doc) || "friend"));
      sgo.classList.toggle("ds-stream-stop", sharing);
      sstatus.classList.toggle("ds-stream-error", !sharing && !!window.__ds_share_error);
      sstatus.textContent = starting
        ? "Starting… if a system screen picker pops up, choose what to share there."
        : sharing
        ? (function (i) {
            i = i || {};
            var route = i.via === "public" || i.direct ? "direct" : (i.via === "tunnel" ? "via relay" : "LAN");
            return "Sharing live — " + (i.codec === "av1" ? "AV1" : "H.264") + " (" + (i.encoder || "?") + "), " + route + ". Preview below.";
          })(window.__ds_share_info)
        : (window.__ds_share_error || ("Streams " + ({ win: "a window", cam: "your camera" }[srcKind] || "your screen") + " to your friend, peer-to-peer and encrypted."));
      // self-preview loopback so you can confirm the share works solo
      if (sharing) {
        sprev.style.display = "block";
        if (sprev.dataset.live !== "1" && window.__ds_share_local) { sprev.dataset.live = "1"; wConnectShare(window.__ds_share_local, sprev); }
      } else {
        sprev.style.display = "none"; sprev.dataset.live = "";
        if (sprev.__pc) { try { sprev.__pc.close(); } catch (e) {} sprev.__pc = null; }
      }
    };
    sgo.addEventListener("click", function (e) {
      // Two loaded copies of the plugin (bundled + self-updated) can both have a listener on
      // this button; the second one used to undo the first (start -> instantly Cancel/Stop).
      if (e && e.__dsHandled) return;
      if (e) e.__dsHandled = true;
      if (window.__ds_share_mode === "webrtc") {
        try { sendSignal(doc, "stop"); } catch (e) {}     // tell the viewer to disconnect
        wStopShare(); setTimeout(srefresh, 60);
      } else {
        var p = wStartShare(window.__ds_share_geom);
        srefresh();                                   // show "Starting…" while /start runs
        p.then(function (j) {
          if (j) { try { sendSignal(doc, currentSig()); } catch (e) {} }  // auto-signal the viewer (url + ICE + audio)
          srefresh();
        });
      }
    });
    shareB.addEventListener("click", function (e) {
      e.stopPropagation();
      [].forEach.call(stage.querySelectorAll(".ds-voice-settings"), function (pp) { if (pp !== spop) pp.style.display = "none"; });
      var show = spop.style.display !== "block";
      spop.style.display = show ? "block" : "none";
      if (show) { window.__ds_share_refresh = srefresh; fillSources(); srefresh(); }
    });
    window.__ds_share_refresh = srefresh;
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

  function docInCall(doc) {
    return !!(doc.querySelector(".OneOnOneVoiceMembers") || doc.querySelector(".VoiceChannelParticipants"))
        && !!doc.querySelector(".activeVoiceButtons");
  }
  function callStage(doc) {
    // participant source: 1:1 call members OR group voice-channel participants
    // (both use the same .friend tile structure inside)
    var src = doc.querySelector(".OneOnOneVoiceMembers") || doc.querySelector(".VoiceChannelParticipants");
    var hasControls = !!doc.querySelector(".activeVoiceButtons");
    var inCall = !!src && hasControls;

    // When the call ends, stop any share we're hosting + drop any stream we're viewing.
    // "Ends" = no friends window shows a call. Steam can have several open (the friends list
    // plus an undocked chat window); judging by THIS one alone stopped every share ~50 ms
    // after it started whenever the call lived in the other window.
    if (!inCall && !window.__ds_any_incall) {
      // tell the viewer too: in a group call they may still be connected, and an unsent
      // "stop" left the last share URL as the newest signal -> a dead tile next call
      if (window.__ds_share_mode === "webrtc") { try { sendSignalToAcct(window.__ds_sig_acct, "stop"); wStopShare(); } catch (e) {} }
      // Forget a received share only when a call we were IN ends — not while still ringing:
      // the host can start sharing before we answer, and clearing it here meant a friend who
      // picked up after "Share" was pressed never saw the stream.
      if (window.__ds_view_url && window.__ds_view_incall) { window.__ds_view_incall = false; window.__ds_view_url = null; window.__ds_view_from = null; if (window.__ds_pc) { try { window.__ds_pc.close(); } catch (e) {} window.__ds_pc = null; } }
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
    if (inCall) window.__ds_view_incall = true;
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
    var callAcct = activeOneOnOneAcct();
    // Only show a share from someone IN this call: the 1:1 partner, or a participant of the
    // group voice channel. A signal is just a chat message, so any friend could send one;
    // auto-connecting to a non-participant's URL would hand them your IP and your screen.
    var fromOk = false;
    if (window.__ds_view_from) {
      if (callAcct) fromOk = callAcct === window.__ds_view_from;
      else fromOk = src_friends.some(function (f) { return rowAccountId(f) === window.__ds_view_from; });
    }
    if (window.__ds_view_url && fromOk) {
      if (!shareTile) {
        shareTile = el(doc, "div", "ds-tile ds-share-tile");
        if (!shareTile.style.position) shareTile.style.position = "relative";
        var vid = el(doc, "video"); vid.className = "ds-share-video";
        // sound ON by default — we try to play unmuted; if the autoplay policy blocks
        // it, fall back to muted + the one-click "Enable sound" button below.
        vid.autoplay = true; vid.muted = false; vid.volume = 1; vid.playsInline = true;
        vid.addEventListener("canplay", function () {
          if (vid.__sndTried || !window.__ds_view_audio) return;
          vid.__sndTried = true; vid.muted = false; vid.volume = 1;
          var p = vid.play && vid.play();
          if (p && p.catch) p.catch(function () { vid.muted = true; if (vid.play) vid.play().catch(function () {}); });
        });
        // no "Screen" label overlay — keeps the shared video clean (e.g. when recording)
        // one-click unmute, shown only if the browser refused unmuted autoplay
        var unmute = el(doc, "div", "ds-unmute"); unmute.textContent = "🔊 Enable sound";
        unmute.style.cssText = "position:absolute;left:8px;bottom:8px;background:#5865f2;color:#fff;padding:4px 9px;border-radius:6px;font-size:12px;cursor:pointer;z-index:5;display:none";
        unmute.addEventListener("click", function (e) {
          e.stopPropagation(); vid.muted = false; vid.volume = 1;
          if (vid.play) vid.play().catch(function () {});
          unmute.style.display = "none";
        });
        // true-fullscreen button: ONLY visible once the tile is already in big mode.
        // Goes edge-to-edge real fullscreen (whole screen), not just the in-app big view.
        var fsbtn = el(doc, "div", "ds-fsbtn"); fsbtn.textContent = "⛶ Fullscreen";
        fsbtn.title = "Fill the whole screen";
        fsbtn.style.cssText = "position:absolute;right:8px;bottom:8px;background:rgba(0,0,0,.55);color:#fff;padding:4px 9px;border-radius:6px;font-size:12px;cursor:pointer;z-index:5;display:none";
        fsbtn.addEventListener("click", function (e) {
          e.stopPropagation();
          var fsEl = doc.fullscreenElement || doc.webkitFullscreenElement;
          if (fsEl) { (doc.exitFullscreen || doc.webkitExitFullscreen || function () {}).call(doc); return; }
          // Fullscreen the TILE (keeps the app's overlay/design) — the blue dotted
          // ring was just the focus outline, suppressed via CSS. play() nudges a
          // repaint so it doesn't sit blank until the mouse moves.
          var req = shareTile.requestFullscreen || shareTile.webkitRequestFullscreen;
          if (req) {
            try {
              var pr = req.call(shareTile);
              if (pr && pr.then) pr.then(function () { try { vid.play(); } catch (er2) {} });
            } catch (er) {}
          }
          try { vid.play(); } catch (er3) {}
        });
        // in exclusive fullscreen the overlay buttons (+cursor) auto-hide after 2.5s
        // idle and reappear on mouse move — like a video player.
        var fsHideTimer = null;
        var fsShowControls = function () {
          shareTile.classList.remove("ds-fs-idle");
          if (fsHideTimer) { clearTimeout(fsHideTimer); fsHideTimer = null; }
          if (doc.fullscreenElement || doc.webkitFullscreenElement)
            fsHideTimer = setTimeout(function () { shareTile.classList.add("ds-fs-idle"); }, 2500);
        };
        var syncFs = function () {
          var fsEl = doc.fullscreenElement || doc.webkitFullscreenElement;
          fsbtn.textContent = fsEl ? "⮌ Exit fullscreen" : "⛶ Fullscreen";
          // visible only in big mode (or while actually fullscreen)
          fsbtn.style.display = (__ds_w.expanded || fsEl) ? "block" : "none";
          if (fsEl) { fsShowControls(); }
          else { shareTile.classList.remove("ds-fs-idle"); if (fsHideTimer) { clearTimeout(fsHideTimer); fsHideTimer = null; } }
        };
        shareTile.addEventListener("mousemove", fsShowControls);
        doc.addEventListener("fullscreenchange", syncFs);
        doc.addEventListener("webkitfullscreenchange", syncFs);
        shareTile.appendChild(vid); shareTile.appendChild(unmute); shareTile.appendChild(fsbtn);
        shareTile.__unmute = unmute; shareTile.__vid = vid; shareTile.__fsbtn = fsbtn;
        shareTile.addEventListener("click", function () {
          __ds_w.expanded = !__ds_w.expanded;
          stage.classList.toggle("ds-share-expanded", __ds_w.expanded);
          syncFs();
        });
        tiles.appendChild(shareTile);
        shareTile.dataset.url = window.__ds_view_url;
        wConnectAuto(window.__ds_view_url, vid);
      } else if (shareTile.dataset.url !== window.__ds_view_url) {
        shareTile.dataset.url = window.__ds_view_url;
        wConnectAuto(window.__ds_view_url, shareTile.querySelector("video"));
      }
      // show the unmute prompt only while audio is available AND still muted
      if (shareTile && shareTile.__unmute)
        shareTile.__unmute.style.display = (window.__ds_view_audio && shareTile.__vid && shareTile.__vid.muted) ? "block" : "none";
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

  // === Appearance (user themes) =============================================
  // The reskin is driven by CSS variables (theme/friends.custom.css :root). A theme = a preset
  // token set + optional accent/roundness overrides + the user's own CSS, stored in this Steam
  // profile's localStorage and injected after the main sheet. Nothing leaves the PC.
  var THEME_KEY = "ds_theme";
  var PRESETS = {
    discord: { label: "Discord", t: {} },
    midnight: { label: "Midnight (OLED)", t: {
      sidebar: "#0b0b0d", bg: "#000000", darkest: "#1a1a1e", raise: "#1c1c21", hover: "#16161a",
      panel: "#0b0b0d", popover: "#111114", media: "#000000" } },
    steam: { label: "Steam blue", t: {
      sidebar: "#171d25", bg: "#1b2838", darkest: "#0e1419", raise: "#2a3f5a", hover: "#22344a",
      panel: "#141b24", popover: "#0f151c", text: "#c7d5e0", bright: "#ffffff", muted: "#8f98a0",
      accent: "#1a9fff", online: "#57cbde", idle: "#e4b54b", offline: "#6d7882" } },
    mocha: { label: "Catppuccin Mocha", t: {
      sidebar: "#181825", bg: "#1e1e2e", darkest: "#11111b", raise: "#313244", hover: "#2a2b3c",
      panel: "#181825", popover: "#11111b", text: "#cdd6f4", bright: "#f5f7ff", muted: "#a6adc8",
      accent: "#cba6f7", "on-accent": "#11111b", danger: "#f38ba8", "danger-hover": "#eba0ac",
      online: "#a6e3a1", idle: "#f9e2af", offline: "#6c7086" } },
  };
  var RADII = { sharp: ["2px", "3px", "4px"], default: null, round: ["10px", "14px", "18px"] };
  // Anything stored or imported goes through here, so only known values reach the page.
  function normTheme(s) {
    var d = { preset: "discord", accent: "", radius: "default", css: "" };
    if (s && typeof s === "object") {
      if (PRESETS.hasOwnProperty(s.preset)) d.preset = s.preset;
      if (/^#[0-9a-f]{6}$/i.test(s.accent || "")) d.accent = s.accent;
      if (RADII.hasOwnProperty(s.radius)) d.radius = s.radius;
      if (typeof s.css === "string") d.css = s.css.slice(0, 50000);
    }
    return d;
  }
  function loadTheme() {
    try { return normTheme(JSON.parse(localStorage.getItem(THEME_KEY))); } catch (e) { return normTheme(null); }
  }
  // Custom CSS can't run script, but it CAN make Steam fetch remote URLs (url(), @import), which
  // would tell a theme's author when you open Steam and, with attribute selectors, leak what's
  // on screen. Themes get pasted from strangers, so only inline data: resources are kept.
  function sanitizeUserCss(css) {
    return String(css || "")
      .replace(/\\/g, "")                                   // no escapes to smuggle "url(" past us
      .replace(/@import[^;]*;?/gi, "/* @import removed */")
      .replace(/url\(\s*(['"]?)(?!data:)[^)]*\)/gi, "none")
      .replace(/[a-z-]*\([^)]*:\/\/[^)]*\)/gi, "none")      // any other function given a URL (image-set, u\72l, ...)
      .replace(/<\/?style/gi, "");
  }
  function themeCss(th) {
    var t = Object.assign({}, (PRESETS[th.preset] || PRESETS.discord).t);
    if (th.accent) t.accent = th.accent;
    var r = RADII[th.radius];
    if (r) { t["radius-sm"] = r[0]; t.radius = r[1]; t["radius-lg"] = r[2]; }
    var vars = Object.keys(t).map(function (k) { return "  --dc-" + k + ": " + t[k] + ";"; }).join("\n");
    // :root:root outranks the sheet's :root wherever Millennium puts quick.css.
    return (vars ? ":root:root {\n" + vars + "\n}\n" : "") + sanitizeUserCss(th.css);
  }
  function applyTheme(doc) {
    var css = themeCss(window.__ds_theme || (window.__ds_theme = loadTheme()));
    var st = doc.getElementById("discordish-user-css");
    if (!st) {
      st = doc.createElement("style");
      st.id = "discordish-user-css";
    }
    // keep it LAST in <head> so it wins over the main sheet and quick.css
    var head = doc.head || doc.documentElement;
    if (head.lastElementChild !== st) head.appendChild(st);
    if (st.textContent !== css) st.textContent = css;
  }
  function saveTheme(th) {
    window.__ds_theme = th;
    try { localStorage.setItem(THEME_KEY, JSON.stringify(th)); } catch (e) {}
    friendsDocs().forEach(function (d) { try { applyTheme(d); } catch (e) {} });
  }
  // Export/import as one line of text people can paste in a post: "dstheme:" + base64(JSON).
  function exportTheme(th) {
    return "dstheme:" + btoa(unescape(encodeURIComponent(JSON.stringify(th))));
  }
  function importTheme(code) {
    var m = String(code || "").trim().match(/^dstheme:([A-Za-z0-9+/=]+)$/);
    if (!m) return null;
    try {
      return normTheme(JSON.parse(decodeURIComponent(escape(atob(m[1])))));
    } catch (e) { return null; }
  }

  // Palette button next to "add friend" opens the Appearance panel.
  function appearanceButton(doc) {
    var host = doc.querySelector(".friendsTabButtonsContainer");
    if (!host || host.querySelector(".ds-theme-btn")) return;
    var b = el(doc, "div", "ds-theme-btn no-drag");
    b.title = "Appearance";
    b.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2C6.5 2 2 6.1 2 11.2c0 3.6 2.9 6.3 6.4 6.3h1.4c.8 0 1.4.6 1.4 1.4 0 .4-.1.7-.4 1-.3.3-.4.6-.4 1 0 .8.6 1.1 1.6 1.1 5.5 0 10-4.3 10-9.6C22 6.4 17.5 2 12 2Zm-5.5 10a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Zm3-4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Zm5 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Zm3 4a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3Z"/></svg>';
    b.addEventListener("click", function (e) { e.stopPropagation(); toggleAppearance(doc); });
    var add = host.querySelector("a");                    // the add-friend link
    host.insertBefore(b, add && add.parentNode === host ? add : null);
  }
  function toggleAppearance(doc) {
    var open = doc.querySelector(".ds-theme-panel");
    if (open) { open.remove(); return; }
    var th = Object.assign({}, window.__ds_theme || loadTheme());
    var pop = el(doc, "div", "ds-voice-settings ds-theme-panel");
    var t = el(doc, "div", "ds-vs-title"); t.textContent = "Appearance"; pop.appendChild(t);
    function row(label, ctl) {
      var r = el(doc, "div", "ds-vs-row"), l = el(doc, "span", "ds-vs-label");
      l.textContent = label; r.appendChild(l); r.appendChild(ctl); pop.appendChild(r); return ctl;
    }
    function select(opts, val, onchange) {
      var s = el(doc, "select", "ds-vs-select");
      opts.forEach(function (o) { var op = el(doc, "option"); op.value = o[0]; op.textContent = o[1]; s.appendChild(op); });
      s.value = val; s.addEventListener("change", function () { onchange(s.value); }); return s;
    }
    var presetSel = row("Theme", select(Object.keys(PRESETS).map(function (k) { return [k, PRESETS[k].label]; }), th.preset,
      function (v) { th.preset = v; th.accent = ""; accent.value = presetAccent(); saveTheme(th); }));
    function presetAccent() { return th.accent || PRESETS[th.preset].t.accent || "#5865f2"; }
    var accent = el(doc, "input", "ds-theme-color"); accent.type = "color"; accent.value = presetAccent();
    accent.addEventListener("input", function () { th.accent = accent.value; saveTheme(th); });
    row("Accent", accent);
    var radiusSel = row("Corners", select([["sharp", "Sharp"], ["default", "Default"], ["round", "Round"]], th.radius,
      function (v) { th.radius = v; saveTheme(th); }));
    var cl = el(doc, "div", "ds-vs-label ds-theme-sub"); cl.textContent = "Custom CSS"; pop.appendChild(cl);
    var css = el(doc, "textarea", "ds-theme-css");
    css.spellcheck = false;
    css.placeholder = ":root { --dc-bg: #202225; }\n.friend { border-radius: 0 !important; }";
    css.value = th.css;
    var cssTimer = null;
    css.addEventListener("input", function () {
      clearTimeout(cssTimer);
      cssTimer = setTimeout(function () { th.css = css.value; saveTheme(th); }, 300);
    });
    pop.appendChild(css);
    var status = el(doc, "div", "ds-src-hint ds-theme-status");
    var btns = el(doc, "div", "ds-theme-btns");
    function button(label, fn) { var b = el(doc, "button", "ds-stream-stop"); b.textContent = label; b.addEventListener("click", fn); btns.appendChild(b); }
    button("Copy theme code", function () {
      var code = exportTheme(th);
      try { navigator.clipboard.writeText(code); status.textContent = "Copied. Paste it anywhere to share."; }
      catch (e) { css.value = code; status.textContent = "Copy the code from the box."; }
    });
    button("Paste theme code", function () {
      var apply = function (text) {
        var got = importTheme(text);
        if (!got) { status.textContent = "That isn't a theme code (it starts with dstheme:)."; return; }
        th = got; saveTheme(th);
        presetSel.value = th.preset; radiusSel.value = th.radius; accent.value = presetAccent(); css.value = th.css;
        status.textContent = "Theme applied.";
      };
      try { navigator.clipboard.readText().then(apply, function () { apply(css.value); }); } catch (e) { apply(css.value); }
    });
    button("Reset", function () {
      th = { preset: "discord", accent: "", radius: "default", css: "" }; saveTheme(th);
      presetSel.value = "discord"; radiusSel.value = "default"; accent.value = presetAccent(); css.value = "";
      status.textContent = "Back to the default look.";
    });
    pop.appendChild(btns); pop.appendChild(status);
    var close = el(doc, "button", "ds-stream-go"); close.textContent = "Done";
    close.addEventListener("click", function () { pop.remove(); });
    pop.appendChild(close);
    pop.addEventListener("click", function (e) { e.stopPropagation(); });
    (doc.body || doc.documentElement).appendChild(pop);
  }

  // === auto-signaling over Steam chat ======================================
  // The host sends "ds-screenshare::<url>" (or ::stop) to the friend over chat; the
  // viewer polls friend chats for it and sets window.__ds_view_url so the call tile
  // connects with no pasting. We hide these marker messages from the chat history.
  var SIG = "ds-screenshare::";
  window.__ds_sig_seen = window.__ds_sig_seen || {};   // per-friend last-processed ordinal
  // Payload = "<whep-url>" optionally followed by "|ice=<base64(JSON iceServers)>" so the
  // viewer gets the host's Cloudflare TURN credentials (needed to relay across NATs).
  // "<url>|ice=<b64 JSON>|ws=<enc>|aud=1" — base64 and encodeURIComponent never emit '|',
  // so a simple split is safe and order-independent.
  function sigPayload(url, ice, ws, audio, codec, lan, acodec) {
    var p = url;
    if (ice && ice.length) { try { p += "|ice=" + btoa(JSON.stringify(ice)); } catch (e) {} }
    if (ws) p += "|ws=" + encodeURIComponent(ws);
    if (audio) p += "|aud=1";
    if (codec && codec !== "h264") p += "|cdc=" + codec;   // absent = H.264 (older hosts)
    if (lan && lan !== url) p += "|lan=" + encodeURIComponent(lan);
    if (audio && acodec === "opus") p += "|ac=opus";        // absent = AAC, WS path only
    return p;
  }
  // A signal is a chat message any Steam friend can send, so everything in it is untrusted.
  // Only accept endpoints shaped like OUR share: https (or wss) anywhere, plain http/ws only to
  // a private/LAN address (an interceptable plain-text handshake must never cross the
  // internet), and only the share paths. ICE servers must be STUN/TURN URLs.
  function isPrivateHost(h) {
    h = (h || "").replace(/^\[|\]$/g, "").toLowerCase();
    var m = h.match(/^(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/);
    if (m) {
      var a = +m[1], b = +m[2];
      return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
             (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254);
    }
    return h === "::1" || /^f[cd][0-9a-f]{2}:/.test(h) || /^fe80:/.test(h);
  }
  function safeShareUrl(u, kind) {   // kind: "whep" | "ws"
    if (typeof u !== "string" || !u || u.length > 2048) return null;
    var x; try { x = new URL(u); } catch (e) { return null; }
    var secure = kind === "ws" ? "wss:" : "https:", plain = kind === "ws" ? "ws:" : "http:";
    if (x.protocol !== secure && !(x.protocol === plain && isPrivateHost(x.hostname))) return null;
    if (x.username || x.password) return null;
    if (!(kind === "ws" ? /\/screen\/ws$/ : /\/screen\/whep$/).test(x.pathname)) return null;
    return u;
  }
  function safeIce(ice) {
    if (!Array.isArray(ice) || !ice.length || ice.length > 8) return null;
    var ok = ice.every(function (s) {
      if (!s || typeof s !== "object") return false;
      var urls = Array.isArray(s.urls) ? s.urls : [s.urls];
      if (!urls.length || urls.length > 8) return false;
      if (!urls.every(function (x) { return typeof x === "string" && x.length < 512 && /^(stun|turn|turns):/.test(x); })) return false;
      return ["username", "credential"].every(function (k) { return s[k] === undefined || (typeof s[k] === "string" && s[k].length < 512); });
    });
    return ok ? ice : null;
  }
  function parseSignal(payload) {
    var parts = payload.split("|");
    var out = { url: parts[0], ice: null, ws: null, audio: false, codec: "h264", lan: null, acodec: "aac" };
    for (var i = 1; i < parts.length; i++) {
      var s = parts[i];
      if (s.indexOf("ice=") === 0) { try { out.ice = JSON.parse(atob(s.slice(4))); } catch (e) {} }
      else if (s.indexOf("ws=") === 0) { try { out.ws = decodeURIComponent(s.slice(3)); } catch (e) {} }
      else if (s.indexOf("aud=") === 0) { out.audio = s.slice(4) === "1"; }
      else if (s.indexOf("cdc=") === 0) { out.codec = s.slice(4) === "av1" ? "av1" : "h264"; }
      else if (s.indexOf("ac=") === 0) { out.acodec = s.slice(3) === "opus" ? "opus" : "aac"; }
      else if (s.indexOf("lan=") === 0) { try { out.lan = decodeURIComponent(s.slice(4)); } catch (e) {} }
    }
    out.url = safeShareUrl(out.url, "whep");
    out.ws = safeShareUrl(out.ws, "ws");
    out.lan = safeShareUrl(out.lan, "whep");
    out.ice = safeIce(out.ice);
    return out;
  }
  // pure helpers, exposed for tests/test_plugin.mjs (no effect in Steam)
  window.__ds_test = { sigPayload: sigPayload, parseSignal: parseSignal, signalIsFresh: signalIsFresh,
                      safeShareUrl: safeShareUrl, isPrivateHost: isPrivateHost,
                      sanitizeUserCss: sanitizeUserCss, themeCss: themeCss, exportTheme: exportTheme,
                      importTheme: importTheme, loadTheme: loadTheme };
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
  function sendSignalToAcct(acct, payload) {
    try {
      if (acct == null) return;
      var fc = window.g_FriendsUIApp.m_ChatStore.GetFriendChat(acct);
      if (fc && fc.SendChatMessageInternal) fc.SendChatMessageInternal(SIG + payload);
    } catch (e) { console.warn("[ds] sendSignalToAcct", e); }
  }
  function sendSignal(doc, payload) {
    var acct = friendAcctForDoc(doc);
    if (acct == null) return;
    // remember who we're sharing with, so the tunnel watcher can re-signal a rebuilt URL
    // without needing a chat document in hand.
    window.__ds_sig_acct = acct;
    sendSignalToAcct(acct, payload);
  }
  // Silence our screen-share signaling messages so start/stop don't ping the friend (sound +
  // unread + toast). Steam already has a gate for this — BShouldSilentlyAddMessage(e) returns
  // true to add a message WITHOUT notifying (used for blocked users). We extend it on the
  // friend-chat prototype to also return true for SIG-prefixed messages. One-time, and degrades
  // gracefully (just keeps notifying) if Steam ever renames the method.
  function patchSilenceSignals(fc) {
    try {
      var proto = Object.getPrototypeOf(fc);
      if (!proto || typeof proto.BShouldSilentlyAddMessage !== "function" || proto.__ds_silenced) return;
      var orig = proto.BShouldSilentlyAddMessage;
      proto.BShouldSilentlyAddMessage = function (e) {
        try {
          var t = e && (e.strMessageInternal || e.message || "");
          if (typeof t === "string" && t.indexOf(SIG) === 0) return true;   // our signaling -> silent
        } catch (_) {}
        return orig.apply(this, arguments);
      };
      proto.__ds_silenced = true;
      window.__ds_silence_patched = true;
      console.log("[ds] screen-share signaling silenced (no start/stop notifications)");
    } catch (e) {}
  }
  // Should a signal message be acted on? `seen` = last ordinal handled for this friend
  // (undefined on the first poll after Steam starts). On that first poll the newest signal in
  // the history may be days old — a share whose "stop" never got sent — and acting on it put a
  // dead share tile in every later call with that friend. So on first sight, only a RECENT
  // signal counts (covers a Steam restart mid-share); older ones are just marked as seen.
  var SIGNAL_FRESH_S = 10 * 60;
  function signalIsFresh(m, seen, nowSec) {
    var ord = m.unOrdinal;
    if (seen !== undefined) return (ord || 0) > seen;
    var ts = +(m.rtTimestamp || m.m_rtTimestamp || 0);
    return !ts || nowSec - ts < SIGNAL_FRESH_S;
  }
  function pollSignals() {
    try {
      var app = window.g_FriendsUIApp, fs = app.m_FriendStore, cs = app.m_ChatStore;
      (fs.all_friends || []).forEach(function (f) {
        var fc = cs.GetFriendChat(f.m_unAccountID);
        if (fc && !window.__ds_silence_patched) patchSilenceSignals(fc);   // one-time prototype patch
        var msgs = fc && fc.m_rgChatMessages;
        if (!msgs || !msgs.length) return;
        for (var i = msgs.length - 1; i >= 0 && i >= msgs.length - 6; i--) {
          var m = msgs[i];
          if (!m.strMessageInternal || m.strMessageInternal.indexOf(SIG) !== 0) continue;
          if (m.unAccountID !== f.m_unAccountID) continue;   // only act on what the FRIEND sent
          var ord = m.unOrdinal || i;
          var seen = window.__ds_sig_seen[f.m_unAccountID];
          window.__ds_sig_seen[f.m_unAccountID] = Math.max(seen || 0, ord);
          if (!signalIsFresh({ unOrdinal: ord, rtTimestamp: m.rtTimestamp, m_rtTimestamp: m.m_rtTimestamp }, seen, Date.now() / 1000)) break;
          var payload = m.strMessageInternal.slice(SIG.length);
          if (payload === "stop") {
            if (!window.__ds_view_from || window.__ds_view_from === f.m_unAccountID) {   // only the sharer can stop their share
              window.__ds_view_url = null; window.__ds_view_ice = null; window.__ds_view_ws = null; window.__ds_view_audio = false; window.__ds_view_from = null;
            }
          }
          else { var p = parseSignal(payload); if (!p.url) break; window.__ds_view_url = p.url; window.__ds_view_ice = p.ice; window.__ds_view_ws = p.ws; window.__ds_view_audio = p.audio; window.__ds_view_codec = p.codec; window.__ds_view_lan = p.lan; window.__ds_view_acodec = p.acodec; window.__ds_view_from = f.m_unAccountID; }
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
      // A date line whose messages are ALL hidden signals would show as an empty day
      // ("Friday, October 16" twice in a row), so hide it too; re-show it if a real
      // message lands under it later.
      doc.querySelectorAll(".msg.timeDivision").forEach(function (div) {
        var shown = false;
        if (/\btime_passes\b/.test(div.className || "")) return;   // the thin gap marker, not a date
        for (var n = div.nextElementSibling; n; n = n.nextElementSibling) {
          var c = n.className || "";
          if (/\btimeDivision\b/.test(c)) { if (/\btime_passes\b/.test(c)) continue; break; }   // next day
          if (!n.dataset.dsHidden) { shown = true; break; }
        }
        if (!shown && !div.dataset.dsDivHidden) { div.style.display = "none"; div.dataset.dsDivHidden = "1"; }
        else if (shown && div.dataset.dsDivHidden) { div.style.display = ""; delete div.dataset.dsDivHidden; }
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
      // Resolve the OTHER party. For OUTGOING calls the native ring label shows OUR
      // name (not the callee's), so resolve the callee from the voice store's target
      // accountID via the friend store — that gives the right name AND avatar.
      var tName = "", tAvatar = "";
      try {
        var tAcct = st && st.m_targetAccountID;
        var fs = window.g_FriendsUIApp && window.g_FriendsUIApp.m_FriendStore;
        var fr = (fs && tAcct && fs.GetFriend) ? fs.GetFriend(tAcct) : null;
        if (!fr && fs && tAcct && fs.all_friends) fr = fs.all_friends.find(function (x) {
          var p = x.m_persona || {};
          return p.m_unAccountID === tAcct || p.accountid === tAcct ||
                 (p.m_steamid && p.m_steamid.GetAccountID && p.m_steamid.GetAccountID() === tAcct);
        });
        if (fr) {
          var per = fr.m_persona || fr.persona || {};
          tName = fr.m_strNickname || per.m_strPlayerName || per.persona_name || "";
          tAvatar = per.avatar_url_full || per.avatar_url_medium || per.avatar_url ||
                    (typeof per.GetAvatarURL === "function" ? per.GetAvatarURL() : "") || "";
          if (!tAvatar) {   // some builds only store the 40-char hash; build the CDN URL
            var h = "" + (per.m_strAvatarHash || per.avatar_hash || per.m_strAvatar || "");
            if (/^[0-9a-f]{40}$/i.test(h)) tAvatar = "https://avatars.steamstatic.com/" + h.slice(0, 2) + "/" + h + "_full.jpg";
          }
        }
      } catch (e) {}
      var nameEl = native.querySelector(".nOdcT-MoOaXGePXLyPe0H, [class*=VoiceStatusLab]");
      var nativeName = (nameEl ? nameEl.textContent : "").replace(/\s*would like[\s\S]*$/i, "").trim();
      // incoming -> trust the native caller label; outgoing -> the resolved callee
      var name = (incoming ? nativeName : tName) || tName || nativeName || chatFriendName(doc) || "Friend";
      var avatar = "";
      if (incoming) {   // incoming ring carries the caller's <img>
        var imgs = native.querySelectorAll("img");
        for (var ii = 0; ii < imgs.length; ii++) { if (imgs[ii].src && imgs[ii].src.indexOf("data:") !== 0) { avatar = imgs[ii].src.replace("_medium", "_full"); break; } }
      }
      if (!avatar) avatar = tAvatar;   // outgoing (or native had no img): resolved target avatar
      if (!avatar && name) {   // last resort: scrape this friend's avatar <img> from the roster DOM
        try {
          var rows = doc.querySelectorAll(".friendlistListContainer .friend, .ChatRoomListGroupItem, .chatWindow");
          for (var ri = 0; ri < rows.length; ri++) {
            var rn = rows[ri].querySelector(".nOdcT-MoOaXGePXLyPe0H, [class*=playerName], [class*=friendName]");
            if (rn && rn.textContent && rn.textContent.trim() === name) {
              var rimg = rows[ri].querySelector("img.avatar, .avatarHolder img");
              if (rimg && rimg.src && rimg.src.indexOf("data:") !== 0) { avatar = rimg.src.replace("_medium", "_full"); break; }
            }
          }
        } catch (e) {}
      }
      if (!avatar && window.localStorage && localStorage.ds_debug === "1") {   // help diagnose unusual builds
        try { console.log("[ds] call avatar unresolved; persona keys:", Object.keys((fr && (fr.m_persona || fr.persona)) || {})); } catch (e) {}
      }
      native.classList.add("ds-native-hidden");         // hide Steam's menu (kept clickable for proxy)
      var ring = main.querySelector(".ds-ring");
      if (existing && existing !== ring) existing.remove();
      if (!ring) {
        ring = el(doc, "div", "ds-ring");
        var av = el(doc, "div", "ds-ring-av");
        var nm = el(doc, "div", "ds-ring-name");
        var sub = el(doc, "div", "ds-ring-sub");   // NOT `st`: that name holds the voice-call state above (one function scope)
        var btns = el(doc, "div", "ds-ring-btns");
        var acc = el(doc, "button", "ds-ring-accept"); acc.textContent = "✓ Accept";
        var dec = el(doc, "button", "ds-ring-decline"); dec.textContent = "✕ Decline";
        acc.addEventListener("click", function () {
          ring.__accepting = true; sub.textContent = "Connecting…"; btns.style.display = "none";
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
        ring.appendChild(av); ring.appendChild(nm); ring.appendChild(sub); ring.appendChild(btns);
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
    if (__ds_tickn % 20 === 1) { try { ensureDocked(); } catch (e) {} }   // until the store is up
    var docs = friendsDocs();
    window.__ds_any_incall = docs.some(function (d) { try { return docInCall(d); } catch (e) { return false; } });
    docs.forEach(function (doc) {
      try { injectCSS(doc); } catch (e) {}
      try { applyTheme(doc); } catch (e) {}
      try { appearanceButton(doc); } catch (e) {}
      try { oneClickOpen(doc); } catch (e) {}
      try { chatTweaks(doc); } catch (e) {}
      try { ringUI(doc); } catch (e) {}
      try { callStage(doc); } catch (e) {}
      try { hideSignalMessages(doc); } catch (e) {}
    });
  }

  // The whole layout needs Steam's "Dock chats to the friends list" (FriendsSettings.
  // bSingleWindowMode). Turn it on ONCE so a fresh install works with no manual step; the flag
  // means a user who later switches it off again isn't overridden on every boot.
  function ensureDocked() {
    try {
      if (localStorage.getItem("ds_docked_auto") === "1") return;
      var s = window.g_FriendsUIApp && g_FriendsUIApp.m_SettingsStore;
      var cur = s && s.FriendsSettings;
      if (!cur || typeof s.BUpdateFriendsSettings !== "function") return;   // store not ready yet
      if (cur.bSingleWindowMode !== true &&
          !s.BUpdateFriendsSettings(Object.assign({}, cur, { bSingleWindowMode: true }))) return;
      localStorage.setItem("ds_docked_auto", "1");
      console.log("[ds] docked chats enabled (one-time)");
    } catch (e) {}
  }

  function init() {
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
  var VERSION = 74;
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
