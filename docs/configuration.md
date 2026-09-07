# Installation and configuration

## Requirements

- Python 3.10 or newer (the code uses `X | Y` type unions and
  `from __future__ import annotations`).
- A reachable Home Assistant instance with its REST API enabled — it is on by
  default.
- A Telegram bot token.

## Install

```bash
cd /opt/hassgram
pip install -r requirements.txt
```

Dependencies, all pinned loosely on purpose:

| Package | Why |
|---|---|
| `python-telegram-bot>=21,<23` | Telegram client and handler framework. The major version is capped because handler APIs move between majors. |
| `httpx>=0.27` | Async HTTP client for the Home Assistant API. |
| `python-dotenv>=1.0` | Loads `.env` in `main()`. |

## Configuration

All configuration is environment variables, read in `main()`. `.env` in the
working directory is loaded first, so in practice that file *is* the
configuration.

| Variable | Required | Default | Meaning |
|---|:---:|---|---|
| `TELEGRAM_BOT_TOKEN` | ✔ | — | Bot token from [@BotFather](https://t.me/BotFather). |
| `HOME_ASSISTANT_API_URL` | ✔ | — | API root, including the `/api/` suffix: `http://homeassistant.local:8123/api/`. |
| `HOME_ASSISTANT_API_ACCESS_TOKEN` | ✔ | — | Long-lived access token. |
| `TELEGRAM_CHAT_ID` | — | *(empty)* | Comma-separated chat ids allowed to use the bot. **Empty means anyone may use it.** |
| `HA_STT_ENTITY` | — | auto-detected | Speech-to-text entity, e.g. `stt.google_ai_stt`. |
| `BOT_LANGUAGE` | — | `it` | Language for a chat that has not said anything recognisable yet, `it` or `en`. Detection overrides it per chat — see [languages.md](languages.md). |
| `STT_LANGUAGE_IT` | — | `it-IT` | Transcription tag for Italian. `STT_LANGUAGE` is still accepted as a synonym, so older `.env` files keep working. |
| `STT_LANGUAGE_EN` | — | `en-US` | Transcription tag for English. |

Missing any of the three required variables exits immediately with a message
naming them. This is the most common first-run mistake.

### Example `.env`

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...
HOME_ASSISTANT_API_URL=http://homeassistant.local:8123/api/
HOME_ASSISTANT_API_ACCESS_TOKEN=eyJhbGciOi...
TELEGRAM_CHAT_ID=182700000
# STT is optional: without these, an stt.* entity is auto-detected
#HA_STT_ENTITY=stt.google_ai_stt
# Language is detected per message; these only set defaults and STT tags
#BOT_LANGUAGE=it
#STT_LANGUAGE_IT=it-IT
#STT_LANGUAGE_EN=en-US
```

`.env` is listed in `.gitignore`. Both tokens in it are equivalent to full
control of the house, so it should be readable only by the account that runs the
bot:

```bash
chmod 600 .env
```

## Getting the credentials

### Home Assistant long-lived access token

Profile (bottom-left avatar) → **Security** tab → *Long-lived access tokens* →
**Create token**. It is shown once. It never expires but can be revoked from the
same page — do that first if it ever leaks.

Verify it before starting the bot:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://homeassistant.local:8123/api/ | jq .
# {"message": "API running."}
```

### Telegram bot token

Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, follow the
prompts. The token looks like `123456789:AA...`.

### Chat id

Send any message to your bot, then:

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
  | jq '.result[].message.chat | {id, type, title, username}'
```

Group ids are negative — keep the leading minus. Multiple ids go in one
comma-separated list; the parser extracts every integer with a regular
expression, so extra spaces, trailing commas and quotes are tolerated:

```ini
TELEGRAM_CHAT_ID=182700000, -1001234567890
```

> **Do not leave `TELEGRAM_CHAT_ID` empty in production.** With no allow-list the
> bot answers anyone who finds it, and the bot's Home Assistant token is
> all-powerful. The startup log warns about this explicitly.

## Run

```bash
python3 bot.py
```

A healthy start logs three lines:

```
… | Home Assistant: API running.
… | Speech-to-text: stt.google_ai_stt (languages: {'it': 'it-IT', 'en': 'en-US'})
… | Bot started (allowed chats: {182700000}, default language: it)
```

The first line means the URL and token work; the second names the transcription
engine (or warns that none was found); the third confirms the allow-list and the
starting language. For running it permanently, see
[operations.md](operations.md).
