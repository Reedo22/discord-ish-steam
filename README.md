# discord-ish-steam

A Discord-style reskin of the Steam friends/chat client, built on
[Millennium](https://steambrew.app/). It does **not** replace Steam or touch its
networking/voice — it restyles the existing CEF web UI and adds a few DOM tweaks
that CSS can't do, so all of Steam's real voice/calling keeps working.

## What it does

- **Docked single-window layout** (requires Steam's "Dock chats to the friends
  list" setting on) — roster + chat in one window, themed dark like Discord.
- **Friends list**: one merged list (no In-Game/Online/Offline sections), status
  dots, circular avatars; **group chats pinned to the top**.
- **Composer**: rounded input, blue send button, circular emoji/attach icons,
  centered text, "Message <friend>…" placeholder.
- **Voice control** moved into a header bar at the top of the chat.
- **Discord-style center-stage call screen** (in group voice channels): half-screen
  participant tiles with name pills, speaking rings, mute badges; real Steam SVG
  control icons (mute/deafen/leave); minimize-to-corner toggle; auto-teardown when
  the call ends; only shown while viewing the call's group.

## Install (Linux)

Requires Millennium installed.

```bash
./install.sh   # writes CSS to Millennium quickcss + installs the plugin
```

Then restart Steam (`steam -shutdown` then relaunch). Make sure
**Settings → Friends & Chat → "Dock chats to the friends list"** is enabled.

## Update

The plugin **auto-updates on every Steam boot** — its backend (`plugin/backend/main.py`)
does a `git pull --ff-only` + refreshes quickcss in a background thread on load.
You can also update manually any time:

```bash
./update.sh   # git pull + reinstall; restart Steam to apply
```

## Windows port — checklist (do in a Windows session)

Most of the client is OS-agnostic (CSS theme + plugin: call UI, voice settings,
broadcast invite all work once installed). What needs finishing on Windows:

1. **Paths**: confirm Millennium's Windows dirs (quickcss / plugins / config.json —
   likely under `%LOCALAPPDATA%\Millennium`). Fix `install.ps1` + the backend
   `_paths()` Windows branch + `CAPTURE_EXE` in the plugin.
2. **Install**: run/fix `install.ps1` (clone → quickcss → copy plugin → enable).
3. **Screen capture**: `stream-capture.ps1` uses ffmpeg `gdigrab` (needs ffmpeg/
   ffplay on PATH). Verify capture + the `MONITORS` coords + launchOpts format
   (Windows uses `X,Y,W,H`; Linux uses `WxH+X+Y`) in `streamScreen()`.
4. **Auto-update**: backend git-pull on boot — confirm git available + repo path.

The repo is **public**, so cloning/auto-update works for friends.

## How it works

- `theme/friends.custom.css` — the whole reskin (loaded via Millennium's
  `quickcss.css`, layered on top of your active theme).
- `plugin/.millennium/Dist/index.js` — hand-written Millennium frontend module
  (no build step) that runs in SharedJSContext, reaches the friends popup via
  `g_PopupManager`, and does the DOM tweaks (voice→header, placeholder, call stage).
- `tools/` — CDP helpers used during development to inspect/inject live.
- `docs/` — design spec, implementation plan, and the live DOM recon notes.

## Screen sharing

The 🖥 button in the chat header opens a stream menu with monitor + quality
pickers and two one-click share buttons:

- **Remote Play to \<friend\>** (low latency) — mirrors the chosen monitor into a
  borderless capture window (`ffplay`/`gdigrab`) and uses
  [RemotePlayWhatever](https://github.com/m4dEngi/RemotePlayWhatever) to host a
  Remote Play Together session under Spacewar (AppID 480) and invite the friend.
  Steam streams the capture window over RPT — much lower latency than broadcast,
  and the friend doesn't need to own anything.
- **Broadcast to \<friend\>** — same capture mirror, shared via Steam Broadcast
  (Go Live + watch invite). Works for any friend but has ~7s delay.
- **Stop sharing** — ends the RPT session (`CloseGroup`) and closes the capture.

Requirements: `ffplay` (ffmpeg) on PATH, and RemotePlayWhatever — `install.sh`
fetches the Linux AppImage into `bin/` automatically; on Windows install it and
ensure `stream-capture.ps1` can find it.

## Notes / limits
- Steam's voice UI is global (roster header), so the call stage is scoped to the
  named group you're viewing rather than bound to a single chat.
- Windows is supported via `install.ps1` (`irm …/install.ps1 | iex`); it
  self-discovers the Millennium install. Dev target remains Linux.
