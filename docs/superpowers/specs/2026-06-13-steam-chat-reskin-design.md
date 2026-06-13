# Steam Chat Reskin — Design

**Date:** 2026-06-13
**Status:** Approved (design), pending spec review

## Problem

Steam's friends/chat client works fine under the hood — voice quality, screen
sharing, and the network pipeline are all good — but the interface feels dated
("like 2007"). The goal is a modern, Discord-style interface wrapped around the
*existing* Steam client, keeping all of Steam's real calling and screen-sharing
functionality intact.

## Key Insight

Since the 2023 Steam client rewrite, the friends/chat UI is a Chromium (CEF)
web app — React + CSS that Valve calls "SteamUI." The proprietary, high-quality
parts (voice, Remote Play / screen sharing, the network protocol) live in the
client binary; the *interface* is just web tech layered on top. That means we
can modernize the interface **in place** by injecting CSS/JS, without
reimplementing or replacing any of the parts that already work well.

We are strictly restyling the web UI. Nothing we build touches the network or
the call/video pipeline.

## Revision (2026-06-13, post-recon)

Live CEF recon changed two assumptions:

1. **Single-window Discord layout is a native Steam feature.** Enabling "Dock
   chats to the friends list" (`bSingleWindowMode`) puts the roster and the open
   chat in ONE window (`.singlewindow`). The theme targets this docked window —
   no window-merging hacks needed. **Docked mode being on is a prerequisite.**
2. **There is no persistent call/screenshare button to promote.** In a DM the
   only voice control is a "Send a voice request" toggle (`.VoiceToggle`) in the
   composer; screenshare appears only during a live call. So promoting
   call/screenshare to a header is **descoped from v1**; we restyle the voice
   toggle and revisit in-call controls later (needs a live-call recon).

Confirmed selectors live in `docs/recon-friends-dom.md`. Class names are mostly
stable and readable, so the restyle is low-risk.

## Approach (chosen)

Build on **Millennium**, an existing, maintained theme + plugin loader for
Steam. It already solves the hard, fragile parts: injecting CSS/JS into Steam's
CEF, surviving Steam updates, providing a plugin API, and shipping a Windows
installer we can later piggyback on.

Approaches considered and rejected:
- **Hand-rolled loader in Rust** (attach to CEF's remote-debug websocket and
  inject ourselves). Appealing for Rust learning, but reinvents what Millennium
  already does and is more fragile across Steam updates. Deferred — could
  revisit if the learning goal outweighs shipping.
- **Pure CSS patch** (SteamFriendsPatcher-style). Too limited: with no
  JavaScript we can restyle but cannot relocate/rewire controls (e.g. pulling
  the screenshare button into a new header).

## Architecture

A single **Millennium package** consisting of:

- **Theme (CSS)** — does most of the work. Restyles the chat window into a
  Discord-style layout: dark palette, persistent friends sidebar with status
  dots, modern message grouping, restyled message input, and a prominent
  call/screenshare header.
- **Plugin (TypeScript)** — only for what CSS cannot do. Primarily relocating
  the existing call/screenshare controls into the promoted header, and any DOM
  regrouping the visual design needs. Kept as small as possible.

Millennium injects both into Steam's CEF chat window at runtime. No part of our
code touches networking or the call pipeline.

## Target Layout (Discord-style)

```
┌──────────┬────────────────────────────────────────┐
│ FRIENDS  │  ▣ Friend Name          [📞] [🖥 Share] │
│          │────────────────────────────────────────│
│ 🟢 Alice │   Alice  10:02                          │
│ 🟢 Bob   │   hey what's up                         │
│ 🟡 Carol │                                         │
│ ⚫ Dave  │   You  10:03                            │
│ ⚫ Erin  │   not much, wanna play                  │
│ [search] │────────────────────────────────────────│
│ ⚙ you    │  [ Message Alice…              ] [send] │
└──────────┴────────────────────────────────────────┘
```

- Persistent friends list on the left with status colors.
- Conversation pane on the right.
- Call + screenshare controls promoted to a prominent header (currently buried),
  still wired to Steam's real call/screenshare functions.

## Scope

### v1 (this milestone)
Get it working **on the developer's Linux machine only**, via a normal
Millennium install. Deliverables:
- Friends sidebar (Discord-style list, avatars, status colors).
- Restyled chat/conversation pane (message grouping, modern input).
- Dark Discord-ish theme.
- Promoted call/screenshare header wired to the existing Steam controls.

This is the "build on Millennium and see how useful it is" checkpoint.

### v2 (deferred, only if v1 proves worth it)
- Windows one-click installer bundling Millennium + this theme, for non-technical
  friends (all friends are on Windows; only the developer is on Linux).
- Polish: handling Steam updates that break selectors, multiple theme variants.

### Out of scope
- Reimplementing voice, screen sharing, or the network protocol.
- Any "new" kind of screen share beyond what Steam already provides.
- Auto-update, signed installers, public-release support burden.

## Implementation Order

1. **Recon first, not styling.** The biggest unknown is how much of Steam's chat
   DOM/CSS we can reach and restyle — especially whether the screenshare button
   can be relocated. Attach Chrome DevTools to Steam's CEF remote-debugging port,
   map the live DOM and class structure, and confirm what's feasible vs.
   wishful **before** writing any theme.
2. Set up Millennium on the Linux dev machine; get a trivial CSS injection
   showing up in the chat window (proves the loop works).
3. Build the theme incrementally against the recon map: layout shell → friends
   sidebar → chat pane → message styling → input.
4. Build the minimal TS plugin to relocate the call/screenshare controls into
   the promoted header.
5. Evaluate usefulness on the dev machine. Decide whether to pursue v2.

## Risks

- **DOM/CSS reachability** — Steam's CEF UI uses generated/obfuscated class
  names that change across updates. Mitigated by recon-first and keeping the
  plugin's DOM assumptions minimal. This is the primary feasibility risk.
- **Relocating the screenshare button** may not be cleanly possible if its
  handler is tightly bound to its original DOM position. Recon resolves this.
- **Millennium lifecycle dependency** — we ride Millennium's maintenance and
  compatibility. Acceptable for a personal/friends-scope project.
