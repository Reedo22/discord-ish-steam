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
./install.sh          # writes CSS to Millennium quickcss + installs the plugin
./setup-autoupdate.sh # optional: daily auto-update via a systemd user timer
```

Then restart Steam (`steam -shutdown` then relaunch). Make sure
**Settings → Friends & Chat → "Dock chats to the friends list"** is enabled.

## Update

```bash
./update.sh   # git pull + reinstall; restart Steam to apply
```

## How it works

- `theme/friends.custom.css` — the whole reskin (loaded via Millennium's
  `quickcss.css`, layered on top of your active theme).
- `plugin/.millennium/Dist/index.js` — hand-written Millennium frontend module
  (no build step) that runs in SharedJSContext, reaches the friends popup via
  `g_PopupManager`, and does the DOM tweaks (voice→header, placeholder, call stage).
- `tools/` — CDP helpers used during development to inspect/inject live.
- `docs/` — design spec, implementation plan, and the live DOM recon notes.

## Notes / limits

- Steam has **no screen sharing in DMs/chat** (only Remote Play / Broadcast game
  streaming), so there's no screenshare control to add.
- Steam's voice UI is global (roster header), so the call stage is scoped to the
  named group you're viewing rather than bound to a single chat.
- Windows installer for non-technical friends is not done yet (dev target is Linux).
