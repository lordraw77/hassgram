# Development

## Layout

| File | Role |
|---|---|
| [`bot.py`](../bot.py) | Bot state, command handlers, natural-language parser, `main()` |
| [`views.py`](../views.py) | Every string and keyboard the bot sends, plus `esc`/`clip`/`tok`. Pure |
| [`callbacks.py`](../callbacks.py) | Inline-button routing |
| [`voice.py`](../voice.py) | Voice notes: transcription and its refusals |
| [`constants.py`](../constants.py) | Telegram limits and Home Assistant domain facts |
| [`ha_client.py`](../ha_client.py) | The only module that performs I/O; caching and error mapping |
| [`entities.py`](../entities.py) | Pure domain layer: search, ranking, formatting. No I/O, no Telegram |
| [`i18n.py`](../i18n.py) | Message catalogue, language detection, the two command grammars. Pure |

Dependencies point one way only — `bot` → {`callbacks`, `voice`, `views`} →
{`entities`, `ha_client`, `i18n`, `constants`}, `entities` → `i18n` — and `i18n`
imports nothing of its own. Keeping that arrow direction is the main structural
rule of the codebase.

`callbacks.on_callback` and `voice.on_voice` take the bot as their **first
argument** instead of being methods on `HassBot`; `main()` binds them with
`functools.partial`. That is what lets them live outside `bot.py` without
importing it at runtime — and it makes what each one needs from the bot visible
in its signature.

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

The suite lives in [`tests/`](../tests) and needs nothing the bot does not
already need — it is written against `unittest`, so there is no test dependency
to install:

```bash
python3 -m unittest discover        # from the project root
python3 -m unittest discover -v     # per-test names
python3 -m unittest tests.test_bot_handlers.SwitchTest   # one class
```

pytest collects it too, if you prefer its output — the same tests either way:

```bash
python3 -m pytest tests -q
```

| file | covers |
|---|---|
| [`tests/fakes.py`](../tests/fakes.py) | the sample house and the Telegram/Home Assistant fakes; no tests of its own |
| [`tests/test_entities.py`](../tests/test_entities.py) | `normalize`, `label`, `is_on`, `search` scoring and ranking, `group_by_area` ordering |
| [`tests/test_i18n.py`](../tests/test_i18n.py) | catalogue structure, `detect`, `normalize_lang`, `strip_filler`, both grammars |
| [`tests/test_ha_client.py`](../tests/test_ha_client.py) | error mapping, both caches, the areas template, the STT header |
| [`tests/test_bot_helpers.py`](../tests/test_bot_helpers.py) | `tok`/`untok` eviction, `clip`, `ha_error_text`, the selectors, the keyboards |
| [`tests/test_bot_handlers.py`](../tests/test_bot_handlers.py) | every command, callback branch, the voice pipeline, `on_error`, and `main`'s wiring |

`ha_client` is exercised through an `httpx.MockTransport` swapped into the live
client, so the real base URL and the real `Authorization` header are asserted
without opening a socket. Everything else runs against the fakes.

### Doctests

The examples in `entities.normalize()` and `i18n.parse()` are doctests and run
as part of the suite (`tests/test_i18n.py::DoctestTest`). They are driven
through `doctest.testmod` rather than the `load_tests` protocol, because pytest
does not implement `load_tests` and would silently skip them. To run them alone:

```bash
python3 -m doctest entities.py i18n.py -v
```

### Three structural tests

These fail on a mistake that would otherwise only surface in production:

- **Every catalogue key used in the code exists.** `i18n.t` raises `KeyError` on
  a missing key by design, so `test_every_key_the_code_asks_for_exists` parses
  every module at the project root for `t(lang, "…")` and `plural("…", n)` and
  checks each one against `MESSAGES`. It globs rather than reading a fixed list,
  so splitting a module cannot silently drop half the catalogue out of the check.
- **Every entry covers both languages and agrees on its placeholders**, and
  renders without raising.
- **The two marker lists do not overlap.** A word in both bumps both counters,
  which `i18n.detect` resolves as a tie — it contributes nothing while looking
  as though it does.

### Cases that encode a decision

These are the ones to check first after a change, because each pins a choice
that is easy to undo by accident:

- `unavailable` entities are excluded from bulk operations but still counted in
  the "N out of M" summary.
- A bulk operation never touches a `switch`, so "turn the house off" cannot cut
  power to the fridge.
- `_is_home` matches exactly, never as a substring, in both languages.
- Targets resolve house → room → entity, so `/accendi studio` is a *room*
  operation even when a light is also named "Studio".
- `i18n.parse("which lights are on", "en")` is `lights_on`, not `on`.
- `clip()` cuts on a line boundary, leaving `<b>` tags balanced.
- An evicted token yields "sessione scaduta" rather than an exception, and every
  callback branch answers the query — including unknown payloads.
- No handler reads a state or remembers a language before `guard` has run.

### Writing a new test

Use the fixtures in `tests/fakes.py`: `house()` returns the sample
installation, `FakeHA` records service calls, and `FakeUpdate` / `FakeQuery`
carry only the attributes the handlers touch. A handler test is three lines:

```python
b = bot.HassBot(FakeHA(), set())
update = FakeUpdate("/accendi salone")
await b.cmd_on(update, context(args=["salone"]))
assert b.ha.calls == [("light", "turn_on", [...])]
```

Derive async cases from `unittest.IsolatedAsyncioTestCase` (or from
`BotTestCase` in `test_bot_handlers.py`, which also clears the process-global
token store).

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

1. Choose a `kind` string, and build the payload as `f"{kind}:{views.tok(value)}"` —
   never put the raw value in `callback_data`, which Telegram caps at 64 bytes.
2. Handle the `kind` in `callbacks.handle`, and make sure **every** path calls
   `query.answer()`, or the client spins until it times out.
3. Handle `views.untok()` returning `None` as an expired session, not as an error.

### Adding an entity domain

`constants.LIGHT_DOMAINS` controls what `/accendi` and `/spegni` can target by name.
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
