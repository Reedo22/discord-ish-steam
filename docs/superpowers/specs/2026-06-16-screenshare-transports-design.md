# Screen-share transports: low-latency tiers + universal free fallback

**Date:** 2026-06-16
**Status:** Approved design, ready for implementation plan
**Component:** `rp-webrtc.py` (host daemon) + `plugin/.millennium/Dist/index.js` (viewer/host UI)

## Problem

Screen share worked on LAN but a remote friend got a connected-but-black tile.
Root cause (verified by debugging): capture/encode/MediaMTX are healthy and the WHEP
*signaling* reaches the friend over the cloudflared tunnel, but the WebRTC **media**
(UDP/SRTP) has no NAT-traversal path with STUN alone — and there is no TURN relay, so on
a symmetric/CGNAT NAT the media never arrives.

Requirements that emerged while exploring fixes:

1. **Anyone → anyone.** Any user must be able to share to any other user, not just
   people inside one person's Tailscale tailnet or LAN.
2. **Free, no account, no credit card** for the baseline path. (Cloudflare TURN is free
   to 1 TB/mo but needs a card; reliable anonymous TURN no longer exists.)
3. **As close to sub-second latency as possible**, ideally < 1 s, even on the free path.
4. Optional low-latency upgrades for power users, configurable from an in-app settings UI.

## Key physics / constraints

- A **cloudflared quick tunnel carries HTTP/TCP only** — it cannot carry WebRTC's UDP
  media. So a sub-second path that rides the tunnel must use a TCP transport.
- True sub-second is only achievable where **UDP flows directly**: LAN, Tailscale, or a
  TURN relay.
- **Tailscale cannot do "anyone → anyone"** — it is per-tailnet. Excellent for *one
  owner + their own friends* (owner logs in, shares nodes; unlimited, low-latency,
  free), but not a public mechanism.
- The lowest-latency transport that fits "free, no account, anyone → anyone, over the
  tunnel" is **fragmented-MP4 over WebSocket → MSE** (~0.5–1 s), not LL-HLS (~1–3 s).

## Architecture: tiered transport with auto-fallback

The viewer always gets a working picture; it uses the best available path and silently
falls back. Transports, best to worst latency:

| Tier | Transport | Latency | Reach | Cost / setup |
|------|-----------|---------|-------|--------------|
| 1 | WebRTC, direct (LAN) | sub-second | same LAN | none |
| 1 | WebRTC over Tailscale | sub-second | owner + their tailnet | both run Tailscale (owner logs in, shares node) |
| 1 | WebRTC via TURN relay | sub-second | anyone | optional key (Cloudflare / Open Relay / generic) |
| 2 | **WS + MSE (fMP4) over tunnel** | ~0.5–1 s | **anyone** | **none — universal free default** |

**Auto-fallback:** the viewer attempts a Tier-1 WebRTC path when one is offered; if no
media track is live within ~5 s, it switches to the Tier-2 WS+MSE url. Because Tier-2
always works for anyone, **a share never ends in black.**

**Single capture, teed to both transports.** To make fallback seamless (no re-capture
when switching), the host's single ffmpeg capture is teed to two outputs:
`[RTSP → MediaMTX (WebRTC/WHEP)]` and `[fragmented-MP4 → daemon → WebSocket]`. Both
transports are therefore live simultaneously and the viewer can switch without the host
restarting capture. (If teeing proves costly, the fallback path may instead start its
own encode on demand — decided in the plan.)

## Components

### Host daemon (`rp-webrtc.py`)

- **WebRTC path (exists):** MediaMTX WHEP. ICE servers from `turn_ice()` (STUN +
  optional TURN key). Already advertises LAN + public IPs.
- **Tailscale tier (new):** detect the host's Tailscale IP (`tailscale ip -4`, or a
  `100.64.0.0/10` interface address); when present, add it to MediaMTX
  `MTX_WEBRTCADDITIONALHOSTS` and offer a WHEP url the viewer reaches via its existing
  localhost `/whep?target=` proxy (handles the https→http mixed-content rule).
- **WS+MSE path (new):** a WebSocket endpoint that streams fragmented MP4. ffmpeg emits
  fMP4 (`-movflags +frag_keyframe+empty_moov+default_base_moof`, per-frame/short
  fragments, 1 s GOP); the daemon reads chunks, sends the init segment (`ftyp`+`moov`)
  to each newly-connected client, then forwards media fragments (`moof`+`mdat`). The
  cloudflared tunnel exposes this endpoint so the viewer connects over `wss://` (tunnel
  supports WebSocket).
- **Transport selection in `/start`:** the response includes the Tier-1 url+type (when a
  low-latency path is available) and the Tier-2 WS url, so the host can signal both.
