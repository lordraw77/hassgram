# Hassgram documentation

Hassgram is a Telegram bot that drives a [Home Assistant](https://www.home-assistant.io/)
installation over its REST API. It answers typed commands, free-form Italian
sentences and voice messages, and it renders inline keyboards so the common
follow-up ("now turn that one off") is a tap rather than another command.

The bot speaks Italian to its users; the code, comments and this documentation
are in English.

## Where to start

| If you want to… | Read |
|---|---|
| install and configure the bot | [configuration.md](configuration.md) |
| know what you can say to it | [usage.md](usage.md) |
| understand how it is put together | [architecture.md](architecture.md) |
| set up or debug voice commands | [voice.md](voice.md) |
| look up a module, class or function | [api-reference.md](api-reference.md) |
| run it as a service, or fix a broken one | [operations.md](operations.md) |
| change or extend it | [development.md](development.md) |

## At a glance

- **Three modules, ~800 lines.** [`bot.py`](../bot.py) owns everything
  Telegram-shaped, [`ha_client.py`](../ha_client.py) owns all HTTP traffic, and
  [`entities.py`](../entities.py) is a pure domain layer with no I/O.
- **Three dependencies**: `python-telegram-bot`, `httpx`, `python-dotenv`.
- **No database, no message queue, no external AI service.** The natural
  language parser is a fixed set of regular expressions; transcription reuses
  the speech-to-text engine already configured inside Home Assistant.
- **Stateless across restarts.** The only in-memory state is a cache of entity
  states, the room mapping, and the callback-token LRU — all rebuilt on demand.

## Design constraints worth knowing

Four Telegram and Home Assistant limits shape a surprising amount of the code.
They are explained where they bite, but in summary:

| Limit | Value | Consequence |
|---|---|---|
| `callback_data` size | 64 bytes | Buttons carry a 12-char token; the real value lives in an in-process LRU. See [architecture.md](architecture.md#callback-tokens). |
| Message length | 4096 chars | Every reply is truncated on a line boundary by `clip()`. |
| Service call addressing | one domain per call | Targets are grouped by domain before switching. |
| REST API area registry | not exposed | Room names are rendered by a Jinja template inside Home Assistant. |
