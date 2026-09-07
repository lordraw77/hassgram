# Architecture

## Module boundaries

The three modules are layered, and the dependency arrows only point one way.

```
          ┌───────────────────────────────────────────┐
          │ bot.py                                    │
          │   Telegram handlers, keyboards, formatting│
          │   Italian natural-language parser         │
          │   voice pipeline                          │
          └──────────────┬─────────────┬──────────────┘
                         │             │
              ┌──────────▼───────┐  ┌──▼──────────────┐
              │ entities.py      │  │ ha_client.py    │
              │ search, ranking, │  │ HTTP, caching,  │
              │ formatting       │  │ error mapping   │
              │ (pure, no I/O)   │  └──┬──────────────┘
              └──────────────────┘     │
                                       ▼
                              Home Assistant REST API
```

- **`entities.py`** knows nothing about Telegram or HTTP. Every function is
  synchronous, pure, and operates on plain dictionaries, which makes it the
  part of the codebase that can be exercised without a running instance.
- **`ha_client.py`** knows nothing about Telegram. It is the only place that
  performs I/O and the only place that raises `HomeAssistantError`.
- **`bot.py`** depends on both and is the only module that imports
  `python-telegram-bot`.

## Request flow

Three entry points converge on one execution path:

```
/accendi studio ─────────► CommandHandler ──┐
                                            │
"accendi la luce dello studio" ─► on_text ──┤
                                            ├─► _dispatch_text ─► _switch ─┐
voice note ─► on_voice ─► STT ──────────────┘                              │
                                                                           │
                                                       _apply ◄────────────┘
                                                          │
button tap ─► on_callback ─► _handle_callback ────────────┼─► _call_on_ids
                                                          │        │
                                                          ▼        ▼
                                              _refresh_message   HA service call
```

Everything that changes the state of the house funnels through
`HassBot._call_on_ids`, which is the single place that talks to
`light.turn_on` / `turn_off`. That is what keeps the four entry points from
drifting apart in behaviour.

## Target resolution

`_switch` resolves a query in order of decreasing specificity, first rule that
yields anything wins:

1. **Whole house** — `_is_home()` matches the normalized query against a closed
   set of words (`casa`, `tutta la casa`, `ovunque`, `appartamento`, `tutto`, …).
   The match is exact, never substring: a room called "Casetta" must not trigger
   a house-wide operation.
2. **A room** — `_match_area()` compares against the area names that actually
   exist. Exact match first, then containment in either direction. An ambiguous
   partial match returns `None` and falls through rather than guessing.
3. **A single entity** — when the search returns exactly one result, or the top
   result's name equals the query exactly.
4. **Several candidates** — a keyboard is offered and nothing is switched until
   the user picks.

Rules 1 and 2 act on the `light` domain only and skip `unavailable` /
`unknown` entities (`_bulk_targets`). Two consequences worth knowing:

- "Turn off the whole house" never cuts power to a fridge or a router that
  happens to sit behind a smart plug. A switch can still be targeted by name.
- The count in the confirmation ("Salone (3 luci) spente") is the number of
  lights that actually received the command.

## Rooms without an area registry

The REST API exposes no area registry: `/api/states` knows nothing about rooms
and there is no `/api/areas`. The template engine, however, does have
`areas()`, `area_entities()` and `area_name()`, so `HomeAssistantClient.areas()`
renders this server-side:

```jinja
{% set ns = namespace(rows=[]) %}
{% for a in areas() %}
  {% for e in area_entities(a) %}
    {% set ns.rows = ns.rows + [e ~ '\t' ~ area_name(a)] %}
  {% endfor %}
{% endfor %}
{{ ns.rows | join('\n') }}
```

One `entity_id<TAB>area name` line per entity. Tab-separated because neither
field can contain a tab, while both can contain spaces and commas. Malformed
lines are skipped rather than raising, so one odd entity cannot break room
support for the whole house.

This mapping is what makes every room-aware feature work, and it is cached for
the lifetime of the process — see [Caching](#caching).

## Caching

| Cache | Lifetime | Invalidated by |
|---|---|---|
| entity states | 5 s (`states(max_age=…)`) | any service call, the refresh button |
| entity → area | process lifetime | restart only |

The 5-second window collapses the several lookups of one command into a single
HTTP request while staying fresh enough that a light toggled from the Home
Assistant app shows up almost immediately. It is invalidated explicitly whenever
the bot itself calls a service, so a confirmation keyboard is never rendered
from a snapshot the bot has just made stale.

The area mapping has no expiry: the registry changes when the user rearranges
their house, which is rare enough that a restart is an acceptable refresh, and
rendering the template is far more expensive than fetching states.

Refreshes are serialised behind an `asyncio.Lock`, so several handlers waking up
at once produce one request rather than one each.

## Callback tokens

Telegram caps `callback_data` at 64 bytes. A single "turn all these off" button
may reference two dozen entity ids, which does not fit. Buttons therefore carry
the first 12 hex characters of the value's SHA-1, and the mapping back lives in
an in-process LRU (`tok()` / `untok()`, capped at `MAX_TOKENS = 2000`).

Payload grammar — `<kind>:<rest>`, where `rest` is one or more tokens:

| Payload | Action |
|---|---|
| `area:<t>` | show a room's lights, with a toggle keyboard |
| `temp:<t>` | show a room's sensors |
| `do:<on\|off>:<t>` | switch one entity, then re-render the message |
| `all:<on\|off>:<t>` | switch every entity in the token, then re-render |
| `refresh:<t>` | re-read the states and re-render the message |

Eviction is a supported outcome, not a bug: `untok()` returns `None`, and the
caller answers "sessione scaduta" and asks the user to re-issue the command.
Buttons from a previous run of the process take the same path, as do payloads
whose `kind` this build does not recognise — with the difference that the
latter simply get an empty `answer()` so the client's spinner stops.

## Message size

Telegram rejects messages over 4096 characters, and "every light in the house"
or "every sensor per room" passes that on a large installation. `clip()` cuts at
the last newline that fits and appends a truncation notice.

Cutting on a line boundary is not an aesthetic choice: every line the bot emits
is self-contained HTML, so a line boundary is guaranteed to leave `<b>` tags
balanced. Cutting at an arbitrary offset could split a tag and get the whole
message rejected as malformed markup.

`HassBot.reply()` is the single exit point towards Telegram, which is what makes
this hold everywhere rather than at each call site. It also defaults
`parse_mode` to HTML — hence `esc()` around every interpolated value that did
not originate in the bot.

## Failure handling

Handlers deliberately do not defend against Home Assistant being unavailable.
`on_error` is registered as the application's global error handler and turns any
unhandled exception into a message:

- `HomeAssistantError` → the underlying error is shown, because "connection
  refused" and "401" tell the user immediately whether the instance is down or
  the token has expired.
- anything else → a generic apology, since its message is not meant for users.

The full traceback goes to the log either way. The one place that handles errors
locally is `on_callback`: a callback query must be answered within seconds or
the client spins, so Home Assistant failures there become a Telegram alert.

## What is deliberately absent

- **No database.** Nothing needs to survive a restart.
- **No LLM, no cloud NLU.** The vocabulary is fixed, which makes the bot
  predictable, instant, and functional without an internet connection.
- **No ffmpeg.** Telegram voice notes are Ogg/Opus, which Home Assistant's STT
  providers accept natively. See [voice.md](voice.md).
- **No per-user state.** Authorisation is a flat allow-list of chat ids.
