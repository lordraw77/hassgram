# Development

## Layout

| File | Role |
|---|---|
| [`bot.py`](../bot.py) | Telegram handlers, keyboards, formatting, natural-language parser, voice pipeline, `main()` |
| [`ha_client.py`](../ha_client.py) | The only module that performs I/O; caching and error mapping |
| [`entities.py`](../entities.py) | Pure domain layer: search, ranking, formatting. No I/O, no Telegram |
| [`i18n.py`](../i18n.py) | Message catalogue, language detection, the two command grammars. Pure |

Dependencies point one way only — `bot` → {`entities`, `ha_client`, `i18n`},
`entities` → `i18n` — and `i18n` imports nothing of its own. Keeping that arrow
direction is the main structural rule of the codebase.

## Conventions

- **Language**: identifiers, comments, docstrings and log messages are English.
  **User-facing strings live only in `i18n.MESSAGES`** — never write one inline,
  in either language. Adding a message means adding a key with both
  translations.
- **Method naming in `HassBot`**: `cmd_*` is bound to a command, `on_*` to a
  non-command update, `_*` is internal. `cmd_*` and `on_*` call `guard()` first;
  `_*` methods assume authorisation has already been checked. That is what lets
  `/accese` and the sentence "quali luci sono accese" share `_lights_on()`
  without checking twice.
- **Every outbound message goes through `HassBot.reply()`**, which applies
  `clip()` and defaults to HTML. Do not call `reply_text` directly.
- **Escape everything interpolated** with `esc()` unless the bot itself wrote
  it. Entity and area names come from Home Assistant and can contain `&` or `<`;
  user queries are echoed back in several error messages.
- **Docstrings** are Google-style with `Args:` / `Returns:` / `Raises:`, and
  explain *why* rather than restating the signature.

## Running locally

```bash
pip install -r requirements.txt
python3 bot.py
```

The bot polls, so no public URL or webhook is needed. Point it at a real Home
Assistant instance; there is no mock mode.

To see the parser and keyboard decisions, raise the log level in
`logging.basicConfig` at the top of [`bot.py`](../bot.py) to `logging.DEBUG`.

## Testing

There is no test suite yet. The natural place to start is `entities.py`, which
is pure and needs nothing but dictionaries, followed by the `HassBot` static
helpers (`_is_home`, `_strip_verbs`, `_audio_format`, `_bulk_targets`) and the
module-level `clip` / `tok` / `untok`.

The examples in `normalize()` are doctests and already run:

```bash
python3 -m doctest entities.py -v
```

For handler-level tests, `HassBot` can be driven without Telegram or Home
Assistant by passing a fake client and a fake update — the handlers only touch
`update.effective_message.reply_text`, `update.effective_chat.id` and
`message.chat.send_action`:

```python
import asyncio, types
from bot import HassBot

STATES = [{"entity_id": "light.studio", "state": "off",
           "attributes": {"friendly_name": "Luce studio"}}]
AREAS = {"light.studio": "Studio"}

class FakeHA:
    def __init__(self): self.calls = []
    async def states(self): return STATES
    async def areas(self): return AREAS
    async def call_service(self, d, s, data): self.calls.append((d, s, data["entity_id"]))
    def invalidate_states(self): pass

class FakeMessage:
    def __init__(self): self.sent = []
    async def reply_text(self, text, **kw): self.sent.append(text)

def fake_update(chat_id=1):
    return types.SimpleNamespace(effective_message=FakeMessage(),
                                 effective_chat=types.SimpleNamespace(id=chat_id))

ha = FakeHA()
bot = HassBot(ha, {1})
u = fake_update()
asyncio.run(bot._dispatch_text(u, "accendi la luce dello studio"))
assert ha.calls == [("light", "turn_on", ["light.studio"])]
print(u.effective_message.sent[0])
```

Cases worth covering, because they encode decisions that are easy to break:

- `unavailable` entities are excluded from bulk operations but still counted in
  the "N out of M" summary.
