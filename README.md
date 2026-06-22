# discord-ish-steam

A Discord-style reskin of the Steam friends/chat client, built on
[Millennium](https://steambrew.app/). It does **not** replace Steam or touch its
networking/voice — it restyles the existing CEF web UI and adds a few DOM tweaks
that CSS can't do, so all of Steam's real voice/calling keeps working. Runs on
**Linux and Windows**.

## What it does

- **Docked single-window layout** (requires Steam's "Dock chats to the friends
  list" setting on) — roster + chat in one window, themed dark like Discord.
- **Friends list**: one merged list (no In-Game/Online/Offline sections), status
  dots, circular avatars (animated avatar frames stay square so they aren't
  clipped); **group chats pinned to the top**.
- **Composer**: rounded input, blue send button, circular emoji/attach icons,
  centered text, "Message <friend>…" placeholder.
- **Voice control** moved into a header bar at the top of the chat.
- **Discord-style center-stage call screen**: participant tiles with name pills,
  speaking rings, mute badges; real Steam SVG control icons (mute/deafen/leave);
  minimize-to-corner toggle; auto-teardown when the call ends.
- **Noise cancellation** on your mic — runs RNNoise (the Krisp-grade engine) in
  the browser, no virtual audio device, works on any OS. On by default.
- **Screen sharing** over WebRTC — share a **monitor, a single app window, or a
  webcam**, with **desktop audio**, to a friend on your LAN or over the internet,
  right inside the call. Live resolution/bitrate/FPS controls. See below.

## Install

Requires [Millennium](https://steambrew.app/) installed and run at least once.

### Windows — easiest: one-click installer

Download **`discord-ish-steam-setup.exe`** from the
[latest release](https://github.com/Reedo22/discord-ish-steam/releases/latest),
double-click it, and click **Yes** on the admin (UAC) prompt. It auto-installs
everything (git, Python, ffmpeg, desktop-audio support, the plugin + theme, host
binaries) and sets the screen-share daemon to auto-start at logon — no terminal.

> The EXE isn't code-signed, so SmartScreen may warn "unknown publisher" — click
> **More info → Run anyway**.

### Windows — PowerShell one-liner

From an **Administrator** PowerShell (so the daemon's logon task can register):
```powershell
irm https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 | iex
```
The installer is idempotent — re-run it any time to update and self-heal anything
missing. If you downloaded the script, run it past the execution policy with
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

### Linux

```bash
git clone https://github.com/Reedo22/discord-ish-steam ~/discord-ish-steam
cd ~/discord-ish-steam && ./install.sh
```
`install.sh` auto-installs system deps (ffmpeg, gstreamer, python3 via
apt/dnf/pacman), fetches the host binaries (MediaMTX + cloudflared), links the
plugin, writes the theme to Millennium's `quickcss.css`, enables the plugin, and
installs the daemon as a systemd `--user` service.

After installing, **fully restart Steam** (Quit from the tray / `steam -shutdown`
then relaunch) and enable **Settings → Friends & Chat → "Dock chats to the
friends list."**

## Update

The plugin **auto-updates on every Steam boot, no backend** — `index.js` fetches
the latest CSS and `index.js` from the repo on load and runs the newer copy when
its `VERSION` is higher than the bundled one (falling back to the bundled code if
GitHub is unreachable). To update the on-disk copy + daemon: re-run the installer
(Windows EXE / one-liner) or `./update.sh` (Linux).

## Screen sharing

The 🖥 button in the call controls opens a menu to pick a **screen, an app window,
or a camera**, plus resolution / bitrate / FPS / audio, then streams it to the
friend you're chatting with — shown as a tile right in the call. Start/stop is
silent (no chat notifications), and changing quality applies live.

How it works — a small localhost daemon (`rp-webrtc.py`) does the capture the CEF
sandbox can't:

```
capture ──▶ H.264 hw-encode (+AAC audio) ──▶ MediaMTX ──▶ WebRTC/WHEP ─┐
(x11grab / gst ximagesrc on Linux,          (NVENC/VAAPI/QSV/          ├─▶ viewer <video>
 gdigrab on Windows; v4l2/dshow webcam)      AMF, else x264)           │
                            └──▶ fragmented-MP4 over WebSocket ────────┘
```

- **Two transports, auto-selected:** same-LAN viewers connect directly over
  **WebRTC/WHEP** (lowest latency); remote viewers fall back to **fMP4 over a
  WebSocket** through an on-demand Cloudflare quick tunnel — no TURN server needed.
  The viewer has an adaptive jitter buffer that smooths bad connections.
- **Desktop audio** is captured with **no virtual audio device** (Linux: the
  PulseAudio monitor; Windows: WASAPI loopback via `pyaudiowpatch`) and muxed into
  the stream. Sound is on by default for the viewer.
- **Per-window capture is occlusion-proof on Linux** (grabs the window's own
  buffer via `ximagesrc xid`) — the window can be behind others, just not
  minimized. Windows uses `gdigrab` by title.
- Viewers only decode H.264, so any GPU/OS can watch. Click the tile to enlarge;
  in big mode a **Fullscreen** button fills the whole screen.

Host requirements: **Python 3**, **ffmpeg** on PATH, **MediaMTX** in `bin/`, and
(Windows desktop audio) **pyaudiowpatch** — all handled by the installer.
`cloudflared` in `bin/` is optional (off-LAN sharing only). Linux per-window
capture also needs `gstreamer1.0-tools` + plugins.

## How it works

- `theme/friends.custom.css` — the whole reskin (loaded via Millennium's
  `quickcss.css`, layered on top of your active theme).
- `plugin/.millennium/Dist/index.js` — hand-written Millennium frontend module
  (no build step) that runs in SharedJSContext, reaches the friends popup via
  `g_PopupManager`, does the DOM tweaks (voice→header, call stage, share UI,
  noise cancellation), and drives the share daemon over `127.0.0.1:48592`.
- `rp-webrtc.py` — the cross-platform screen-share daemon (control API + capture).
  Run `python rp-webrtc.py --selftest` to diagnose capture/encode/audio.
- `bin/installer.nsi` + `bin/build-installer.sh` — build the one-click Windows EXE.
- `bin/rp-mediamtx.yml` — MediaMTX config (RTSP ingest + WebRTC/WHEP).

## Notes / limits

- Steam's voice UI is global (roster header), so the call stage is scoped to the
  group/friend you're viewing rather than bound to a single chat.
- Per-window share can't capture a **minimized** window (no frames are presented).
- Only the WebSocket/fMP4 path carries audio; the WebRTC/LAN path is video-only
  (Opus mux is a future addition).
- On Millennium ≥ v3, CEF uses a debugging *pipe* rather than a TCP port, so the
  `tools/` CDP helpers won't attach (deploy = commit → restart Steam; the plugin
  self-updates).
