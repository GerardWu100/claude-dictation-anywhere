# Trail notes

## Decisions
- 2026-10-31 macOS shortcut hiding: the event tap is active (can drop events) only when the shortcut has a non-modifier key (letter, F-key, mouse button); modifier-only shortcuts keep a listen-only tap, which cannot add input latency. Tested live: Command+Option+G dictates with no stray G.
- 2026-10-31 macOS banner is a non-activating, click-through NSPanel pill above the Dock, not `osascript display notification` (that lands in Notification Center history under Script Editor's icon).

- 2026-10-31 macOS input uses a direct Quartz event tap (`mac_events.py`), not pynput. pynput 1.8.2 reports every mouse button past the third as `Button.middle` (side buttons unusable) and gives no sign when Input Monitoring is missing. Shortcuts are stored as `key:<key code>` / `mouse:<button number>`, so they follow the physical key under any layout.
- 2026-10-31 macOS refuses to type when the front app changed between release and delivery; the text stays on the clipboard. The front app is read at release, not at press, so nothing delays the first Space.

- 2026-10-25 Drive the real Claude Code CLI in a pty instead of calling its voice WebSocket with the token from `~/.claude/.credentials.json`. Anthropic's consumer terms (Feb 2026) allow subscription OAuth only inside Claude Code and Claude.ai; a homemade client risks the account.
- 2026-10-25 Settings and history live on a localhost browser page, not a native window. A Tk window was built and rejected by the user as ugly (Tk 9 on GNOME: tiny fonts, huge checkboxes); the browser page is simpler.
- 2026-10-25 The hidden session runs in its own empty folder (`<data dir>/dictation`), outside any git repo, because Claude Code feeds the folder name and git branch to dictation as word hints. The app answers the first-run "trust this folder" prompt itself; the session has no tools, so that is safe.
- 2026-10-31 Silence other audio during recording by muting the default output, not by pausing players (players live in different browsers/apps; MPRIS pause was unreliable for the user) and not by setting volume 0 (mute leaves the level untouched). Mute after the start sound ends, unmute at release rather than after transcription.
- 2026-10-31 Keep the hidden Claude Code running by default (`idle_shutdown_minutes = 0`). A cold start costs 1-3 s, and users start talking at the press, so the first words were lost after every idle break; 250 MB is the price.
- 2026-10-31 Hidden-session memory (PSS: resident memory with shared pages split fairly, after 8 s, no children): `--safe-mode --tools ""` 184 MB; plus `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 168 MB; plus `--strict-mcp-config --disable-slash-commands` 164 MB (kept). Voice still records with all of them. Dropping `--safe-mode` *looks* smaller (138 MB) only because startup hangs on a "Claude in Chrome detected" dialog, and it would run the user's SessionStart hooks and plugins: keep it. Every feature beyond the transcript (history, mute, typing, clipboard) lives in this app; Claude Code keeps no transcript files for the dictation folder.

## Assumptions

- 2026-10-31 After Input Monitoring is granted, the app retries the shortcut every 5 s while the settings page polls; whether macOS lets a running process create the tap without a restart is unchecked. Check: start without the permission, grant it, watch the warning.
- 2026-10-31 Key-combination shortcuts on Linux (evdev) were written on macOS and only unit-tested through the shared combination logic. Check on Arch: set Ctrl+Alt with "Change…", dictate.

## Gotchas

- 2026-10-31 macOS: until the microphone prompt for the starting app is answered, Claude Code's native audio check blocks its whole UI at "keep holding…" (no error, Ctrl+G ignored). The app now asks for the microphone at startup and restarts a session frozen this way.
- 2026-10-31 macOS: inside pystray's AppKit loop the main thread runs no Python, so `signal.signal` handlers (SIGTERM, Ctrl+C) never fire; use `PyObjCTools.MachSignals`.
- 2026-10-31 Agent shells on this Mac run under the Claude desktop app, so macOS permissions there are the Claude app's, not the terminal's. In zsh, `log` is a builtin; use `/usr/bin/log`.

- 2026-10-25 macOS AppKit crashes if a controller worker updates the tray menu directly. Dispatch tray refresh, visibility, and stop to the main event loop; Linux permits the existing worker updates.
- 2026-10-25 A hidden `claude` started from inside another Claude Code session inherits `CLAUDE_*` / `ANTHROPIC_*` variables and shows "Not logged in". Strip them.
- 2026-10-25 `claude --bare` disables OAuth, so `/voice` is unavailable; use `--safe-mode`. `--safe-mode` plus voice settings passed through `--settings` does record. `--no-session-persistence` only works with `--print`.
- 2026-10-25 "Not logged in" appears a few seconds after the prompt draws, so a startup check misses it; detect leftover typed spaces after a hold instead.
- 2026-10-25 With escape codes stripped, Claude Code's screen often has no spaces between words (they are cursor moves), and its renderer redraws only changed cells: the recording footer reads "listning…". Match markers with whitespace removed, and never rely on the word "listening".
- 2026-10-31 Voice warm-up on a running session: "keep holding…" at 0.06 s, "listening" at 0.15 s, first microphone levels at 0.24 s after the first repeated space. Words spoken before that are lost, so nothing may run between the key press and the first Space; the start sound alone takes 0.27 s with pw-play.
- 2026-10-25 Keychron mice: the sniper button sends KEY_F8 through the receiver's keyboard interface ("Keychron  Keychron Link  Keyboard", double spaces), and receivers appear as several devices. ydotoold's virtual device also reports side buttons; ignore it or typed text echoes back.
- 2026-10-25 pystray on GNOME needs PyGObject (builds from PyPI on Arch) and the AppIndicator extension. In the built-in browser pane, `document.hidden` is true, so the page must fetch once regardless of visibility.
- 2026-10-25 A GitHub wiki's git repo (`<repo>.wiki.git`) does not exist until someone saves the first page in the web UI; `gh repo edit --enable-wiki` alone is not enough. The wiki repo is the only copy of the wiki pages; edit them there.
- 2026-10-26 After rewriting commits to drop an author, GitHub's repo-page Contributors sidebar stays stale even when the contributors API is already clean. Switching the default branch away and back to the same name does not refresh it; renaming the default branch (`master` → `main`) did, at once. The global git identity is AIkry; this repo sets a local Gerard Wu / GerardWu100 identity.
- 2026-10-31 The Claude desktop app's chat box drops the first ~10 characters (~40 ms) of ydotool typing; history had the full transcripts, and the same typing into kgx or a Brave textarea lost nothing (muting, sounds, and the banner ruled out). Felt like "dictation misses my first words" though the recording was fine: compare history with the typed text first.

## Failed approaches

- 2026-10-25 Codex dictation. *disproved*. Codex CLI 0.164 has no dictation code. The ChatGPT/Codex desktop app's global dictation is app-scoped on Linux and cannot paste there. Its `chatgpt.com/backend-api/transcribe` endpoint answers scripts with a 403 bot-check page; do not try to get around it.
- 2026-10-25 pi (pi-mono) as a lighter Codex front end. *disproved*: no built-in dictation, and its ChatGPT login covers chat models only.
- 2026-10-25 Shrinking the hidden Claude Code with Bun memory options (`BUN_OPTIONS=--smol`, `BUN_JSC_forceRAMSize`). *disproved*: RSS stayed at about 250 MB and PSS (shared memory split fairly) at about 194 MB. About 141 MB is its own working memory; about 108 MB is program code the kernel can drop and reload under memory pressure. Only quitting the session saves more.

## Open blockers

## Untried ideas

- Start at login (Linux `~/.config/autostart` entry, macOS LaunchAgent) as an opt-in setting: removes the manual start; about an hour.