- `_is_home` matches exactly, never as a substring, in both languages.
- `i18n.strip_filler("accendi tutto", "it")` and
  `i18n.strip_filler("turn everything off", "en")` both return `"casa"`, not `""`.
- `i18n.detect` leaves an ambiguous message on the chat's current language.
- `i18n.parse("which lights are on", "en")` is `lights_on`, not `on`.
- `clip()` leaves `<b>` tags balanced.
- An evicted token yields "sessione scaduta" rather than an exception.
- Every callback branch answers the query, including unknown payloads.

## Common changes

### Adding a command

1. Write an `async def cmd_x(self, update, ctx)` on `HassBot`, starting with
   `if not await self.guard(update): return`.
2. If the behaviour should also be reachable from a sentence, put the body in a
   `_x()` helper that does *not* guard, and have both callers use it — the
   pattern of `cmd_on_now` / `_lights_on`.
3. Register it in `main()`:
   `app.add_handler(CommandHandler(["italian", "english"], hass.cmd_x))`.
4. Add it to the `/start` text, to [usage.md](usage.md) and to the README table.

Handler order matters: the catch-all text handler is registered last and
excludes commands, so an unknown `/command` is not fed to the parser.

### Teaching it a new phrasing

The grammar lives in `i18n.PATTERNS` and `i18n.RULES`, one entry per language;
`_dispatch_text` only maps the resulting intent to a handler. Everything
operates on `normalize()` output — lowercase, no accents, `_ . '` turned into
spaces — so patterns must be written in that form: `perche`, not `perché`, and
`what s` for `what's`.

Adding a verb usually means touching two things in the same language's pattern
set: the expression that recognises the intent, and the `strip` list that
removes the verb before the remainder is searched. **Never add a word to a
`strip` list that could be part of a room or entity name** — it would be deleted
from every query in that language.

Rule order in `RULES` is significant and differs between the languages.
Temperature is checked first in both, because "quanti gradi in salone" names a
room too. English then checks the listing rule before `off`/`on`, because `on`
is both the imperative particle and the state — see
[languages.md](languages.md#natural-language).

Adding a whole language is a contained job, described in
[languages.md](languages.md#adding-a-third-language).

### Adding a callback button

1. Choose a `kind` string, and build the payload as `f"{kind}:{tok(value)}"` —
   never put the raw value in `callback_data`, which Telegram caps at 64 bytes.
2. Handle the `kind` in `_handle_callback`, and make sure **every** path calls
   `query.answer()`, or the client spins until it times out.
3. Handle `untok()` returning `None` as an expired session, not as an error.

### Adding an entity domain

`LIGHT_DOMAINS` controls what `/accendi` and `/spegni` can target by name.
`_bulk_targets` separately restricts bulk operations to `light` — deliberately,
so "spegni casa" cannot cut power to a fridge behind a smart plug. Changing the
first without understanding the second is the kind of edit that turns a
convenience into an incident.

## Things that will bite you

| Trap | Why |
|---|---|
| Calling `reply_text` directly | Skips `clip()`; the message will be rejected outright once the list grows past 4096 characters. |
| Putting real values in `callback_data` | 64-byte cap. Use `tok()`. |
| Forgetting `query.answer()` | The button spins for ~30 seconds. |
| Using `is_on()` to pick bulk targets | It folds `unavailable` into "off", so unreachable lights would be counted as acted upon. Filter on the raw state, as `_bulk_targets` does. |
| Writing accented patterns | `normalize()` strips accents before matching, so `è` can never match. |
| Writing a user-facing string inline | It will only ever exist in one language. Add a key to `i18n.MESSAGES`. |
| Adding `on`/`off` to an English pattern casually | `on` is both a verb particle and a state; rule order in `RULES` is what keeps them apart. |
| Assuming `attributes` keys exist | Home Assistant omits attributes freely; use `.get()`. |
| Expecting area changes to appear | The area map is cached for the process lifetime. Restart. |
| Catching bare `Exception` around `edit_message_text` | It hides real failures. Catch `BadRequest` and re-raise anything that is not "message is not modified". |

## API surface

Module-by-module reference: [api-reference.md](api-reference.md).
