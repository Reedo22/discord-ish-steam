# Screen-share latency & A/V-sync measurement

**Date:** 2026-06-17
**Status:** Approved (design)
**Component:** `plugin/.millennium/Dist/index.js` (viewer) + `rp-webrtc.py` (daemon)

## Problem

The screen-share video and Steam's native voice travel separate paths with
different latencies, so the video lags the voice — when the sharer reacts to
something, the viewer hears it before seeing it. We want **hard numbers** for
the video latency and the resulting A/V desync before deciding whether/how to
reduce it (i.e. measure before optimizing).

Voice is Steam-native and not cleanly instrumentable, so we treat it as a
documented constant. The lever is video latency; this feature measures it.

## Goal (scope)

Continuously estimate, on the viewer side, and surface via **console logging +
an optional on-screen HUD**:

- transport in use (WebRTC vs WS/fMP4)
- resolution, fps, dropped frames
- playout buffer depth (the dominant *tunable* latency)
- network RTT (tunnel or WebRTC)
- **estimated glass-to-glass video latency**
- **estimated A/V desync** (= video latency − voice constant)

Non-goals (YAGNI): on-host visual marker / round-trip-through-video ground-truth
probe; Steam-voice instrumentation. Revisit the marker only if the estimate is
too fuzzy to make a decision.

## Architecture

Almost entirely viewer-side reads of already-exposed APIs. One trivial daemon
endpoint enables RTT timing on the WS path.

### Component A — Daemon `/ping` (rp-webrtc.py)

Add `GET /ping` to the media server (`MediaH`, on the tunneled `MEDIA_PORT`).
Returns `200` with an empty/tiny body immediately, plus permissive CORS
(consistent with the existing WHEP CORS handling). Because the tunnel is
valid-HTTPS, the viewer (an `https://steamloopback.host` context) can `fetch()`
it without a mixed-content violation and time the round-trip.

~6 lines, additive, no change to existing routes.

### Component B — Viewer metrics collector (index.js)

A 1 Hz sampler bound to the active share `<video>` element, started/stopped with
the share tile, **gated by `window.__ds_metrics_on`** (default off).

Per sample, branch on transport (reuse existing flags: `v.__wentWs` ⇒ WS,
else `v.srcObject` ⇒ WebRTC):

**WS / fMP4 path**
- `bufferMs` = `(v.buffered.end(last) − v.currentTime) * 1000`
- `rttMs` = timed `fetch(tunnelBase + "/ping", {cache:"no-store"})` (tunnelBase
  derived from `__ds_view_ws`/`__ds_view_url` minus the path); median of last few,
  null on failure
- fps/res/dropped from `v.getVideoPlaybackQuality()` deltas + `videoWidth/Height`
- `encodeConst` = `ENCODE_WS` (~110ms: nvenc ll + ~100ms fragment accumulation)

**WebRTC path** (richer, exact)
- from `pc.getStats()` inbound-rtp + candidate-pair:
  `rttMs` = `currentRoundTripTime*1000`,
  `bufferMs` = `jitterBufferDelay/jitterBufferEmittedCount*1000`,
  `framesPerSecond`, `framesDropped`, `frameWidth/Height`
- `encodeConst` = `ENCODE_WEBRTC` (~40ms; no fragment buffering)

**Derived (both)**
- `estVideoMs` = `bufferMs + rttMs/2 + encodeConst + frameMs(~20)`
- `estDesyncMs` = `estVideoMs − VOICE_CONST(250)`  (positive ⇒ video lags voice)

Constants live in one clearly-commented block at the top of the metrics code so
calibration is a one-line edit.

### Component C — Output

- **Logging:** when active, `console.log("[ds-metrics] " + JSON.stringify(sample))`
  once per second, and push to a bounded ring buffer `window.__ds_metrics`
  (last 120 samples) for copy-out / inspection.
- **HUD:** when active, a small fixed-corner overlay inside the share tile
  showing the same fields, updated each sample. Pure DOM, removed when metrics
  off or the share ends. Non-interactive (`pointer-events:none`).

Activation: setting `window.__ds_metrics_on = true` in the CEF console starts
both logging and the HUD; `= false` stops and removes them. No key binding.

## Data flow

```
share tile <video> ──┬─ (WS)     buffered/currentTime, getVideoPlaybackQuality
                     └─ (WebRTC) pc.getStats()
            fetch(tunnel/ping) ── RTT (WS path only)
                     │
            sampler (1 Hz, if __ds_metrics_on)
                     ├─ console.log [ds-metrics]
                     ├─ window.__ds_metrics ring buffer
                     └─ HUD overlay
```

## Error handling

- `/ping` fetch fails (LAN/no-tunnel, or daemon down) ⇒ `rttMs = null`,
  `estVideoMs` omits the RTT term and is flagged `rttUnavailable:true`.
- `getStats()` / `getVideoPlaybackQuality()` unsupported ⇒ those fields null;
  sampler still emits what it has (never throws into the polling loop).
- Sampler is fully wrapped in try/catch; any error logs once and the loop
  continues (never disrupts the actual share).

## Calibration (one-time, manual)

On LAN, run the stopwatch method (centisecond timer full-screen, photo of both
screens) to get true glass-to-glass once; adjust `ENCODE_WS` so the estimate
matches. `VOICE_CONST` stays a documented assumption (~250ms) unless we later
measure it.

## Testing

No automated harness (browser/CEF + live pipeline). Verification is manual:
1. Start a share; in the viewer CEF console set `window.__ds_metrics_on = true`.
2. Confirm `[ds-metrics]` lines log ~1/sec with sane values, HUD appears.
3. Force each transport (LAN ⇒ WebRTC, VPN ⇒ WS) and confirm the right branch
   populates (WebRTC fields vs buffer/ping fields).
4. Validate the WS estimate against one stopwatch reading; tune `ENCODE_WS`.
5. Set `= false`; confirm logging stops and HUD is removed.

## Version / deploy

Bump `var VERSION` (47 → 48) since `index.js` changes; commit + push to `master`;
both machines self-update on Steam restart.
