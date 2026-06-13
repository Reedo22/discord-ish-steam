# Steam Chat Reskin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reskin Steam's friends/chat window into a Discord-style layout using a Millennium theme (plus a tiny plugin only if needed), keeping Steam's real voice/screenshare intact.

**Architecture:** A Millennium *theme* (`friends.custom.css`) does the bulk of the work, restyling the friends/chat CEF window in place. A small Millennium *plugin* (injected JS) is added only if recon proves the call/screenshare controls can't be relocated with CSS alone. Nothing touches Steam's network or call pipeline.

**Tech Stack:** Millennium (Steam theme/plugin loader), vanilla CSS, Chrome DevTools against Steam's CEF remote-debug port, optional JS + Python plugin.

---

## Important context for whoever executes this

- This runs on the developer's **Linux** machine (Pop!_OS). Steam stores its CEF UI as a Chromium web app.
- Steam's friends/chat window uses **generated, hashed CSS class names** (e.g. `friendsListContent_HASH`). The semantic prefix before the hash is stable across the same Steam build, so we target it with attribute-contains selectors: `[class*="friendsListContent"]`. **The exact prefixes are unknown until Task 2 inspects the live DOM** — that is what the recon file captures. Every styling task below references the recon file for its selectors.
- There are no unit tests here — this is UI theming. "Verification" means reloading the window and confirming the change in DevTools and visually. That is a legitimate, required step, not a shortcut.
- Millennium paths on Linux:
  - Themes (skins): `~/.local/share/millennium/skins/`
  - Plugins: `~/.local/share/millennium/plugins/`
- Commit after every task.

---

## File Structure

- `theme/skin.json` — Millennium theme manifest. Declares the theme and uses `UseDefaultPatches` so the `friends.custom.css` filename auto-targets the friends/chat window.
- `theme/friends.custom.css` — the entire reskin (layout shell, sidebar, chat pane, messages, input, call/screenshare header, color palette). One focused file; sectioned with comments.
- `docs/recon-friends-dom.md` — output of Task 2: the confirmed selector map for every UI element we restyle. Source of truth the CSS tasks read from.
- `plugin/plugin.json`, `plugin/backend/main.py`, `plugin/public/reskin.js` — **only created in Task 9, only if Task 2 finds CSS can't relocate the screenshare button.**
- `install-dev.sh` — symlinks `theme/` into the Millennium skins dir so edits are live without copying.

---

## Task 1: Install Millennium and prove the injection loop

**Files:**
- None in repo yet (environment setup).

- [ ] **Step 1: Install Millennium on Linux**

