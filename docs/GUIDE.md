# `wa` guide

`wa` lets an agent (Claude, Codex, Cursor, Gemini, Grok and others) read and operate the WhatsApp Desktop app installed on a Mac. It works only on macOS, with the native WhatsApp app. Read the [notice in the README](../README.md#caveats) before you use it. There are two ways to use it:

- the `wa` CLI, which answers in JSON;
- the `wa-mcp` MCP server (stdio), with the same functions.

Reading comes straight from the WhatsApp SQLite databases, always read-only. Sending, archiving and calling go through one of two paths:

- **companion** (section 5, experimental). The Mac becomes one more linked device of the account, like WhatsApp Web, through GoWA, an unofficial client that runs on the Mac itself. It needs no clicks and does not bring WhatsApp to the front.
- **Desktop app UI**. A `whatsapp://` link and the app's menu bar, with a check in the database afterwards.

The default (`--via auto`) uses the companion when it is connected and the app UI otherwise. Nothing goes through third-party servers except WhatsApp itself, the voice agent, which uses OpenAI, and transcription through OpenAI when you turn it on. Reading and sending through the app open no network connection of their own, and there is no telemetry.

## 1. Install and update

You need macOS, the WhatsApp Desktop app signed in, Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/jxnxts/whatsapp-mac.git
cd whatsapp-mac
uv tool install --force --reinstall .   # installs ~/.local/bin/wa and ~/.local/bin/wa-mcp
wa --version
```

To update, run `git pull` and the same `uv tool install`. Always pass `--reinstall`. Without it, `uv` reuses the old build when the version number has not changed.

## 2. macOS permissions

| Permission | What for | Who needs it |
|---|---|---|
| Full Disk Access | reading the WhatsApp databases | the app that runs the agent (Terminal, Cursor, VS Code, Claude Desktop…) |
| Accessibility | sending, archiving, pinning, calling (menu clicks) | the same app |
| Automation (System Events) | macOS asks on the first action | the same app |
| Microphone | the voice agent only | the same app |

All of them are in *System Settings > Privacy & Security*. After you turn on a permission, quit and reopen the app. Full Disk Access and Accessibility apply to the whole app, not only to `wa`, so grant them only to the app that needs them.

The databases live in `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/` (`ChatStorage.sqlite`, `ContactsV2.sqlite`, `LID.sqlite`, `CallHistory.sqlite`, `Labels.sqlite`). When the agent runs inside an app with its own helper daemon and the error persists, add the daemon's executable to Full Disk Access too (in the file picker, `Cmd+Shift+.` shows hidden folders).

Without Full Disk Access every command returns:

```json
{"error": "full_disk_access_required", "message": "No permission to read WhatsApp data. Grant Full Disk Access ..."}
```

Without Accessibility or Automation, sending stops before it presses any key and returns `accessibility_required` or `automation_permission_required`.

Sending, actions and calls through the app also need the Mac screen unlocked. Before it touches the app, `wa` reads the lock state with `ioreg` (no System Events involved). When the screen is locked it stops with `screen_locked`, and nothing is opened, pasted, clicked or sent. Reads and drafts work with the screen locked.

## 3. Safety guards

Nothing that changes state happens unless both conditions below hold:

1. **The request confirms it.** `--confirm` on the CLI or `confirm=true` in MCP. Without it the tool only returns the plan (a draft).
2. **The process environment allows it.**

| Variable | Allows |
|---|---|
| `WA_ALLOW_SEND=1` | sending a message |
| `WA_ALLOW_ACTIONS=1` | archive, unarchive, pin, mute, mark as unread, chat lists |
| `WA_ALLOW_CALLS=1` | calling, hanging up and the voice agent |

Other rules:

- Through the app UI, the tool clicks only when WhatsApp is the frontmost app. If another app takes focus, it refuses (`whatsapp_not_frontmost`) instead of clicking in the wrong place.
- Through the app UI, nothing runs while the Mac screen is locked (`screen_locked`).
- If the database does not confirm a toggle action, the tool tries a second time and stops (`attempts` shows how many there were).
- Block, delete and clear chat do not exist, on purpose.
- The WhatsApp Desktop databases are never changed. Archiving and similar actions sync with the phone, so they go through the app UI or the companion, never through a database write.
- The same guards apply to both paths (`--via companion` or `desktop`). The companion also limits sends per minute and per day (section 5).

## 4. Commands

Every command returns JSON (one line). `--pretty` indents it. Errors are JSON too, with exit code 1. The `error` field holds a stable code: `not_found`, `ambiguous`, `invalid_argument`, `full_disk_access_required`, `accessibility_required`, `automation_permission_required`, `screen_locked`, `send_refused`, `send_failed`, `action_refused`, `action_failed`, `database_busy`, `companion_unavailable`, `companion_failed`, `rate_limited`, `missing_dependency`, `media_not_downloaded`, `transcriber_unavailable`, `unsupported`, `path_refused`, `file_too_large` and others.

```sh
wa chats [--limit N] [--unread] [--archived] [--groups|--direct|--kind K] [--query NAME] [--include-hidden]
wa messages --chat <jid|phone|name> [--since D] [--until D] [--limit N] [--cursor C] [--include-system] [--media KIND]
wa search <text> [--chat C] [--since D] [--until D] [--limit N] [--cursor C]
wa contacts <name|phone>
wa send --to <phone|jid|name> --text <msg> [--confirm] [--via auto|companion|desktop] [--reply-to ID] [--method ui|shortcut|draft]
wa send --to <phone|jid|name> --file <path> [--caption T] [--as image|video|audio|voice|document|sticker] [--allow-any-path] [--confirm]
wa media --message ID [--no-ocr] [--no-transcribe] [--frames N] [--max-pages N] [--max-chars N] [--page-image] [--no-download]
wa media-cache [--clear]
wa chat-action --to <phone|jid|name> --action <action> [--via auto|companion|desktop] [--confirm]
wa list-create|list-add|list-remove|list-delete ... [--confirm]
wa calls [--limit N] [--missed]
wa lists
wa call --to <phone|jid|name> [--kind voice|video] [--confirm]
wa hangup [--confirm]
wa voice-call --to <phone|jid|name> --goal "<goal>" [--max-minutes 5] [--monitor] [--confirm]
wa voice-test --goal "<goal>" [--max-minutes 3]
wa companion link|status|logout|inbox|service ...
wa mcp
```

### Read

```sh
wa chats --limit 20                     # most recent chats
wa chats --unread                       # only chats with unread messages
wa chats --groups | --direct            # groups only / one-to-one only
wa chats --archived                     # archived
wa chats --query "Maria"                # by name
wa messages --chat "+14155550123" --since 2026-11-01 --limit 50
wa search "meeting" --since 2026-11-20  # case and accent insensitive
wa search "invoice" --chat "Alex"
wa contacts "Maria"                     # name -> phone/jid
wa calls --limit 20 [--missed]          # call history
wa lists                                # chat lists (from the companion when it is connected)
```

- **Dates** (`--since`/`--until`) accept ISO (`2026-11-01`, `2026-11-01T10:00`, local time) or relative values (`30m`, `12h`, `7d`, `2w`). Output dates are ISO 8601 with the local offset.
- **Limits** default to 20, with a maximum of 100 per page. For more results, pass the `next_cursor` from the answer in `--cursor`. Messages come newest first.
- **`--chat` and `--to`** accept a phone number with country code (with or without the Brazilian ninth digit), a jid (`…@s.whatsapp.net`, `…@lid`, `…@g.us`) or a name. An ambiguous name returns `ambiguous` with the candidates.
- **Chats** have `jid`, `name`, `kind` (`direct`, `group`, `broadcast`, `community`, `channel`; `status` is left out by default), `unread`, `archived`, `last_message_at`, `phone` (one-to-one chats) and `pinned`.
- **Messages** have `id`, `date`, `from_me`, `from` (`name` and `jid`; in groups the author comes from the address book, the group member or the profile name), `type` (`text`, `image`, `audio`, `video`, `document`, `link`, `sticker`… or `type_N`), `text` (`null` for most media; for a document, its title) and, for a message with a file, `media` (see [Media](#media)). Search results also carry `chat`.
- Removed chats never show up. Hidden chats (`ZHIDDEN=1`) show up only with `--include-hidden`, which exists only on the CLI. MCP never shows hidden chats.

### Send

```sh
wa send --to "+14155550123" --text "Hi!"                                # draft: does not send
WA_ALLOW_SEND=1 wa send --to "+14155550123" --text "Hi!" --confirm      # sends
wa send --to "+1…" --text "…" --method draft                            # only opens the chat with the text filled in
```

```sh
WA_ALLOW_SEND=1 wa send --to "+1…" --text "Hi!" --confirm --via companion        # forces the companion
WA_ALLOW_SEND=1 wa send --to "1203…@g.us" --text "Good morning" --confirm         # group by jid (companion or app)
WA_ALLOW_SEND=1 wa send --to "+1…" --text "Deal" --reply-to 3EB0… --confirm       # quoted reply (companion)
wa send --to "+1…" --text "…" --via desktop                                      # forces the app UI
```

The answer always names the path it used in `via` (`companion` or `desktop`). Text can be up to 4096 characters on the CLI and 2000 in MCP.

How a real send through the app UI (`--method ui`, the default) works:

1. It checks that the Mac screen is unlocked (`ioreg`). If it is locked, it stops with `screen_locked`.
2. For a person, it opens `whatsapp://send?phone=<digits>&text=<text>`, which opens the chat with the text in the compose box (without sending). For a group, it opens `whatsapp://openchat/<jid>`, which takes no text, so it first saves your clipboard text and copies the message to the clipboard. It waits about 3 s.
3. It brings WhatsApp to the front (`activate`) and waits up to about 5 s for it to become frontmost.
4. For a group, it presses Cmd+A and Cmd+V in the compose box, again only with WhatsApp in front.
5. It presses Return **only if the frontmost process is "WhatsApp"**. The check and the keystroke run in the same script, so there is no race with another app.
6. For a group, it puts your previous clipboard text back (`clipboard_restored`), even when the send stops early.
7. It queries the database (read-only) for up to about 10 s, looking in that number's chats (or in the group chat) for a message of yours with exactly the same text and a date after the start.

WhatsApp comes to the front for a moment during the send. Do not type anything during that time.

Results:

- `"sent": true` with `confirmation` (`message_id`, `status`, `date`, `chat_jid`, `chat_matched`) means the message showed up in the database. `chat_matched: false` means it showed up in a chat whose JID `wa` did not know (a new chat). For a group, that case also adds a `warning`.
- `"sent": false` comes with a `reason`:
  - `whatsapp_not_frontmost`: WhatsApp did not come to the front, Return was **not** pressed and nothing was sent;
  - `not_confirmed_in_db`: Return **was** pressed (`pressed_return: true`), but the message did not show up in the database in time. Check WhatsApp before you retry, so you don't send it twice.

On the CLI, `--confirm` without a confirmed send exits with code 1.

Recipients are **people** (a phone number, `@s.whatsapp.net`, or `@lid` with a known phone number) and **groups** by jid (`…@g.us`, which must be a chat in the database; find it with `wa chats --groups`). A group name alone does not resolve. `wa` returns `not_found` with the matching group jids as candidates. Communities, broadcast lists (`@broadcast`) and channels (`@newsletter`) are refused with `invalid_argument`. `--method shortcut` only sends to people. The group send through the app has only run against simulators.

#### Shortcuts method (optional, `--method shortcut`)

`wa send ... --confirm --method shortcut` runs `shortcuts run "WA Send" --input-path <json>` with `{"to": "+14155550123", "text": "Hi!"}` (change the name with `WA_SHORTCUT_NAME`). The shortcut takes Text, runs **Get Dictionary from Input**, reads `to` and `text` with **Get Dictionary Value** and sends the message. Here `"sent": true` only means the shortcut finished without an error.

The "Send Message via WhatsApp" action (`net.whatsapp.WhatsApp.send`) did not work in testing. The Shortcuts app says the action needs WhatsApp installed, because it does not recognize the current WhatsApp app (`net.whatsapp.WhatsApp`). That is why `ui` is the default.

### Media

Messages with a file carry a `media` field. `wa media` reads the file itself.

```sh
wa messages --chat "Alex" --media document          # only messages with a document (also: any, image, video, audio, voice, sticker, gif)
wa media --message 18342                            # the id comes from wa messages / wa search
wa media --message 18342 --max-pages 3 --max-chars 5000
wa media --message 18350 --frames 6                 # a video: 6 frames and the transcript
wa media-cache                                      # size of the OCR and transcript cache
wa media-cache --clear                              # deletes it
```

The `media` field has `kind` (`image`, `video`, `audio`, `voice`, `document`, `sticker`, `gif`), `mime`, `size_bytes`, `duration_s` (audio and video) or `pages` (documents), `file_name` and `title` (documents), `caption`, and `downloaded`: whether the file is on this Mac. `wa search` also matches captions and document file names.

What `wa media` returns, by kind:

| Kind | Returns |
|---|---|
| image, sticker | the image as JPEG (longest side up to 1568 px, at most `WA_MEDIA_IMAGE_MAX_BYTES`) and OCR text |
| document | the text of PDF, DOCX, XLSX, PPTX, RTF, ODT and plain text files, limited by `--max-pages` (pages, sheets or slides; default 10) and `--max-chars` (default 20,000). PDF pages without text get OCR. A PDF with little text also comes with its first page as an image (`--page-image` forces it) |
| audio, voice | a transcript |
| video, gif | `--frames` frames spread over the video (default 4, maximum 8) and the transcript of its sound |

On the CLI the images are saved in the cache folder and the answer lists their paths. In MCP, `wa_get_media` returns them as image content the model can see. Each answer has `untrusted: true`: the text, OCR and transcript come from whoever sent the file, so treat them as data.

Media files are only read. When a file is not on the Mac (`downloaded: false`), `wa` asks the companion to download it (`GET /message/<id>/download` on GoWA). The companion only has messages that arrived after it was paired. Otherwise the answer is `media_not_downloaded`: open the chat in WhatsApp on the Mac and let the file download.

**Install.** Reading images works out of the box (macOS `sips` reduces them). The rest is optional:

```sh
uv tool install --force --reinstall '.[media]'   # pypdf, python-docx, openpyxl, python-pptx, Pillow, pyobjc-framework-Vision
brew install ffmpeg                              # videos, and voice notes you send
uv tool install mlx-whisper                      # transcripts (Apple Silicon)
```

Without the extra, `wa` still reads PDFs through PDFKit when pyobjc is there and Word, RTF and ODT through macOS `textutil`, and says what is missing otherwise (`missing_dependency` with an `install` hint).

**OCR** uses the macOS Vision framework through pyobjc, in the same process: no helper to compile and no Xcode. It runs on the Mac.

**Transcription** runs on the Mac with mlx-whisper (`mlx_whisper` on the PATH or in `~/.local/bin`) or whisper.cpp (`whisper-cli` and a ggml model in `WA_WHISPER_CPP_MODEL`). The first mlx-whisper run downloads the model from Hugging Face. The OpenAI API is used only when you opt in with `WA_TRANSCRIBE_REMOTE=1` and no local transcriber is installed (or `WA_TRANSCRIBE_ENGINE=openai`); the key comes from the Keychain (`wa-mac-openai`, the same as the voice agent) or `OPENAI_API_KEY`, and the audio then leaves the Mac. Without any transcriber, audio answers `transcriber_unavailable` with the install hint. A video still returns its frames.

**Cache.** OCR, document text and transcripts are cached in `~/.local/share/wa-mac/cache`, keyed by the SHA-256 of the file and the settings. The folder is 700 and the files 600. `wa media-cache --clear` deletes it.

#### Send a file

```sh
cp ~/Desktop/report.pdf ~/.local/share/wa-mac/outbox/
wa send --to "+1…" --file report.pdf --caption "This week's report"            # draft: does not send
WA_ALLOW_SEND=1 wa send --to "+1…" --file report.pdf --caption "…" --confirm    # sends
WA_ALLOW_SEND=1 wa send --to "1203…@g.us" --file memo.m4a --as voice --confirm   # converted to a voice note
wa send --to "+1…" --file ~/Pictures/cat.jpg --allow-any-path                   # a file outside the outbox (CLI only)
```

- Files go out **only through the companion** (GoWA `/send/image`, `/send/video`, `/send/audio`, `/send/file`, `/send/sticker`). `--via desktop` answers `unsupported`.
- `--as` picks `image`, `video`, `audio`, `voice`, `document` or `sticker`. By default it follows the file type, and anything that is not an image, video or audio goes as a document. `voice` converts the audio to ogg/opus with ffmpeg and sends it as a voice note. HEIC and TIFF images are converted to JPEG with `sips`. `--caption` works for images, videos and documents.
- The size limits come from GoWA (`/app/info`: 20 MB for images, 100 MB for videos, 50 MB for the rest by default). A larger file answers `file_too_large`.
- The guards are the same as for text: `--confirm` and `WA_ALLOW_SEND=1`, and each file counts against the companion send limit. The draft shows the file name, kind, size, MIME type, recipient and size limit.
- **Which files can go out.** An agent under prompt injection could try to send your keys or documents to someone. So only files inside the outbox folder are accepted: `WA_MEDIA_OUTBOX`, by default `~/.local/share/wa-mac/outbox`, created with mode 700. A bare name means a file in that folder. Paths with `..` and symbolic links that lead out of the folder are refused with `path_refused`. On the CLI, `--allow-any-path` lifts this. In MCP, only `WA_ALLOW_ANY_FILE=1` in the server environment lifts it, and it is off by default.

| Variable | Default |
|---|---|
| `WA_MEDIA_OUTBOX` | `~/.local/share/wa-mac/outbox` (the only folder MCP can send from) |
| `WA_ALLOW_ANY_FILE` | off; `1` lets MCP send files from anywhere |
| `WA_MEDIA_IMAGE_MAX_BYTES` | 1,000,000 bytes per image handed to the agent |
| `WA_MEDIA_CACHE_DIR` | `~/.local/share/wa-mac/cache` |
| `WA_OCR_LANGUAGES` | empty: Vision detects the language (otherwise, for example, `pt-BR,en-US`) |
| `WA_TRANSCRIBE_ENGINE` | `auto` (mlx-whisper, then whisper.cpp, then OpenAI if allowed); or `mlx`, `whisper.cpp`, `openai` |
| `WA_TRANSCRIBE_MODEL` | mlx: `mlx-community/whisper-large-v3-turbo`; whisper.cpp: a ggml file; OpenAI: `whisper-1` |
| `WA_TRANSCRIBE_LANGUAGE` | empty: detected (otherwise an ISO code such as `pt` or `en`) |
| `WA_WHISPER_CPP_MODEL` | path of the ggml model for whisper.cpp |
| `WA_TRANSCRIBE_REMOTE` | off; `1` allows the OpenAI API |

### Chat actions

```sh
WA_ALLOW_ACTIONS=1 wa chat-action --to "+1…" --action archive --confirm
# actions: archive | unarchive | pin | unpin | mute-8h | mute-1w | mute-always | unmute | mark-unread
```

`--via` works as it does for sending. Through the companion, the action goes out as an account state update (it syncs with the phone), accepts a group by jid and answers `via: companion` with the GoWA response.

Through the app UI, everything goes through the WhatsApp menu bar (via System Events), which acts on the open chat. Without `--confirm` you get the **plan** and the chat's current state in the database. With `--confirm` and the switch on:

1. It opens `whatsapp://send?phone=<digits>` (the chat, without text) and waits about 3 s.
2. It brings WhatsApp to the front (up to about 5 s) and waits about 0.8 s for the menu to reflect the open chat.
3. It reads the current title of the item in the Chat menu. Titles toggle (Archive and Unarchive, Pin and Unpin, Mute and Unmute). If the chat is already in the requested state, it does not click and returns `"already": true`.
4. It clicks. The "WhatsApp is frontmost" check and the click run in the same script.
5. It checks the database for up to 10 s: `ZARCHIVED` (archive), a far-future `ZLASTMESSAGEDATE` (pin), `ZWACHATPROPERTIES.ZMUTEDATE` (mute) and `ZUNREADCOUNT` (unread). The result is in `verified`. If it could not check, you get `verified: false` and a `reason` (`not_seen_in_db`, `chat_not_in_db`…).
6. If the database did not confirm and the menu **still shows the previous state** (the click did not take, as seen when unarchiving), it waits about 1 s, brings WhatsApp to the front and clicks a second and last time, then checks again. `attempts` says how many clicks there were (1 or 2). If the menu already shows the new state, it does not click again (`retry_skipped`).

The answer has `clicked` (the menu item it clicked), `verified`, `attempts` and a `reason` when something fails.

Menu titles depend on the WhatsApp language. `wa` accepts the Brazilian Portuguese titles (tested on a real account) and the English ones (taken from the app's localization files, `en.lproj` in WhatsApp.app and in its SharedModules framework):

| Use | Portuguese | English |
|---|---|---|
| menus | Conversa, Ligar | Chat, Call |
| archive / unarchive | Arquivar, Desarquivar | Archive, Unarchive |
| pin / unpin | Fixar, Desafixar | Pin, Unpin |
| mute | Silenciar > 8 horas, 1 semana, Sempre | Mute > 8 hours, 1 week, Always |
| unmute | Reativar som, Reativar notificações… | Unmute, Unmute sound, Unmute notifications… |
| unread | Marcar como não lida, Marcar como lida | Mark as unread, Mark as read |
| calls | Ligação de voz, Ligação de vídeo, Encerrar ligação | Voice call, Video call, End Call |
| call devices | Microfone, Alto-falante | Microphone, Speaker |

Limits through the app UI:

- Only one-to-one chats with a known phone number. Groups are refused (they need the companion).
- The title of the item that unmutes a chat was not observed on a real account. `wa` accepts several variants. If the menu has another title, `unmute` fails with `action_failed` and lists the menu items.
- On some Macs `ZWACHATPROPERTIES` is empty, so a mute can come back with `verified: false` even when it worked.

### Chat lists (companion only)

```sh
WA_ALLOW_ACTIONS=1 wa list-create --name "Work" --confirm
WA_ALLOW_ACTIONS=1 wa list-add    --list "Work" --chat "+1…" --confirm
WA_ALLOW_ACTIONS=1 wa list-remove --list "Work" --chat "+1…" --confirm
WA_ALLOW_ACTIONS=1 wa list-delete --list "Work" --confirm
```

WhatsApp Desktop for Mac does not receive chat lists from the phone. Its `Labels.sqlite` stays empty and the sidebar shows no lists. The companion keeps its own copy instead. GoWA records every list and chat that reaches it through app-state events, and right after it connects it runs one full sync of the lists that already exist. `GET /wa-mac/lists` on GoWA returns them.

- With the companion connected, `wa lists`, `wa_list_lists` and `--list` resolve against the companion's lists (`source: "companion"`). Deleted lists are left out.
- Without the companion, they fall back to the Desktop database (`source: "desktop"`), which is usually empty.
- `--list` accepts the name or the id shown by `wa lists`.
- After each operation, `wa` polls the companion's list state for up to 5 s and reports `verified: true` once the change shows up there (`verified_by: "companion"`). WhatsApp Desktop is never used to confirm a list operation.

Creating, adding a chat, removing it and deleting a list were tested on a real account. Each returned `verified: true`, and the list and the chat showed up on the phone.

### Calls

```sh
WA_ALLOW_CALLS=1 wa call --to "+1…" --kind voice --confirm    # or --kind video
WA_ALLOW_CALLS=1 wa hangup --confirm
```

After it clicks "Voice call" or "Video call", `wa call` checks that the "End Call" item in the Call menu became enabled (`verified`). `wa hangup` clicks "End Call" and checks that the item became disabled.

### Voice agent

```sh
wa voice-test --goal "Practice: confirm Tuesday's meeting at 7:30 pm"    # talk through the Mac's microphone and speaker, no call
WA_ALLOW_CALLS=1 wa voice-call --to "+1…" --goal "Confirm Tuesday's meeting at 7:30 pm" --max-minutes 5 --monitor --confirm
```

Prerequisites:

- The OpenAI key in the Keychain: `security add-generic-password -a "$USER" -s wa-mac-openai -w` (it prompts for the key without showing it), or the `OPENAI_API_KEY` variable. `wa` reads it with `security find-generic-password -s wa-mac-openai -w` and never stores or logs it.
- For `voice-call` only, two virtual audio cables: `brew install --cask blackhole-2ch blackhole-16ch`. Then restart the Mac (or `sudo killall coreaudiod`). Without them `wa` stops before calling, with `missing_dependency` and that command.
- Model: `WA_REALTIME_MODEL` wins. Without it the default is `gpt-realtime`, checked against `/v1/models` (if the account lacks it, `wa` uses the newest realtime model listed).
- The name of the person the agent represents: `WA_OWNER_NAME` (optional).
- The language the agent speaks: `WA_VOICE_LANGUAGE`, for example `pt-BR` (optional).
- Permissions: Full Disk Access, Accessibility and Automation (as for actions), plus **Microphone** for the app that runs `wa`.
- Guards: `--confirm` (MCP: `confirm=true`) **and** `WA_ALLOW_CALLS=1`.

| Cable | Role |
|------|-------|
| BlackHole 2ch | WhatsApp to the agent (`wa` sets it as the call's **Speaker**) |
| BlackHole 16ch | the agent to WhatsApp (`wa` sets it as the call's **Microphone**) |

`wa` switches the devices in the Call menu of the ongoing call, not in the Mac settings. The rest of the system keeps its usual microphone and speaker.

How the agent behaves:

- In its first turn it introduces itself as an **assistant**. With `WA_OWNER_NAME="Maria"` it says "Hi, this is Maria's assistant". Without the variable it says it is "a virtual assistant". It never says it is the owner, never pretends to be them and never imitates their voice.
- It speaks the language in `WA_VOICE_LANGUAGE`. Without it, it starts in English and switches to the other person's language.
- It follows only the goal. For anything else, it says the person will get back to them. It does not agree to commitments outside the goal and does not ask for passwords or bank details.
- It has the `end_call` tool (with a summary of the result) and hangs up on its own.
- It respects `--max-minutes` (default 5, maximum 30). It gets a reminder to say goodbye 20 s before the end, and the call is cut 15 s after the limit no matter what.

Flow:

1. Checks the cables and the key. Without them, nothing happens.
2. Calls (like `wa call`) and checks "End Call".
3. In the Call menu, sets Microphone to BlackHole 16ch and Speaker to BlackHole 2ch.
4. Bridges the audio: BlackHole 2ch to PCM16 24 kHz to the Realtime API (WebSocket), and the reply audio back to BlackHole 16ch (with resampling). The agent speaks when the person answers and says something (server-side voice detection). If the person interrupts, the agent's audio stops.
5. At the end (the agent called `end_call`, time ran out, the connection dropped or an error happened), it **always** clicks Call > End Call, even after an error.
6. Saves `~/.local/share/wa-mac/calls/<date>.json` (outside the repository; change it with `WA_CALLS_DIR`) with the recipient, goal, model, transcript (person and assistant), result and how the call ended, and returns a summary and the file path.

`--monitor` mirrors both sides to the default speaker so you can hear the call. Use headphones with `voice-test`, so the agent does not hear itself.

## 5. Companion mode (GoWA, experimental)

The Mac becomes one more device under *Linked devices*, named `wa-mac`. GoWA, a WhatsApp Web client written in Go, makes the link. It is built from the public fork [jxnxts/go-whatsapp-web-multidevice](https://github.com/jxnxts/go-whatsapp-web-multidevice) (branch `wa-companion`).

**Status.** Pairing, status, chat lists (create, add, remove, delete) and these chat actions were tested on a real account: archive, unarchive, pin, mute, unmute and mark unread. Unpin through the companion was accepted by GoWA, but WhatsApp Desktop kept the chat pinned, so it is not confirmed yet. Sending and the inbox only went through simulated tests. Treat them as experimental.

### Risks

- **It is an unofficial client.** WhatsApp does not allow this kind of program and can suspend or ban the number without warning. Use it sparingly, with no bulk sending and no aggressive automation.
- **The session file is the account.** Anyone who copies `~/.local/share/wa-mac/gowa/storages/` can read and send messages as you until you unlink the device. That is why the folder is closed (`chmod 700`) and must never go to a cloud backup, a repository or another computer.
- **To cut access at any time**, unlink on the phone (below). That ends the session right away, even if someone has a copy.

### Install and start

Needs Go (`brew install go`) and git.

```sh
scripts/build-gowa.sh               # clones the fork and builds ~/.local/share/wa-mac/gowa/bin/gowa
wa companion service start          # creates the Keychain password and the LaunchAgent, then starts it
wa companion service status         # pid, address, authentication and folder permissions
wa companion service stop           # stops it (it does not come back until the next start)
```

`build-gowa.sh` accepts `GOWA_SRC` (the `src/` folder of a local clone of the fork; it uses that code and downloads nothing), `GOWA_REPO` and `GOWA_REF` (default: the public fork, branch `wa-companion`) and `WA_GOWA_DIR` (data folder).

The service (`local.wa-mac.gowa`) listens **only on 127.0.0.1:3787**, requires Basic Auth with a user and password (the password lives in the Keychain, service `wa-mac-gowa`, and never shows up on screen or in logs) and restarts if it crashes. Its settings are fixed. It does not send read receipts, does not auto-reply, does not reject calls and does not download media. It has no webhooks, no Chatwoot and no web dashboard, and it does not go "online" (presence is sent as unavailable, so the phone keeps notifying). Logs are in `~/.local/share/wa-mac/gowa/logs/`. To change the port, set `WA_GOWA_PORT`. launchd starts agents with a bare PATH, so the plist sets one with the folder of the ffmpeg found at install time, Homebrew's folders and the system ones: GoWA needs ffmpeg to send videos and voice notes. After installing ffmpeg, run `wa companion service start` again; when the plist changes it reloads the agent instead of only restarting it.

### Pair (phone in hand)

```sh
wa companion link                       # uses your own number (the one in WhatsApp Desktop)
wa companion link --phone +1…           # another number
```

An 8-character code shows up (`ABCD-EFGH`). On the phone, open **WhatsApp > Settings > Linked devices > Link a device > Link with phone number instead** and type the code. `wa` checks every 2 s and stops when the device links, when pairing fails or when the code expires (3 min; `--timeout` changes it).

### Status and unlink

```sh
wa companion status              # connected / not_linked / service_down, with the number masked
wa companion logout --confirm    # unlinks this Mac and deletes the session
```

On the phone, open *Linked devices*, tap `wa-mac` and choose *Log out*. After that `wa companion status` shows `not_linked`, and `--via auto` goes back to the app UI.

### Real-time inbox

```sh
wa companion inbox --since 2h --limit 50 [--unread]
```

It shows the **received** messages the companion captured since pairing (plus the recent history the phone sends during pairing), without marking anything as read. `--unread` is an approximation. It counts a message as read up to the last point where you read the chat on another device.

### Companion send limits

| Variable | Default |
|---|---|
| `WA_COMPANION_MAX_PER_MINUTE` | 10 sends per minute |
| `WA_COMPANION_MAX_PER_DAY` | 100 sends per day |
| `WA_COMPANION_DESKTOP_CHECK_S` | 0 (no check in the Desktop database; up to 10 s when set) |

One recipient per call. A send counts as confirmed when GoWA returns the message id (`confirmation.message_id`). The limit log keeps only times, in `~/.local/share/wa-mac/companion-sends.json`.

Calls only go through the Desktop app UI.

## 6. MCP tools (`wa-mcp`)

| Tool | Does | Guard |
|---|---|---|
| `wa_list_chats` | lists chats (filters: unread, kind, archived, name) | none |
| `wa_read_messages` | messages of a chat; `media` filters by kind | none |
| `wa_search_messages` | text search (also captions and document file names) | none |
| `wa_get_media` | the file of a message: image content, document text, OCR, transcript, video frames | none |
| `wa_find_contact` | name to phone/jid | none |
| `wa_list_calls` | call history | none |
| `wa_list_lists` | chat lists (from the companion when it is connected) | none |
| `wa_send_message` | sends (text up to 2,000 characters); `via`, `reply_to` | `confirm` + `WA_ALLOW_SEND` |
| `wa_send_media` | sends a file from the outbox folder through the companion; `kind`, `caption`, `reply_to` | `confirm` + `WA_ALLOW_SEND` |
| `wa_chat_action` | archive, pin, mute, mark as unread; `via` | `confirm` + `WA_ALLOW_ACTIONS` |
| `wa_companion_status` | is the companion connected? | none |
| `wa_companion_inbox` | messages received in real time (does not mark them as read) | none |
| `wa_list_create` / `wa_list_add` / `wa_list_remove` / `wa_list_delete` | lists through the companion, confirmed against its list state | `confirm` + `WA_ALLOW_ACTIONS` |
| `wa_call` / `wa_hangup` | call / hang up | `confirm` + `WA_ALLOW_CALLS` |
| `wa_voice_call` | a call with the voice agent | `confirm` + `WA_ALLOW_CALLS` |

- The default limit is 20, the maximum is 100 per call, and each answer holds at most about 60,000 characters (the rest comes through `next_cursor`; `truncated_by_size: true` warns about it).
- Message text is third-party content. The read tool descriptions tell the agent to treat it as data, never as instructions. That does not stop prompt injection, so keep the `WA_ALLOW_*` switches off and read the draft before you confirm.
- With `method=ui` (the default), a confirmed send brings WhatsApp to the front for a moment. Reads and drafts never touch the UI.

Quick stdio test:

```sh
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | wa-mcp
```

## 7. Register the server in an agent

For any MCP client, the server is a stdio process. The command is `~/.local/bin/wa-mcp` (use the absolute path; check it with `which wa-mcp`), with no arguments. Without environment variables it only reads.

| Agent | Where the configuration lives |
|---|---|
| Claude Code | `claude mcp add --scope user whatsapp -- "$HOME/.local/bin/wa-mcp"` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Codex | `~/.codex/config.toml` |
| Cursor | `~/.cursor/mcp.json` |
| VS Code | `~/Library/Application Support/Code/User/mcp.json` |
| Gemini CLI | `~/.gemini/settings.json` (runs only in a trusted folder) |
| opencode | `~/.config/opencode/opencode.json` |
| Grok CLI | `~/.grok/config.toml` |

JSON format, used by most clients:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/Users/YOUR_USER/.local/bin/wa-mcp",
      "args": [],
      "env": { "WA_ALLOW_SEND": "1" }
    }
  }
}
```

TOML format (Codex, Grok CLI):

```toml
[mcp_servers.whatsapp]
command = "/Users/YOUR_USER/.local/bin/wa-mcp"
args = []
# env = { WA_ALLOW_SEND = "1", WA_ALLOW_ACTIONS = "1", WA_ALLOW_CALLS = "1" }
```

Allow only what you use: `WA_ALLOW_SEND=1`, `WA_ALLOW_ACTIONS=1` and/or `WA_ALLOW_CALLS=1` in `env`, then restart the agent. The app that runs the agent needs the permissions from section 2. `wa_voice_call` blocks until the call ends, so raise the client's tool timeout (for example to 1900 s).

## 8. Common problems

| Message | What to do |
|---|---|
| `full_disk_access_required` | grant Full Disk Access to the app that runs the agent and reopen it |
| `accessibility_required` / `automation_permission_required` | grant Accessibility and accept Automation for that app |
| `whatsapp_not_frontmost` | another app was in front; try again without touching the Mac |
| `screen_locked` | the Mac screen is locked; unlock it and try again (nothing was sent or clicked) |
| `send_failed` / `action_failed` with `reason: osascript_timeout` | System Events or WhatsApp did not answer; check the lock screen, the permissions and any open dialog in WhatsApp |
| `verified: false`, `not_seen_in_db` | the action may not have taken; check WhatsApp before you retry |
| `The item 'Mute' is disabled` | the chat does not allow it (for example, the chat with yourself) |
| `action_failed` with `menu_items` | the WhatsApp UI is in a language `wa` does not know yet, or the title changed |
| `missing_dependency` (voice) | the Keychain key or BlackHole is missing |
| Tools do not show up in Gemini | open Gemini in a folder marked as trusted |
| New code did not take effect | `uv tool install --force --reinstall .` |
| `companion_unavailable` | the service is not running or the Mac is not paired: `wa companion status` |
| `companion_failed` with 401 | the Keychain password and the service's do not match: `wa companion service start` rewrites the script and restarts it |
| `rate_limited` | you passed the companion limit; wait or change the variables |
| `media_not_downloaded` | the file is not on the Mac and the companion could not fetch it: open the chat in WhatsApp on the Mac and let it download |
| `transcriber_unavailable` | install mlx-whisper or whisper.cpp, or opt in to OpenAI (see [Media](#media)) |
| `missing_dependency` (media) | install the `media` extra or ffmpeg; the `install` field has the command |
| `path_refused` | copy the file into the outbox folder (`WA_MEDIA_OUTBOX`) |
| `unsupported` | files only go through the companion; drop `--via desktop` |
| `file_too_large` | GoWA's limit for that kind of file (`/app/info`) |

## 9. Known limitations

- Chats with **Advanced Chat Privacy** are **not** filtered when reading yet (the flag in the database has not been identified).
- Calling, hanging up, muting and the voice agent have not been tested on a real account yet.
- The English menu titles come from the localization files (`en.lproj` in WhatsApp.app and in its SharedModules framework) and have not been tested on a real account. Other UI languages are not supported for desktop actions yet.
- WhatsApp Desktop for Mac does not show chat lists. They only exist through the companion. The "unavailable" presence has **not** been tested on a real account yet. Through the app UI, creating or moving lists does not exist (there is no menu for it).
- Unpin through the companion is not confirmed. GoWA accepted it, but WhatsApp Desktop kept the chat pinned in the real test.
- Groups accept sending through the app or the companion (by jid). Chat actions on a group only work through the companion.
- Through the app UI, WhatsApp comes to the front for a moment for every send or action.
- The companion inbox `--unread` is approximate (see section 5).
- A voice note is told apart from an audio file by its type (`audio/ogg`, opus) or by having been recorded in the app. A forwarded MP3 shows up as `audio`.
- The companion can only download the files of messages that arrived after pairing.
- The database notes are for WhatsApp 26.46 (see [DEVELOPMENT.md](DEVELOPMENT.md#database-notes-whatsapp-for-mac-2646)). The databases are opened read-only and never copied. Status updates (stories) are left out.

### What has run on a real account

| Feature | Status |
|---|---|
| Reading chats, messages, search and contacts | tested on a real account |
| Sending to a person through the app | tested on a real account (Portuguese UI) |
| Sending to a group through the app | simulated tests only |
| Locked-screen check (`screen_locked`) | simulated tests only |
| Archive, unarchive and pin through the app | tested on a real account (Portuguese UI) |
| Calling, hanging up, muting and the voice agent | simulated tests only |
| English WhatsApp UI | simulated tests only, titles taken from the localization files |
| Companion pairing and status | tested on a real account |
| Chat actions through the companion (archive, unarchive, pin, mute, unmute, mark unread) | tested on a real account |
| Unpin through the companion | not confirmed (Desktop kept the chat pinned) |
| Chat lists through the companion (create, add a chat, remove it, delete) | tested on a real account |
| Companion text sending and inbox | experimental, simulated tests only |
| Media: image OCR, PDF text and voice note transcripts (`wa media`) | tested on a real account |
| Sending an image with a caption, a PDF and a voice note through the companion | tested on a real account |
| Media: videos (sending, frames and transcript), DOCX/XLSX/PPTX, files fetched through the companion, images through MCP | simulated tests only |
