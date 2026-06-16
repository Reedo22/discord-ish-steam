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
- **Discord-style center-stage call screen** (in group voice channels): half-screen
  participant tiles with name pills, speaking rings, mute badges; real Steam SVG
  control icons (mute/deafen/leave); minimize-to-corner toggle; auto-teardown when
  the call ends; only shown while viewing the call's group.
- **Screen sharing** over WebRTC — share a whole monitor or a single app window, to
  a friend on your LAN or over the internet. See below.

## Install

Requires [Millennium](https://steambrew.app/) installed and run at least once.

**Linux**
```bash
git clone https://github.com/Reedo22/discord-ish-steam ~/discord-ish-steam
cd ~/discord-ish-steam && ./install.sh
```

**Windows** (PowerShell)
```powershell
irm https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 | iex
```

The installer is one-stop: it installs the system dependencies (ffmpeg, gstreamer,
python3 — via apt/dnf/pacman on Linux), fetches the host binaries (MediaMTX +
cloudflared), links/copies the plugin, writes the theme to Millennium's
`quickcss.css`, enables the plugin, and sets up the screen-share daemon as a
service (systemd `--user` on Linux, a logon Scheduled Task on Windows). On Windows,
install Python + ffmpeg first (`winget install Python.Python.3 Gyan.FFmpeg`).

Then **fully restart Steam** (`steam -shutdown` then relaunch / Quit from the tray)
and enable **Settings → Friends & Chat → "Dock chats to the friends list."**

## Update

The plugin **auto-updates on every Steam boot, no backend** — `index.js` fetches
the latest CSS and `index.js` from the repo on load and runs the newer copy when its
`VERSION` is higher than the bundled one (falling back to the bundled code if GitHub
is unreachable). `plugin.json` sets `useBackend: false`, so it loads on every
Millennium version. To update the on-disk copy + daemon: `./update.sh` (Linux) or
re-run the Windows one-liner.

## Screen sharing

The share button in the chat header opens a menu to pick a **monitor or an app
window** plus a quality level, then streams it to the friend you're chatting with.

How it works — a small localhost daemon (`rp-webrtc.py`) does the capture the CEF
sandbox can't:

```
capture ──▶ H.264 hardware encode ──▶ MediaMTX ──▶ WebRTC / WHEP ──▶ viewer's <video>
(x11grab / gst ximagesrc          (NVENC / VAAPI /   (RTSP→WebRTC)
 on Linux; gdigrab on Windows)     QSV, else x264)
```

- **Per-window capture is occlusion-proof on Linux** (grabs the window's own buffer
  via `ximagesrc xid`), so the window can be behind others — just not minimized. On
  Windows it uses `gdigrab` by window title.
- **Same-LAN** viewers connect directly. **Off-LAN** sharing uses an on-demand
  Cloudflare quick tunnel for valid-HTTPS signaling; media stays P2P via STUN.
- Viewers only decode H.264, so any GPU/OS can watch.

Host requirements: **Python 3**, **ffmpeg** on PATH, and **MediaMTX** in `bin/`
(Linux: download from [mediamtx releases](https://github.com/bluenviron/mediamtx/releases);
Windows: `install.ps1` fetches it via `bin\fetch-windows.ps1`). Linux per-window
capture also needs `gstreamer1.0-tools` + plugins. `cloudflared` in `bin/` is
optional (off-LAN only).

## How it works

- `theme/friends.custom.css` — the whole reskin (loaded via Millennium's
  `quickcss.css`, layered on top of your active theme).
- `plugin/.millennium/Dist/index.js` — hand-written Millennium frontend module
  (no build step) that runs in SharedJSContext, reaches the friends popup via
  `g_PopupManager`, does the DOM tweaks (voice→header, placeholder, call stage),
  and drives the share daemon over `127.0.0.1:48592`.
- `rp-webrtc.py` — the cross-platform screen-share daemon (control API + capture).
- `bin/rp-mediamtx.yml` — MediaMTX config (RTSP ingest + WebRTC/WHEP).
- `tools/` — CDP helpers used during development to inspect/inject live.
- `docs/recon-friends-dom.md` — live DOM recon notes (selectors).

## Notes / limits

- Steam's voice UI is global (roster header), so the call stage is scoped to the
  named group you're viewing rather than bound to a single chat.
- Per-window share can't capture a **minimized** window (no frames are presented).
- On Steam clients running Millennium ≥ v3, CEF uses a debugging *pipe* rather than a
  TCP port, so the `tools/` CDP helpers (which expect a port) won't attach unless you
  create `.cef-enable-remote-debugging` in your Steam root.