Run the official installer (from https://steambrew.app — confirm the current command on the site before running):

```bash
curl -fsSL https://steambrew.app/install.sh | sh
```

- [ ] **Step 2: Restart Steam and confirm Millennium loaded**

Fully quit Steam (`steam -shutdown`) then relaunch it. Open Steam → **Settings**. 
Expected: a **Millennium** (or "Themes"/"Plugins") section is present in Settings. If it's missing, Millennium did not load — stop and resolve before continuing.

- [ ] **Step 3: Confirm the skins directory exists**

Run: `ls -la ~/.local/share/millennium/skins/`
Expected: the directory exists (may be empty). If it doesn't exist, create it: `mkdir -p ~/.local/share/millennium/skins/`

- [ ] **Step 4: Commit a note recording the working Millennium version**

```bash
millennium --version > docs/millennium-version.txt 2>&1 || echo "see Steam Settings > Millennium" > docs/millennium-version.txt
git add docs/millennium-version.txt
git commit -m "chore: record working Millennium version"
```

---

## Task 2: Recon — map the friends/chat window DOM

**Files:**
- Create: `docs/recon-friends-dom.md`

- [ ] **Step 1: Enable Steam's CEF remote debugging**

Create the marker file in the Steam install dir and restart Steam:

```bash
touch ~/.local/share/Steam/.cef-enable-remote-debugging
steam -shutdown; sleep 5; steam &
```

(If Steam is installed elsewhere, find it: `find ~ -maxdepth 4 -name steamui -type d 2>/dev/null` and create the marker in that install root.)

- [ ] **Step 2: Open the friends window and attach DevTools**

Open the Steam **Friends & Chat** window (Friends list), then open a real conversation with someone so the chat pane and call/screenshare controls are visible. In a Chromium-based browser, go to:

```
http://localhost:8080
```

Expected: a list of inspectable targets. Click the one whose title matches the Friends/Chat window. The DevTools Elements panel opens on the live friends window.

- [ ] **Step 3: Capture the selector for each element and write the recon file**

Using the DevTools element picker, click each element below and record the **semantic class prefix** (the part before the random hash). Write `docs/recon-friends-dom.md` with this exact structure, filling the right column with real prefixes:

```markdown
# Friends/Chat DOM Recon (Steam build: <paste Steam build number>)

| Element                         | Selector to use                          | Notes |
|---------------------------------|------------------------------------------|-------|
| Friends-window root / body      | `[class*="..."]` or `body`               |       |
| Friends list container (left)   | `[class*="..."]`                         |       |
| A single friend row             | `[class*="..."]`                         |       |
| Friend avatar                   | `[class*="..."]`                         |       |
| Online/status indicator         | `[class*="..."]`                         |       |
| Search box                      | `[class*="..."]`                         |       |
| Open-chat / main pane container | `[class*="..."]`                         |       |
| Chat header (name bar)          | `[class*="..."]`                         |       |
| Voice/call button               | `[class*="..."]`                         |       |
| Screenshare button              | `[class*="..."]`                         |       |
| Message list                    | `[class*="..."]`                         |       |
| A single message row            | `[class*="..."]`                         |       |
| Message author / timestamp      | `[class*="..."]`                         |       |
| Message text input              | `[class*="..."]`                         |       |

## Screenshare relocation finding
- Where does the screenshare button currently live in the DOM tree? (describe its parent chain)
- Can it be moved into the chat header purely with CSS (flex reordering, `order`, absolute positioning within a shared ancestor)?  YES / NO
- If NO: what is the minimum DOM move needed? (this decides whether Task 9's plugin is required)
```

- [ ] **Step 4: Commit the recon file**

```bash
git add docs/recon-friends-dom.md
git commit -m "docs: friends/chat DOM selector recon map"
```

---

## Task 3: Theme skeleton + prove CSS reaches the friends window

**Files:**
- Create: `theme/skin.json`
- Create: `theme/friends.custom.css`
- Create: `install-dev.sh`

- [ ] **Step 1: Write the theme manifest**

Create `theme/skin.json`:

```json
{
  "name": "Discord-ish Chat",
  "description": "Discord-style reskin of the Steam friends/chat window.",
  "author": "reedo",
  "version": "0.1.0",
  "UseDefaultPatches": true
}
```

(`UseDefaultPatches: true` makes Millennium auto-inject any `friends.custom.css` in this folder into the friends/chat window. Do not also add `Patches`/`Steam-Webkit` while this is true.)

- [ ] **Step 2: Write a deliberately obvious test rule**

Create `theme/friends.custom.css`:

```css
/* === Discord-ish Steam chat reskin === */

/* TEMP smoke test: make the whole friends window background hot pink.
   This only exists to prove injection works; replaced in Task 4. */
body {
  background: #ff00ff !important;
}
```

- [ ] **Step 3: Write the dev install script (symlink so edits are live)**

Create `install-dev.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
SKINS="$HOME/.local/share/millennium/skins"
mkdir -p "$SKINS"
ln -sfn "$(pwd)/theme" "$SKINS/discord-ish-chat"
echo "Linked theme -> $SKINS/discord-ish-chat"
echo "Now enable 'Discord-ish Chat' in Steam Settings > Millennium > Themes, then reload."
```

- [ ] **Step 4: Install, enable, and verify the smoke test**

Run: `bash install-dev.sh`
Then in Steam: **Settings → Millennium → Themes**, select "Discord-ish Chat", and reload (Millennium's reload button, or restart Steam). Open the Friends window.
Expected: the friends window background is hot pink. This confirms `friends.custom.css` is reaching the right window. If nothing changes, the filename/targeting is wrong — re-check `skin.json` and that the theme is enabled before continuing.

- [ ] **Step 5: Commit**

```bash
chmod +x install-dev.sh
git add theme/skin.json theme/friends.custom.css install-dev.sh
git commit -m "feat: theme skeleton + verified friends-window CSS injection"
```

---

## Task 4: Layout shell — two-pane Discord structure

**Files:**
- Modify: `theme/friends.custom.css`

Uses selectors from `docs/recon-friends-dom.md`: "Friends list container (left)" and "Open-chat / main pane container". Replace the bracketed prefixes below with the recon values.

- [ ] **Step 1: Replace the smoke test with the palette + layout shell**

In `theme/friends.custom.css`, delete the hot-pink rule and write:

```css
/* === Discord-ish Steam chat reskin === */

/* ---- Palette (Discord dark) ---- */
:root {
  --dc-bg:        #313338;  /* main chat area */
  --dc-sidebar:   #2b2d31;  /* friends sidebar */
  --dc-darkest:   #1e1f22;  /* search / inset wells */
  --dc-text:      #dbdee1;
  --dc-muted:     #949ba4;
  --dc-accent:    #5865f2;  /* blurple */
  --dc-online:    #23a55a;
  --dc-idle:      #f0b232;
  --dc-offline:   #80848e;
  --dc-hover:     #35373c;
}

/* ---- Layout shell ---- */
/* Friends list = fixed-width left sidebar */
[class*="<friends-list-container-prefix>"] {
  background: var(--dc-sidebar) !important;
  width: 280px !important;
  min-width: 280px !important;
  border-right: 1px solid var(--dc-darkest) !important;
}

/* Open conversation = flexible main pane */
[class*="<main-pane-container-prefix>"] {
  background: var(--dc-bg) !important;
  flex: 1 1 auto !important;
}
```

- [ ] **Step 2: Verify the two-pane layout**

Reload the friends window. Open a conversation.
Expected: left list is a fixed ~280px dark sidebar, the conversation fills the rest with a slightly lighter background. Use DevTools to confirm the rules applied (no strikethrough = selector matched). If a selector didn't match, fix the prefix from recon.

- [ ] **Step 3: Commit**

```bash
git add theme/friends.custom.css
git commit -m "feat: Discord palette + two-pane layout shell"
```

---

## Task 5: Friends sidebar — rows, avatars, status dots

**Files:**
- Modify: `theme/friends.custom.css`

Uses recon selectors: "A single friend row", "Friend avatar", "Online/status indicator", "Search box".

- [ ] **Step 1: Append sidebar styling**

Append to `theme/friends.custom.css`:

```css
/* ---- Friends sidebar rows ---- */
[class*="<friend-row-prefix>"] {
  border-radius: 6px !important;
  margin: 1px 8px !important;
  padding: 4px 8px !important;
  color: var(--dc-muted) !important;
}
[class*="<friend-row-prefix>"]:hover {
  background: var(--dc-hover) !important;
  color: var(--dc-text) !important;
}
[class*="<avatar-prefix>"] {
  border-radius: 50% !important;
  width: 32px !important;
  height: 32px !important;
}
/* status dot */
[class*="<status-indicator-prefix>"] {
  border: 3px solid var(--dc-sidebar) !important;
  border-radius: 50% !important;
}

/* ---- Search box ---- */
[class*="<search-box-prefix>"] {
  background: var(--dc-darkest) !important;
  color: var(--dc-text) !important;
  border: none !important;
  border-radius: 4px !important;
}
```

- [ ] **Step 2: Verify**

Reload. Expected: friend rows are rounded, highlight on hover, avatars are circular with a ringed status dot; search box is a dark inset well. Confirm each rule matched in DevTools.

- [ ] **Step 3: Commit**

```bash
git add theme/friends.custom.css
git commit -m "feat: Discord-style friends sidebar (rows, circular avatars, status dots)"
```

---

## Task 6: Chat pane — message list and message rows

**Files:**
- Modify: `theme/friends.custom.css`

Uses recon selectors: "Message list", "A single message row", "Message author / timestamp".

- [ ] **Step 1: Append message styling**

Append to `theme/friends.custom.css`:

```css
/* ---- Message list ---- */
[class*="<message-list-prefix>"] {
  background: var(--dc-bg) !important;
  padding: 8px 16px !important;
}
[class*="<message-row-prefix>"] {
  color: var(--dc-text) !important;
  padding: 2px 0 !important;
  line-height: 1.375 !important;
}
[class*="<message-row-prefix>"]:hover {
  background: rgba(2,2,2,0.06) !important;
}
[class*="<message-author-prefix>"] {
  color: #f2f3f5 !important;
  font-weight: 600 !important;
}
```

- [ ] **Step 2: Verify**

Reload, open a chat with history. Expected: messages on the dark background, readable light text, bold author names, subtle hover highlight per message. Confirm in DevTools.

- [ ] **Step 3: Commit**

```bash
git add theme/friends.custom.css
git commit -m "feat: Discord-style chat message list and rows"
```

---

## Task 7: Message input box

**Files:**
- Modify: `theme/friends.custom.css`

Uses recon selector: "Message text input".

- [ ] **Step 1: Append input styling**

Append to `theme/friends.custom.css`:

```css
/* ---- Message input ---- */
[class*="<message-input-prefix>"] {
  background: #383a40 !important;
  color: var(--dc-text) !important;
  border: none !important;
  border-radius: 8px !important;
  margin: 0 16px 16px !important;
  padding: 11px 16px !important;
}
[class*="<message-input-prefix>"]::placeholder {
  color: var(--dc-muted) !important;
}
```

- [ ] **Step 2: Verify**

Reload. Expected: the message box is a single rounded pill with padding, matching Discord's composer. Confirm in DevTools.

- [ ] **Step 3: Commit**

```bash
git add theme/friends.custom.css
git commit -m "feat: Discord-style message input composer"
```

---

## Task 8: Call/screenshare header (CSS path)

**Files:**
- Modify: `theme/friends.custom.css`

Uses recon selectors: "Chat header (name bar)", "Voice/call button", "Screenshare button". 
**Do this task only if the recon file's "Screenshare relocation finding" says relocation is possible with CSS (YES).** If it says NO, skip to Task 9 instead, then return here for the styling parts.

- [ ] **Step 1: Append header promotion + button styling**

Append to `theme/friends.custom.css`:

```css
/* ---- Chat header with promoted call/screenshare ---- */
[class*="<chat-header-prefix>"] {
  background: var(--dc-bg) !important;
  border-bottom: 1px solid var(--dc-darkest) !important;
  display: flex !important;
  align-items: center !important;
  padding: 0 16px !important;
  min-height: 48px !important;
}

/* Promote call + screenshare to the right of the header, made prominent */
[class*="<call-button-prefix>"],
[class*="<screenshare-button-prefix>"] {
  order: 99 !important;            /* push to end of the flex header */
  margin-left: 8px !important;
  width: 36px !important;
  height: 36px !important;
  border-radius: 8px !important;
  color: var(--dc-text) !important;
  background: transparent !important;
}
[class*="<call-button-prefix>"]:hover,
[class*="<screenshare-button-prefix>"]:hover {
  background: var(--dc-hover) !important;
}
```

- [ ] **Step 2: Verify**

Reload, open a chat. Expected: call and screenshare buttons sit prominently at the right end of the chat header and still launch Steam's real call/screenshare when clicked (test an actual call with a friend, or confirm the click handler fires in DevTools). Confirm the buttons are reachable and the original Steam behavior is unchanged.

- [ ] **Step 3: Commit**

```bash
git add theme/friends.custom.css
git commit -m "feat: promote and restyle call/screenshare controls in chat header"
```

---

## Task 9: Plugin to relocate screenshare button (ONLY IF recon said CSS-relocation = NO)

**Files:**
- Create: `plugin/plugin.json`
- Create: `plugin/backend/main.py`
- Create: `plugin/public/reskin.js`

Skip this entire task if Task 2's recon finding said the screenshare button can be relocated with CSS alone (Task 8 already handled it). This task exists because some controls have handlers bound to their original DOM position and must be physically moved in the DOM, which CSS cannot do.

- [ ] **Step 1: Write the plugin manifest**

Create `plugin/plugin.json`:

```json
{
  "name": "discordish_chat",
  "common_name": "Discord-ish Chat Helper",
  "version": "0.1.0",
  "include": ["public"]
}
```

- [ ] **Step 2: Write the backend that injects the JS into the friends window**

Create `plugin/backend/main.py`:

```python
import Millennium  # provided by the Millennium runtime
import os, shutil

PLUGIN_NAME = "DiscordishChat"

def _deploy_js():
    # Copy our script into Steam's steamui so Millennium can inject it.
    src = os.path.join(os.path.dirname(__file__), "..", "public", "reskin.js")
    dst_dir = os.path.join(Millennium.steam_path(), "steamui", PLUGIN_NAME)
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dst_dir, "reskin.js"))

class Plugin:
    def _front_end_loaded(self):
        _deploy_js()

    def _load(self):
        _deploy_js()
        Millennium.add_browser_js(f"{PLUGIN_NAME}/reskin.js")
        Millennium.ready()

    def _unload(self):
        pass
```

(If `Millennium.steam_path()` is not the exact API name in the installed version, find the correct path helper in Steam Settings → Millennium → plugin docs, or read another installed plugin's `backend/main.py` under `~/.local/share/millennium/plugins/`.)

- [ ] **Step 3: Write the relocation script**

Create `plugin/public/reskin.js` — replace the two selector strings with the recon values:

```javascript
// Moves the screenshare button into the chat header so CSS can style it.
(function () {
  const HEADER = '[class*="<chat-header-prefix>"]';
  const SHARE  = '[class*="<screenshare-button-prefix>"]';

  function relocate() {
    document.querySelectorAll(HEADER).forEach((header) => {
      if (header.querySelector('[data-reskin-moved]')) return;
      // search the whole window for the screenshare control
      const share = document.querySelector(SHARE);
      if (share && !header.contains(share)) {
        share.setAttribute('data-reskin-moved', '1');
        header.appendChild(share);
      }
    });
  }

  // Steam re-renders the chat pane on conversation switch; re-run on DOM changes.
  const obs = new MutationObserver(() => relocate());
  obs.observe(document.body, { childList: true, subtree: true });
  relocate();
})();
```

- [ ] **Step 4: Install and verify the relocation**

```bash
ln -sfn "$(pwd)/plugin" "$HOME/.local/share/millennium/plugins/discordish-chat"
```

Enable the plugin in Steam Settings → Millennium → Plugins, restart Steam, open a chat.
Expected: the screenshare button now physically sits inside the chat header element (confirm in DevTools that its parent is the header), and clicking it still starts Steam screenshare. Then return to **Task 8** to style it.

- [ ] **Step 5: Commit**

```bash
git add plugin/plugin.json plugin/backend/main.py plugin/public/reskin.js
git commit -m "feat: plugin to relocate screenshare button into chat header"
```

---

## Task 10: Polish pass and final verification

**Files:**
- Modify: `theme/friends.custom.css`

- [ ] **Step 1: Walk the whole friends window and fix stragglers**

With the theme enabled, open the friends window and a conversation. Look for anything still showing Steam's old light/blue styling (scrollbars, context menus, group headers, tab bars). For each, pick its selector in DevTools and add a rule using the existing palette variables. Append them under a `/* ---- Polish ---- */` section. Common one — scrollbars:

```css
/* ---- Polish ---- */
[class*="<message-list-prefix>"]::-webkit-scrollbar { width: 8px; }
[class*="<message-list-prefix>"]::-webkit-scrollbar-thumb {
  background: var(--dc-darkest) !important;
  border-radius: 4px !important;
}
```

- [ ] **Step 2: Full manual verification against the spec**

Confirm each v1 deliverable from the spec is present:
- [ ] Discord-style friends sidebar with circular avatars and status dots
- [ ] Restyled chat pane with grouped, readable messages
- [ ] Rounded message composer
- [ ] Dark Discord-ish palette throughout (no leftover light/blue Steam chrome)
- [ ] Call + screenshare controls promoted to the chat header and still functional (place a real test call/screenshare with a friend)

- [ ] **Step 3: Write a short README so a future you (or a friend) can reproduce it**

Create `README.md` documenting: prerequisites (Millennium), `bash install-dev.sh`, enabling the theme, and the note that the Windows one-click installer for friends is deferred to v2.

- [ ] **Step 4: Commit**

```bash
git add theme/friends.custom.css README.md
git commit -m "feat: polish pass + README; v1 reskin complete"
```

---

## Out of scope (v2, do not build now)
- Windows one-click installer bundling Millennium + this theme for friends.
- Handling Steam updates that change selector prefixes (will require re-running Task 2's recon).
- Multiple theme variants / light mode.
