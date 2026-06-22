# discord-ish-steam — Windows one-click installer (`discord-ish-steam-setup.exe`)

**Date:** 2026-06-21
**Status:** approved (design)

## Problem

The friend's install half-worked: desktop audio wasn't installed and he has to
launch the screen-share daemon (`rp-webrtc.py`) by hand. Root cause is **not**
missing features — `install.ps1` already (a) installs `pyaudiowpatch` for WASAPI
loopback audio and (b) registers a logon scheduled task that auto-starts the
daemon. The symptoms are what happens when `install.ps1` runs **non-elevated**:
the `irm | iex` one-liner has no admin rights, so the winget package installs and
the scheduled-task registration silently degrade.

Goal: a **double-click installer that guarantees elevation**, auto-installs every
prerequisite (so the friend needs nothing but Steam + Millennium), and then runs
the existing `install.ps1` unchanged in spirit. The maintainer (Reedo) just
pushes updates; the friend never touches a terminal.

## Non-goals (YAGNI)

- Code signing. SmartScreen will warn "unknown publisher"; acceptable for a
  friend. Signing is a paid cert + separate effort.
- Add/Remove Programs uninstaller entry. Can add a small uninstaller later.
- Replacing the `irm | iex` one-liner — it stays for power users.

## Approach — thin elevating bootstrapper (NSIS EXE)

The `.exe` is a wrapper. **All install logic stays in `install.ps1`** (single
source of truth, keeps Linux parity intact). The EXE only guarantees elevation
and a double-click UX.

```
discord-ish-steam-setup.exe   (NSIS, RequestExecutionLevel admin)
   │  UAC prompt on launch
   ├─ extract bundled install.ps1 → $TEMP
   ├─ try: irm <raw github>/master/install.ps1 → overwrite with latest (online)
   │       (on any failure, keep the bundled copy — offline-safe)
   └─ powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1   (elevated)
            └─ existing install.ps1:
               winget: git + Python + ffmpeg     ← git ADDED this round
               pip:    pyaudiowpatch
               clone/pull repo, install plugin + theme, enable in config.json
               fetch mediamtx.exe + cloudflared.exe
               register logon task + start daemon  ← now elevated, so it sticks
```

## Components

1. **`bin/installer.nsi`** — NSIS script.
   - `RequestExecutionLevel admin` (UAC manifest baked into the EXE).
   - Bundles `install.ps1` via `File`.
   - On run: extract to `$TEMP`, attempt fresh fetch of `install.ps1` from
     GitHub raw (PowerShell `irm`, TLS 1.2, swallow failures → keep bundled).
   - Execute `powershell -NoProfile -ExecutionPolicy Bypass -File "$TEMP\install.ps1"`,
     streaming output to the NSIS log window so failures are visible.
   - Final page: "Done — fully restart Steam, then enable 'Dock chats to the
     friends list'."
   - Output name: `discord-ish-steam-setup.exe`.

2. **`bin/build-installer.sh`** — runs `makensis bin/installer.nsi` on Linux,
   emits `dist/discord-ish-steam-setup.exe`. Installs `makensis` guidance if
   missing. Repeatable / CI-able.

3. **`install.ps1`** — one change: add `Git.Git` to the existing winget
   auto-install block (alongside Python + ffmpeg), with the same "winget absent"
   warning fallback. Running `install.ps1` directly is unchanged for power users.

## Portability

Purely additive Windows artifact. The Linux path (`install.sh` + systemd) is
untouched. The only shared-file change is one winget line in `install.ps1`, which
never executes on Linux.

## Distribution

The `.exe` is published as a **GitHub Release asset** (not committed to the repo
tree — binaries bloat history). Build locally, then
`gh release create/upload`. `dist/` is gitignored. README keeps the power-user
one-liner and adds a "Just want it to work? Download setup.exe" link.

## Test plan

1. **Wine smoke test (required before any push).** Validates *bootstrapper
   mechanics only*: the EXE launches under Wine, shows its UI, extracts
   `install.ps1`, and invokes PowerShell. Wine has no winget/Steam/Millennium, so
   `install.ps1` itself cannot complete here — that is expected and not a failure.
2. **Windows VM end-to-end (real validation).** Run the EXE on the VM: confirm
   UAC prompt, winget installs git/Python/ffmpeg, pyaudiowpatch installs, plugin
   + theme land, daemon auto-starts at logon, `--selftest` passes.
3. Only after both: build final EXE, publish Release, tell the friend to
   download + double-click.

## Risks

- **Wine coverage is shallow** by design — green Wine ≠ working install. The VM
  is the gate that matters.
- **GitHub fresh-fetch could pull a broken `install.ps1`.** Mitigated: bundled
  copy is the fallback, and we only fetch over TLS 1.2 with failures swallowed.
- **winget may be absent on older Windows.** `install.ps1` already warns and
  degrades; the friend's machine has winget (modern Win10/11).
