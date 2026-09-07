# Operations

## Running as a systemd service

```bash
cp /opt/hassgram/hassgram.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hassgram
```

The shipped unit is minimal:

```ini
[Service]
Type=simple
WorkingDirectory=/opt/hassgram
ExecStart=/usr/bin/python3 /opt/hassgram/bot.py
Restart=always
RestartSec=10
```

`WorkingDirectory` matters: `load_dotenv()` resolves `.env` relative to it.

`Restart=always` is doing real work here. `post_init` calls Home Assistant
before polling starts and lets the failure propagate, so if the bot boots before
Home Assistant does, the process exits and systemd retries ten seconds later
until the instance answers. That is the intended behaviour, not a crash loop to
suppress.

### Hardening (recommended)

The unit as shipped runs as **root**, which a Telegram-facing process holding an
all-powerful Home Assistant token does not need:

```ini
[Service]
User=hassgram
Group=hassgram
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadOnlyPaths=/opt/hassgram
```

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin hassgram
chown -R root:hassgram /opt/hassgram
chmod 640 /opt/hassgram/.env
chown root:hassgram /opt/hassgram/.env
systemctl daemon-reload && systemctl restart hassgram
```

## Logs

```bash
journalctl -u hassgram -f
journalctl -u hassgram --since "1 hour ago" -p warning
```

Format: `%(asctime)s %(levelname)s %(name)s | %(message)s`. `httpx` is pinned to
`WARNING`, otherwise every API call would log a line.

What to expect at each level:

| Level | Events |
|---|---|
| `INFO` | startup (Home Assistant reachable, STT engine chosen, allow-list), every transcription |
| `WARNING` | refused chat ids (**someone found your bot**), failed transcriptions, empty allow-list at startup |
| `ERROR` | unhandled exceptions, with traceback, from `on_error` |
| `DEBUG` | ambiguous room matches, unknown callback payloads, suppressed "message is not modified" edits |

Log messages are English regardless of what language the bot is speaking to its
users.

A healthy start is exactly three INFO lines:

```
… | Home Assistant: API running.
… | Speech-to-text: stt.google_ai_stt (languages: {'it': 'it-IT', 'en': 'en-US'})
… | Bot started (allowed chats: {182700000}, default language: it)
```

Turn on debug when diagnosing the parser or the keyboards, by changing `level`
in the `logging.basicConfig` call at the top of [`bot.py`](../bot.py).

## Health

There is no health endpoint — the bot is an outbound long-poll client with no
listening socket. What to check instead:

```bash
systemctl is-active hassgram                       # process alive
journalctl -u hassgram | grep "Bot avviato" | tail -1   # last successful start
journalctl -u hassgram -p err --since today        # errors today
```

The real check is functional: send `/luci` and see whether a keyboard comes
back.

## Runbook

### The bot does not answer at all

1. `systemctl status hassgram` — is it running?
2. `journalctl -u hassgram -n 50` — did it fail at startup? A missing variable
   exits with a message naming it.
3. Restart loop with `network: ...`: the instance is
   unreachable from this host. Check with
   `curl -s -H "Authorization: Bearer $TOKEN" $HOME_ASSISTANT_API_URL`.
4. Nothing in the log at all when you message it: the bot may be polling with a
   different token, or another process is polling the same bot — Telegram gives
   updates to only one long-poll client. Check for a second instance:
   `pgrep -af bot.py`.

### "⛔️ Non sei autorizzato a usare questo bot."

Your chat id is not in `TELEGRAM_CHAT_ID`. The log line
`Access denied for chat <id>` gives you the id to add. Group ids are negative.
Restart after editing `.env` — configuration is read once at startup.

### "⚠️ Home Assistant non risponde."

The global error handler saw a `HomeAssistantError`. The chat gets the cause
localised by `bot.ha_error_text`; the log line carries the same cause in the
raw `kind[ status]: detail` form.

| In the log | In the chat | Meaning |
|---|---|---|
| `network: ...` | "Rete non raggiungibile" / "Network unreachable" | unreachable: instance down, wrong host, firewall |
| `http 401: ...` | "…ha risposto 401" / "…answered 401" | token revoked or wrong |
| `http 404: ...` | "…ha risposto 404" / "…answered 404" | wrong URL — the `/api/` suffix is required |
| `http 500: ...` | "…ha risposto 500" / "…answered 500" | a Home Assistant integration is failing; check its own log |
| `stt: ...` | "Il motore di trascrizione ha rifiutato l'audio" | the STT provider refused the clip; see [voice.md](voice.md) |

### Rooms are missing or wrong

Room names come from a template rendered **once per process** and cached for its
lifetime. After adding or renaming areas in Home Assistant, restart the bot:

```bash
systemctl restart hassgram
```

If rooms never appear at all, test the template by hand:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"template":"{{ areas() | list }}"}' \
  http://homeassistant.local:8123/api/template
```

An empty list means no areas are defined in Home Assistant, and every entity
will be filed under "Senza stanza".

### A light is listed but does not respond

Check its raw state with `/stato <name>`. `unavailable` means Home Assistant
cannot reach the device; that is upstream of the bot. Note that bulk operations
skip such entities by design, which is why "Salone (2 luci)" can report fewer
lights than the room contains.

### The bot answers in the wrong language

Language is detected per message and remembered per chat; a message too short to
carry evidence keeps the chat where it was. `/lingua it` or `/language en` pins
it. Voice messages follow the chat's current language, so a voice note recorded
right after switching is transcribed with the new tag. See
[languages.md](languages.md).

Note that the preference lives in memory: **a restart puts every chat back to
`BOT_LANGUAGE`.**

### Buttons say "Sessione scaduta"

Expected for old messages, and for any message predating the last restart. The
token store keeps the last 2000 entries. Re-issue the command.

### Messages end with "… elenco troncato."

The reply exceeded 4000 characters and was cut on a line boundary. Ask for less
— a single room instead of the whole house.

## Upgrading

```bash
systemctl stop hassgram
# update the files
pip install -r /opt/hassgram/requirements.txt
systemctl start hassgram
journalctl -u hassgram -n 20
```

Nothing persists across restarts: no database, no migrations. Two pieces of
in-memory state are lost: the callback-token store, so keyboards in old messages
report an expired session on their first tap, and the per-chat language, which
reverts to `BOT_LANGUAGE` until the next recognisable message.

`python-telegram-bot` is capped below v23 in `requirements.txt` because handler
APIs change between major versions — lifting that cap is a code change, not a
configuration one.

## Security notes

- `.env` holds two credentials that are each equivalent to full control of the
  house. `chmod 600`, and never commit it (it is in `.gitignore`).
- `TELEGRAM_CHAT_ID` is the **only** access control in the bot. Empty means
  anyone who finds the bot controls the house; the startup log warns about it.
- Watch for `Access denied for chat` in the log. Repeated hits mean the bot has
  been found by someone else.
- If the Home Assistant token leaks, revoke it in Home Assistant (Profile →
  Security → Long-lived access tokens), issue a new one, update `.env`, restart.
- If the Telegram token leaks, `/revoke` in @BotFather, then update `.env`.