- **Settings endpoints (new):** `POST /turn-key` writes `~/.config/discordish/turn.json`
  (chmod 600), clears cached creds, and reconfigures; `GET /settings` returns current
  state (tailscale up?, key set?, bitrate, relay usage) for the panel.
- **Cost limiter (TURN path only):** a monthly estimate of relayed GB (bitrate × off-LAN
  WebRTC streaming time, persisted in `~/.config/discordish/usage.json`, reset monthly).
  When the next share would exceed a configurable soft cap (default ~900 GB, 0 =
  unlimited), the TURN path is skipped and the share uses WS+MSE instead. Estimate is
  intentionally conservative (assumes relay even if the path was direct) so it can only
  *under*-use the relay, never cause an overage. Bitrate is configurable (default 6 Mbps;
  options 3 / 6 / 12).

### Plugin (`plugin/.millennium/Dist/index.js`)

- **Signal format:** extend the `ds-screenshare::` payload (already carries `url` and
  optional `|ice=`) to also carry a transport `type` and the Tier-2 WS url, e.g.
  `url|ice=<b64>|t=webrtc|ws=<wss-url>`. Backward-compatible parse.
- **Viewer:**
  - `type=webrtc` → existing `wConnectShare` (now using the host-supplied ICE servers).
  - Tier-2 → new `wConnectWsMse(wsUrl, video)`: open the WebSocket, create a
    `MediaSource`, `addSourceBuffer('video/mp4; codecs="avc1.<profile>"')`, append the
    init segment then media fragments; keep the buffer trimmed to stay low-latency.
  - **Auto-fallback:** when a Tier-1 url is offered, try it; if no live track within ~5 s,
    call `wConnectWsMse` with the WS url. Uses MSE built into Chromium — no external lib.
- **Settings panel (new):** a "Screen share" section in the existing share/voice settings
  UI the plugin injects. Shows: Tailscale status (detected + IP, or "not running"),
  optional relay key entry — either a **Cloudflare key** (Key ID + token) *or* a
  **generic TURN** paste (urls + username + credential) — bitrate selector, and relay
  usage vs cap. Save/Clear POST to the daemon's localhost endpoints.

## Data flow (remote viewer, no Tailscale, no key — the default)

1. Host `/start`: capture tees to MediaMTX (WHEP) and fMP4→WS. Daemon ensures the
   cloudflared tunnel and returns `{ ws: "wss://<tunnel>/screen/ws", whep: <…>, type }`.
2. Host plugin signals the friend over Steam chat: `ds-screenshare::<whep>|t=webrtc|ws=<ws>`.
3. Friend's viewer tries WebRTC (will fail off-LAN with no relay), and within ~5 s
   switches to `wConnectWsMse(<ws>)` → fMP4 over the tunnel → MSE `<video>`. ~0.5–1 s.

## Error handling

- WS connect fails / MSE unsupported → tile shows a one-line "couldn't start stream"
  instead of silent black.
- Tier-1 offered but never connects → silent fallback to WS+MSE (the normal case off-LAN
  with no relay).
- TURN mint fails or over cap → skip relay, WS+MSE; panel status reflects it.
- Control API (`/turn-key`, `/settings`, `/start`) stays **localhost-only**; only the
  media port (WHEP / WS) is ever tunneled, so settings can't be reached remotely.

## Testing

- **Capture health:** pull a frame from each transport and assert non-black (we already
  do this for RTSP; add a WS+MSE pull).
- **Off-LAN simulation:** laptop on phone hotspot or full-tunnel VPN (so it cannot reach
  the host LAN IP), second Steam account friended to the main one; confirm the tile shows
  video (not black) via WS+MSE, and via WebRTC when a key/Tailscale is present.
- **Latency:** rough glass-to-glass check (on-screen clock vs viewer) for WS+MSE target
  < ~1 s and WebRTC sub-second.
- **YAML/JS validity** gates as today (ast.parse for Python, esprima for index.js).

## Phasing (for the implementation plan)

1. **WS+MSE universal path + auto-fallback** — the core ask (free, anyone, ~sub-second).
2. **Tailscale tier** — detect + advertise + signal (low-latency for owner + friends).
3. **Settings panel + provider-agnostic TURN key + cost limiter.**

Each phase is independently shippable; Tier-1 WebRTC + STUN already exists from prior work.

## Out of scope

- Media-over-QUIC / WebTransport (not viable over cloudflared today).
- Bundling hls.js / LL-HLS (superseded by WS+MSE for the universal path).
- Automating Tailscale auth-key onboarding in the public installer (the owner logs in;
  baking an auth key into a public repo is a security non-starter).
