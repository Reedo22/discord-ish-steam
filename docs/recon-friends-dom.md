# Friends/Chat DOM Recon (live, via CEF debug port 36377)

Captured by attaching to Steam's CEF DevTools protocol and dumping the live DOM
of both windows. Steam build id: 1780352834.

## Architectural findings (these change the design — see design-impact section)

0. **RESOLVED: native single-window mode exists — enable "Dock chats to the
   friends list".** Steam ships a docked/combined mode (`bSingleWindowMode`,
   loc key `FriendSettings_DockChats`, command `ToggleDockedMode`). When on, the
   window root gains `.singlewindow` and the roster + chat live in ONE window.
   This is the layout the theme targets. **The theme assumes docked mode is on.**

1. **Without docking, roster and chats are SEPARATE CEF windows** (`Friends List`
   plus one window per conversation, with in-window `.ChatTabs`). CSS cannot
   merge separate OS windows — which is why we rely on the native docked mode
   above instead.

2. **No persistent call/screenshare buttons.**
   - In a DM the only voice control is `.VoiceToggle` ("Send a voice request")
     inside the composer toolbar (`.ChatMessageEntryVoice > .buttonsContainer`).
   - There is NO screenshare button until a voice call is active; the in-call
     controls (incl. screenshare) render only once a call starts and were not in
     the DOM during recon. The screenshare control still needs recon during a
     live call.

3. **Good news: class names are mostly stable and readable** (not hashed), so
   most selectors are plain class selectors, no `[class*=]` needed.

## Selector map — Friends List (roster) window

| Element                     | Selector                          | Notes |
|-----------------------------|-----------------------------------|-------|
| Window root                 | `.chat.fullheight.responsive`     | also `.friendsListContainer` |
| Roster container            | `.friendlist`                     | stable |
| Header / title bar          | `.friendListHeaderContainer`      | stable |
| Current user block          | `.currentUserContainer`           | + `.online/.offline` state |
| Current user avatar         | `.currentUserAvatar .avatarHolder`| |
| Quick-access favorites      | `.quickAccessFriends`             | |
| Favorite friend item        | `.favoriteElement`                | |
| A friend row                | `.friend`                         | + `.ingame/.offline/.online` |
| Avatar                      | `.avatarHolder`                   | + `.avatar` (img), `.avatarStatus` |
| Status (color)              | via `.friend.ingame/.offline/.online` | state on row + `.avatarStatus` |
| Friend name (quick access)  | `.playerName`                     | |
| Tab bar (Friends/etc.)      | `.socialTabContainer`             | `.friendTab`, `.tabLabel`, `.activeTab` |
| Search button               | `.searchIconButton`               | title="Search my friends list" |
| Add friend button           | `.addFriendButton`                | |
| Friends list body           | `.FriendsListContent`             | scrolling list area |
| List container              | `.friendlistListContainer`        | |
| Friend group header         | `.friendGroup` / `.groupName`     | |

## Selector map — Chat (conversation) window

| Element                     | Selector                          | Notes |
|-----------------------------|-----------------------------------|-------|
| Window root                 | `.multiChatDialog`                | |
| Title/tab bar               | `.ChatTabs` / `.chatTitleBar`     | per-window window controls |
| Chat tab item               | `.friend.ingame` inside tab list  | avatar + `.labelHolder` |
| Chat header (currently empty)| `.chatHeader`                    | EMPTY in DM — candidate insertion point |
| Chat dialog                 | `.chatWindow` / `.ChatRoomGroup`  | |
| Message scroll area         | `.chatHistoryScroll`              | |
| Message list                | `.chatHistory`                    | |
| A message / time divider    | `.msg`                            | `.msg.timeDivision` for date sep |
| Chat body                   | `.chatBody` / `.chatStack`        | |
| Composer wrapper            | `.chatEntry`                      | stable |
| Text input                  | `.chatEntry textarea`             | |
| Submit button               | `.chatEntry button[type=submit]`  | title="Submit" |
| Emoticon button             | `button[title="Emoticon Picker"]` | |
| Send Special button         | `button[title="Send Special"]`    | |
| Voice request toggle        | `.VoiceToggle`                    | title="Send a voice request"; the only call control in DM |
| Voice entry container       | `.ChatMessageEntryVoice`          | `.Inactive` when no call |
| Add-to-group button         | `.inviteAnotherFriendButton`      | |

