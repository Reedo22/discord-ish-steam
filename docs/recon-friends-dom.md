# Friends/Chat DOM Recon (live, via CEF debug port 36377)

Captured by attaching to Steam's CEF DevTools protocol and dumping the live DOM
of both windows. Steam build id: 1780352834.

## Architectural findings (these change the design — see design-impact section)

1. **Roster and chats are SEPARATE CEF windows.**
   - `Friends List` window = the roster.
   - Each open conversation is its own window (e.g. `John Mantle Holder`), with
     in-window **chat tabs** (`.ChatTabs`, `.multiChatDialog`) so multiple chats
     can tab inside one chat window.
   - There is no native single-window "sidebar + chat" (Discord) layout in the
     current state. CSS cannot merge two OS windows into one.

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

## Screenshare relocation finding
- The screenshare button does not exist in the DM DOM at rest. It appears only
  during an active voice call, in a voice/call panel not yet recon'd.
- Therefore "promote the screenshare button to a header" (old plan Task 8/9) is
  not applicable as written. Needs a live-call recon pass before we can style or
  move any in-call control. Lower priority than restyling what's always visible.
