# Development

## Set up and test

```sh
uv sync
uv run pytest
uv run python scripts/smoke_real.py
```

`pytest` never touches real data. It runs against synthetic SQLite databases and simulators of WhatsApp and GoWA (`tests/fake_whatsapp.py`, `tests/fake_gowa.py`), all with made-up content.

`scripts/smoke_real.py` is the one script that opens your real database. It prints only counts and timings.

To try a change without installing, run `uvx --from . wa chats --limit 3`. To install your working copy, run `uv tool install --force --reinstall .`. Without `--reinstall`, `uv` reuses the old build when the version number has not changed.

## Source layout

```
src/wa/db.py          read-only connection, retries, permission errors
src/wa/core.py        chats, messages, search, contacts, JID/phone/name resolution
src/wa/send.py        drafts and sending through the app (double confirmation)
src/wa/ui.py          WhatsApp menu bar automation (System Events)
src/wa/actions.py     chat actions, calling and hanging up, menu titles per language
src/wa/routing.py     picks the path (companion or app UI)
src/wa/companion.py   GoWA client, pairing, send limits
src/wa/service.py     GoWA LaunchAgent
src/wa/voice.py       voice agent (Realtime API, BlackHole, transcript)
src/wa/cli.py         the `wa` CLI
src/wa/mcp_server.py  the MCP server (`wa-mcp`)
tests/                synthetic databases and simulators
```

## How reads stay read-only

The connection opens with `mode=ro` and sets `PRAGMA query_only=ON`. It reads the WAL too, so the data is live. `wa` never copies the database files.

## How sends through the app are guarded

Before it touches the app, `wa` reads the screen lock state from `ioreg -n Root -d1` (`IOConsoleLocked`, or `CGSSessionScreenIsLocked` on the console session), without System Events. A locked screen stops the command with `screen_locked`. If `ioreg` gives no answer, the osascript timeout covers it. The keystroke or menu click happens only when WhatsApp is the frontmost app. The focus check and the action run in the same script. If another app takes focus, the command refuses with `whatsapp_not_frontmost`. After a send, `wa` looks for the message in the database and reports it in `confirmation`. After an action, it checks the chat state in the database and reports `verified`.

## Menu titles and languages

Chat actions and calls find menu items by title, per language, in `src/wa/actions.py`. The Brazilian Portuguese titles were tested on a real account. The English ones were taken from `en.lproj` in WhatsApp.app and in its SharedModules framework. An unknown language fails with `action_failed` and returns the menu titles it found (`menu_items`), which is the list you need.

## Database notes (WhatsApp for Mac 26.46)

The schema is not documented and can change in any release.

- Dates are seconds since 2001-01-01. Add 978307200 to get Unix time.
- `ZSESSIONTYPE` is 0 for one-to-one chats, 1 for groups, 2 for broadcast lists, 3 for status, 4 for communities and 5 for channels.
- Pinned chats store `ZLASTMESSAGEDATE` in the far future. The real date comes from `ZLASTMESSAGE`.
- In groups, the author comes from `ZWAMESSAGE.ZGROUPMEMBER → ZWAGROUPMEMBER.ZMEMBERJID`.
- The flag for Advanced Chat Privacy has not been identified, so those chats are not filtered yet.

## Companion build

`scripts/build-gowa.sh` clones the fork [jxnxts/go-whatsapp-web-multidevice](https://github.com/jxnxts/go-whatsapp-web-multidevice) (branch `wa-companion`) and builds `~/.local/share/wa-mac/gowa/bin/gowa`. It needs Go and git. It accepts `GOWA_SRC` (the `src/` folder of a local clone, so nothing is downloaded), `GOWA_REPO` and `GOWA_REF`.

GoWA runs as the LaunchAgent `local.wa-mac.gowa`, listens only on `127.0.0.1` and requires Basic Auth. The password lives in the Keychain.

WhatsApp Desktop for Mac does not receive chat lists, so the fork keeps them in `storages/wamac_lists.json`, fed by the `LabelEdit` and `LabelAssociationChat` app-state events (including the echo of its own patches). Lists made before pairing only arrive in a full sync of the `regular` app-state collection, which whatsmeow does not turn into events by default. The fork runs that sync with events enabled once after the device connects, and on demand through `POST /wa-mac/lists/resync` or `GET /wa-mac/lists?resync=true`. `wa` reads `GET /wa-mac/lists` and confirms each list operation there.

## README animation

```sh
uv run --no-project --with playwright==1.49.1 python -I scripts/flow-gif/build.py
```

It renders `scripts/flow-gif/scene.html` frame by frame and needs ffmpeg.
