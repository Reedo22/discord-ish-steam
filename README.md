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

### Windows — easiest: one-click installer

**[Download discord-ish-steam-setup.exe](https://github.com/Reedo22/discord-ish-steam/releases/latest/download/discord-ish-steam-setup.exe)**
(always the newest release), double-click it, and click **Yes** on the admin (UAC)
prompt. It installs everything — Millennium if it's missing, git, Python, ffmpeg, the
plugin + theme, host binaries — sets the screen-share helper to start at logon, and
restarts Steam. No terminal. Run it again any time to update.

> The EXE isn't code-signed, so Windows may show "Windows protected your PC" — click
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

Requires [Millennium](https://steambrew.app/) installed and run at least once.

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
(Linux X11: x11grab / gst ximagesrc;        (NVENC/VAAPI/QSV/          ├─▶ viewer <video>
 Linux Wayland: ScreenCast portal ->         AMF, else x264)           │
 PipeWire -> gst pipewiresrc;                                          │
 gdigrab on Windows; v4l2/dshow webcam)                                │
                            └──▶ fragmented-MP4 over WebSocket ────────┘
```

- **AV1 when the GPU can, H.264 otherwise** (Settings: Codec → Auto). Measured on
  real gameplay + scrolling-code clips (SSIM/PSNR): AV1 needs ~40–50% less bitrate than
  H.264 for the same quality; NVENC runs at preset p5 / tune ll / spatial AQ, 2 s GOP,
  1 s VBV. Defaults: 1440p60 @ 8 Mbps. AV1 encode needs RTX 40 / RX 7000 / Intel Arc;
  every viewer can decode it (Chromium ships a software AV1 decoder).
- **One capture, one encode, both transports.** ffmpeg's tee muxer feeds WebRTC
  (via MediaMTX) and the WebSocket/fMP4 path from the same encode.
- **Video goes direct, not through Cloudflare.** The daemon forwards its WebRTC UDP
  port (8189) on your router with **UPnP** (checked: it reads the mapping back, and
  reports double-NAT/CGNAT), so off-LAN viewers connect straight to you. A Cloudflare
  quick tunnel then only carries the tiny WebRTC handshake; the WebSocket/fMP4
  fallback uses it only when WebRTC can't connect. Cloudflare's terms don't allow
  serving video through their CDN on free plans, so this matters beyond latency.
- **Own domain (optional):** point an HTTPS reverse proxy at port 48890 (path
  `/screen/`, WebSocket upgrade, buffering off) and put
  `{"url": "https://your.domain"}` in `~/.config/discordish/public.json` (or set
  `DS_PUBLIC_URL`). Then no tunnel is used at all. Viewers in the same house get
  the LAN address too, since many routers don't loop back to their own public IP.
- **Every share has a random access key** in its URLs; the public-facing port
  answers nothing without it.
- **Desktop audio — everything except Steam.** Steam's own audio (the call's voices
  and any incoming share) is left out, the way Discord leaves out its own: otherwise the
  friend hears their own voice echoed back a second late, and two people sharing at
  once feed each other into a screeching loop. Linux: a private PipeWire recording
  stream with every app's playback linked in except Steam's (no virtual device; apps
  still play normally). Windows 10 2004+/11: WASAPI process loopback excluding Steam's
  webhelper process tree. Extra apps to leave out: `DS_AUDIO_EXCLUDE=vesktop,...`.
  Older systems fall back to the whole desktop mix (with a warning in the log).
- **Desktop audio** needs **no virtual audio device** and is encoded once as
  **Opus** and carried on BOTH transports. That needs ffmpeg ≥ 7.1 (Opus over RTMP);
  `bin/fetch-linux.sh` downloads a static build into `bin/` when the distro one is
  older, picking the oldest suitable branch whose NVENC still works on your driver.
  With an older ffmpeg, audio falls back to AAC on the WebSocket path only.
- **Per-window capture is occlusion-proof on Linux** (X11: grabs the window's own
  buffer via `ximagesrc xid`; Wayland: the compositor streams the window over
  PipeWire) — the window can be behind others, just not minimized. Windows uses
  `gdigrab` by title.
- **Wayland is fully supported** via the xdg-desktop-portal ScreenCast API: the
  first share shows the desktop's own picker dialog once, then the choice is
  remembered (portal restore token) and later shares start with no prompt.
  Window-share on Wayland always picks through the system dialog (compositors
  don't let apps enumerate other windows).
- Viewers only decode H.264, so any GPU/OS can watch. Click the tile to enlarge;
  in big mode a **Fullscreen** button fills the whole screen.

Host requirements: **Python 3**, **ffmpeg** on PATH, **MediaMTX** in `bin/`, and
(Windows desktop audio) **pyaudiowpatch** — all handled by the installer.
`cloudflared` in `bin/` is optional (off-LAN sharing only). Linux per-window
capture also needs `gstreamer1.0-tools` + plugins; Wayland additionally needs
`gstreamer1.0-pipewire` + `python3-dbus` + `python3-gi` (the installer adds them).

## How it works

- `theme/friends.custom.css` — the whole reskin (loaded via Millennium's
  `quickcss.css`, layered on top of your active theme).
- `plugin/.millennium/Dist/index.js` — hand-written Millennium frontend module
  (no build step) that runs in SharedJSContext, reaches the friends popup via
  `g_PopupManager`, does the DOM tweaks (voice→header, call stage, share UI,
  noise cancellation), and drives the share daemon over `127.0.0.1:48592`.
- `rp-webrtc.py` — the cross-platform screen-share daemon (control API + capture).
  Run `python rp-webrtc.py --selftest` to diagnose capture/encode/audio.
- `bin/installer.nsi` + `bin/build-installer.sh` — build the one-click Windows EXE\n  (`/S` silent, `/offline` = use the bundled install.ps1). CI builds and runs it on every\n  push; pushing a `v*` tag publishes it as a release.
- `bin/rp-mediamtx.yml` — MediaMTX config (RTSP ingest + WebRTC/WHEP).
- The control API (`127.0.0.1:48592`) only answers Steam's own pages
  (`https://steamloopback.host`, `*.steampowered.com`, `*.steamcommunity.com`) and
  non-browser clients (curl, the Lua backend). Any other web page open in a browser
  on the same machine is refused, so it can't start a share or read your tunnel URL.
  Refusals are logged (`[ctl] refused …`); add an origin with
  `DS_ALLOW_ORIGINS=https://example` if you really need one.

## Tests

```bash
tests/run.sh          # daemon + Lua backend (python unittest) + plugin (node --test)
```

Stdlib only (no pip installs). The daemon tests include an end-to-end run (real
ffmpeg → fMP4 pump → WebSocket viewer) that skips if ffmpeg is missing; the Lua
tests load `main.lua` through `libluajit` via ctypes and skip without it; the
plugin tests need Node ≥ 18.

## Notes / limits

- **No plugin backend (on purpose).** Millennium 3 holds Steam's whole window back until
  every plugin backend reports ready, with no timeout; an older Millennium build hangs
  Steam even when it does. So the plugin ships `useBackend: false` and the screen-share
  daemon is started by systemd (Linux) / a logon task (Windows) instead.
  `plugin/backend/main.lua` (which calls `millennium.ready()`) stays in the repo for
  when every install is on Millennium ≥ 3.6.
- **Keep Millennium current.** A June 2026 Linux build loaded its frontend library but
  never initialized against the current Steam client, so no plugin ran at all. Update
  with Millennium's installer (the Windows installer here does it automatically).

- Steam's voice UI is global (roster header), so the call stage is scoped to the
  group/friend you're viewing rather than bound to a single chat.
- Per-window share can't capture a **minimized** window (no frames are presented).
- On Millennium ≥ v3, CEF uses a debugging *pipe* rather than a TCP port, so the
  `tools/` CDP helpers won't attach (deploy = commit → restart Steam; the plugin
  self-updates).