## Docked single-window layout (what the theme targets)

Window title becomes `Friends List - <friend>`; root is `.chat.fullheight.singlewindow`.

```
.chat.singlewindow
 .chat.displayRow
  .friendsListContainer            LEFT rail (roster) — give it a fixed width + sidebar bg
   .friendlist
    .friendListHeaderContainer     header / title bar
     .friendListCollapse           collapse-rail button (Discord-like)
     .currentUserContainer.online  your avatar + name + status
     .quickAccessFriends           favorites strip
     .socialTabContainer           Friends tab + .searchIconButton + .addFriendButton
    .FriendsListContent
     .friendlistListContainer
      .friendGroup (.gameGroup / .onlineFriends / .offlineFriends)
       .groupName / .groupCount     group header
       .friend (.online/.offline/.ingame/.awayOrSnooze)
        .avatarHolder (.avatar img, .avatarStatus)
        .labelHolder → .playerName-ish name div + status sub-text
  .chatDialogs                     RIGHT pane (chat) — flex:1, main bg
   .chatWindow
    .chatHeader                    EMPTY — usable as a header insertion point
    .ChatRoomGroup
     .chatBody → .chatHistoryScroll → .chatHistory (.msg)
     .chatEntry                    composer: textarea + submit + emoticon + .VoiceToggle
```

Status colors today come from Steam vars (online green `rgb(145,194,87)`, etc.)
keyed off `.friend.online/.ingame/.offline`. Override per-state on `.friend` and
`.avatarStatus`.

## In-call (voice) UI — captured live during a group voice channel

Lives in the group's **details sidebar** (`detailsView` → `chatRoomVoiceChannel`,
~252px wide), NOT a center stage. No screenshare control in group voice (Steam
only offers screenshare in 1:1 calls / Remote Play).

| Element                     | Selector                                   | Notes |
|-----------------------------|--------------------------------------------|-------|
| Control bar                 | `.activeVoiceButtons`                      | row of buttons |
| Mute mic                    | `.VoiceControlPanelButton.ToggleMicrophoneButton` | title "Mute Microphone" |
| Deafen (output)             | `.VoiceControlPanelButton.ToggleVoiceOutputButton` | title "Disable Incoming Audio" |
| Leave call                  | `.VoiceControlPanelButton.chatEndVoiceChat`| title "Leave Voice Chat" |
| Participant list            | `.VoiceChannelParticipants`                | compact list of `.friend` rows |
| A participant               | `.VoiceChannelParticipants .friend`        | + `.speaking` when talking (ring hook) |
| Per-participant voice icons | `.voiceStatusIconsContainer` / `.voicestatusIcon.voiceStatusMic.disabled` / `.voiceStatusOutput.disabled` | muted indicators |

**Discord-copy status:** Phase 1 (done, CSS) = circular controls, red leave,
avatar tiles, speaking ring, red muted-mic. Phase 2 (not done) = relocate/enlarge
the voice panel into the main area as a center stage (needs JS via the plugin +
g_PopupManager, like the voice-move).

## Screenshare relocation finding
- The screenshare button does not exist in the DM DOM at rest. It appears only
  during an active voice call, in a voice/call panel not yet recon'd.
- Therefore "promote the screenshare button to a header" (old plan Task 8/9) is
  not applicable as written. Needs a live-call recon pass before we can style or
  move any in-call control. Lower priority than restyling what's always visible.
