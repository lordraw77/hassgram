<!-- Docker Hub short description (max 100 chars, 96 used):
     Telegram bot for Home Assistant: lights, temperature and voice commands, in Italian and English.
     Paste the rest of this file into the repository Overview (limit 25,000 chars). -->

# hassgram

**A Telegram bot that drives Home Assistant — lights, temperatures and voice
notes — speaking both Italian and English.**

Ask it in whatever language you happen to be typing in: *"turn on the light in
the study"* or *"accendi la luce dello studio"*. It follows you, and answers in
the same language, voice notes included.

- **Source:** https://github.com/lordraw77/hassgram
- **Image:** `lordraw/hassgram`
- **Tags:** `latest`, plus one immutable tag per release (`1.0.0`, …)
- **Architectures:** `linux/amd64`, `linux/arm64`, `linux/arm/v7`

## Quick start

```bash
docker run -d --name hassgram --restart unless-stopped \
  -e HOME_ASSISTANT_API_URL="http://homeassistant.local:8123/api/" \
  -e HOME_ASSISTANT_API_ACCESS_TOKEN="<long-lived access token>" \
  -e TELEGRAM_BOT_TOKEN="<token from @BotFather>" \
  -e TELEGRAM_CHAT_ID="123456789" \
  lordraw/hassgram:latest
```

### docker compose

```yaml
services:
  hassgram:
    image: lordraw/hassgram:latest
    container_name: hassgram
    restart: unless-stopped
    env_file: .env
```

The bot only makes outbound connections (Telegram long polling, plus the Home
Assistant REST API), so there is no port to publish and no volume to mount. It
keeps nothing on disk; all state is a small in-memory cache. It runs as an
unprivileged user (uid 10001).

## Configuration

| variable | required | meaning |
|---|---|---|
| `HOME_ASSISTANT_API_URL` | yes | REST API endpoint, e.g. `http://homeassistant.local:8123/api/` |
| `HOME_ASSISTANT_API_ACCESS_TOKEN` | yes | Home Assistant long-lived access token |
| `TELEGRAM_BOT_TOKEN` | yes | bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | recommended | comma-separated allow-list of chat ids. **If empty the bot answers anyone** — test only |
| `BOT_LANGUAGE` | no | starting language of a new chat, `it` or `en` (default `it`) |
| `HA_STT_ENTITY` | no | speech-to-text entity, e.g. `stt.google_ai_stt`. Autodetected when unset |
| `STT_LANGUAGE_IT` | no | language tag for Italian voice notes (default `it-IT`) |
| `STT_LANGUAGE_EN` | no | language tag for English voice notes (default `en-US`) |
| `RUNNABLES_REFRESH_SECONDS` | no | how often the scene/script/automation catalogue is re-read (default `300`; `0` reads it once at startup) |

Get the access token from your Home Assistant profile page → Security →
Long-lived access tokens. Get your chat id by messaging the bot and reading the
log line it prints for unauthorised chats.

## Commands

| command | what it does |
|---|---|
| `/lights` | per-room summary plus a keyboard; tap a room to see its lights with toggles |
| `/lights living` | only the lights matching "living" |
| `/whatson` | every light currently on, grouped by room |
| `/on study` | turn on one light, a whole room, or the whole house with `/on home` |
| `/off kitchen` | turn off; ambiguous names get a button choice |
| `/temperature` | temperature and humidity for every room |
| `/temperature bathroom` | just that room |
| `/state <name>` | state of any entity — sensors, switches, climate, anything |
| `/run` | list every scene, script and automation, one button each |
| `/run cinema` | run that scene, script or automation |
| `/language it\|en` | pin the language of this chat |

`/run` is the only command that *starts* something rather than switching it, and
it picks the right service per domain: `scene.turn_on`, `script.turn_on` and
`automation.trigger` — not `automation.turn_on`, which would merely enable the
automation without running it. The catalogue it offers is read at startup and
refreshed on a cycle, so the menu answers from memory.

The commands above are also published to Telegram's own command menu at startup,
in Italian and in English, so they show up as you type `/`.

Every command has an Italian alias: `/luci`, `/accese`, `/accendi`, `/spegni`,
`/temperatura`, `/stato`, `/esegui`, `/lingua`. **The name you use is itself a language
signal:** `/lights` answers in English, `/luci` in Italian.

Plain sentences work too — *"turn everything off"*, *"how warm is it in the
bedroom?"*, *"which lights are on"*, *"run the cinema scene"* — and so does **"home"** as a stand-in for
every room at once.

## Voice notes

Send a voice message (or an audio file, or a video note) saying the same thing
you would type. The bot transcribes it, echoes back what it understood, and runs
it.

Transcription uses the speech-to-text engine **already configured in Home
Assistant** (`POST /api/stt/<entity_id>`) — no extra service, no extra API key.
Telegram's ogg/opus voice notes are passed through untouched, so no ffmpeg and
no conversion are needed. Voice is transcribed in the chat's current language,
so switch with `/language` (or just write a message in the other language)
before recording.

If your Home Assistant has no `stt.` entity, typed commands keep working and
voice notes get a polite explanation instead.

## How it works

Room names are not exposed by the Home Assistant REST API, so the
`entity_id → area` map is rendered by a Jinja template on the Home Assistant
side and cached. Entity states are cached for 5 seconds and invalidated on every
service call. Name matching is fuzzy across friendly name, entity id and room,
so "luciCucina", "kitchen" and "cucina lights" all land on the same place.

## Security notes

- Set `TELEGRAM_CHAT_ID`. An empty allow-list means anyone who finds your bot
  can switch your lights.
- The access token is a full-privilege Home Assistant credential — pass it via
  `env_file`/secrets rather than baking it into an image or a compose file in
  version control.
- The container needs no privileges, no host network and no volumes.

## Tags

- `latest` — the current release.
- `X.Y.Z` — built from the git tag of the same name, never overwritten.

Full documentation, the source and the issue tracker live at
**https://github.com/lordraw77/hassgram**.
