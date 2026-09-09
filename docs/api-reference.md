# API reference

Signatures and one-line summaries for every module, class and function. The full
prose — arguments, return values, failure modes and the reasoning behind each
decision — lives in the docstrings themselves; this page is the map.

Read it alongside [architecture.md](architecture.md), which explains how these
pieces fit together.


## `entities`

[`entities.py`](../entities.py) — Pure domain layer: search, ranking and formatting over plain state
dictionaries. No I/O, no Telegram types, nothing to mock.

### Module constants

| Name | Value | Purpose |
|---|---|---|
| `OFF_STATES` | `{'off', 'unavailable', 'unknown', 'none', ''}` | States that count as "not on", `unavailable` and `unknown` included. |
| `NO_AREA` | `'\x00no-area\x00'` | Language-neutral sentinel for entities in no room; rendered per conversation. |

### Functions

#### `normalize(text: str) -> str`

Fold a string into the canonical form used for every comparison.

#### `friendly_name(state: dict[str, Any]) -> str`

Return the human-readable name of an entity.

#### `label(state: dict[str, Any], areas: dict[str, str]) -> str`

Return a display name disambiguated by room when that adds information.

#### `is_on(state: dict[str, Any]) -> bool`

Report whether an entity counts as active.

#### `state_icon(state: dict[str, Any]) -> str`

Pick the emoji that represents an entity's state on a button or in a list.

#### `state_text(state: dict[str, Any], lang: str = i18n.DEFAULT_LANG) -> str`

Translate a raw state into the word shown to the user.

#### `search(query: str, states: list[dict[str, Any]], areas: dict[str, str], domains: tuple[str, ...] | None = None, limit: int = 8) -> list[dict[str, Any]]`

Find the entities that best match a free-text query, best match first.

#### `group_by_area(states: list[dict[str, Any]], areas: dict[str, str]) -> dict[str, list[dict[str, Any]]]`

Group entities by room for the per-area summaries and keyboards.


## `i18n`

[`i18n.py`](../i18n.py) — Everything that differs between Italian and English: the message catalogue,
the language detector and the two command grammars. Pure, like `entities`.

### Module constants

| Name | Value | Purpose |
|---|---|---|
| `LANGS` | `('it', 'en')` | Supported language codes. |
| `DEFAULT_LANG` | `'it'` | Fallback language, overridable with `BOT_LANGUAGE`. |
| `MESSAGES` | `{'unauthorized': {'it': '⛔️ Non sei autorizzato a usare q…` | The whole catalogue: key → one string per language. |
| `MARKERS` | `{'it': '\\b(accendi\|accende\|accendere\|accesa\|accese\|acces…` | Per-language words used as evidence by `detect`; no word appears in both. |
| `HOME_WORDS` | `{'it': {'casa', 'tutta casa', 'tutta la casa', 'tutte le …` | Words meaning "the whole house", per language; matched exactly. |
| `HOME_TOKEN` | `'casa'` | Canonical internal target a whole-house sentence reduces to. |
| `_IT` | `{'temperature': '\\b(temperatur\\w*\|caldo\|freddo\|umidit\\…` |  |
| `_EN` | `{'temperature': '\\b(temperature\|temp\|degrees\|warm\|cold\|h…` |  |
| `PATTERNS` | `{'it': _IT, 'en': _EN}` | Per-language regex fragments used by the grammar. |
| `RULES` | `{'it': (('temperature', (_IT['temperature'],)), ('on', (_…` | Ordered (intent, patterns) per language; first match wins. |

### Functions

#### `plural(key: str, count: int) -> str`

Pick the singular or plural variant of a catalogue key.

#### `t(lang: str, key: str, **kwargs: object) -> str`

Render a catalogue entry in the requested language.

#### `detect(low: str, fallback: str = DEFAULT_LANG) -> str`

Guess which language a sentence is written in.

#### `normalize_lang(value: str | None, fallback: str = DEFAULT_LANG) -> str`

Coerce a user- or environment-supplied language tag to a supported one.

#### `is_home(query: str) -> bool`

Report whether a query means "the whole house", in either language.

#### `strip_filler(low: str, lang: str) -> str`

Reduce a sentence to the thing it talks about.

#### `parse(low: str, lang: str) -> tuple[str | None, str]`

Turn a normalized sentence into an intent and a target.


## `ha_client`

[`ha_client.py`](../ha_client.py) — The only module that performs I/O. Owns the HTTP session, the two caches,
and the single exception type every failure is mapped to.

### `class HomeAssistantError`

Raised for every failure while talking to Home Assistant. Carries the failure
*structured* — `kind` (`network`, `http`, `stt`, `generic`), `status` and `detail` —
rather than a ready-made sentence: this module does not know which language the
chat is speaking. `bot.ha_error_text` turns it into the localised line.

| Signature | Summary |
|---|---|
| `__init__(detail: str, kind: str = 'generic', status: int \| None = None)` | Build the error. |

### `class HomeAssistantClient`

Async facade over the subset of the Home Assistant REST API that Hassgram uses.

**Public interface**

| Signature | Summary |
|---|---|
| `__init__(base_url: str, token: str, timeout: float = 15.0) -> None` | Build the client and its shared HTTP session. |
| `async aclose() -> None` | Close the underlying HTTP session and release its connection pool. |
| `async ping() -> str` | Check that the instance is reachable and the token is accepted. |
| `async states(max_age: float = 5.0) -> list[dict[str, Any]]` | Return every entity state, served from a short-lived cache. |
| `invalidate_states() -> None` | Drop the cached snapshot so the next `states` call refetches. |
| `async render_template(template: str) -> str` | Render a Jinja template inside Home Assistant and return its output. |
| `async call_service(domain: str, service: str, data: dict[str, Any]) -> Any` | Invoke a Home Assistant service and invalidate the state cache. |
| `async stt_entities() -> list[str]` | List the speech-to-text entities exposed by Home Assistant. |
| `async speech_to_text(audio: bytes, entity_id: str, language: str = 'it-IT', audio_format: str = 'ogg', codec: str = 'opus', sample_rate: int = 16000) -> str` | Transcribe an audio clip with a Home Assistant speech-to-text entity. |
| `async stt_options(entity_id: str) -> dict[str, Any]` | Read the formats a speech-to-text entity accepts. |
| `async areas() -> dict[str, str]` | Return the entity-to-room mapping, rendered once and cached forever. |

**Internals**

| Signature | Summary |
|---|---|
| `async _request(method: str, path: str, **kwargs: Any) -> Any` | Perform one HTTP request and normalise its outcome. |


## `constants`

[`constants.py`](../constants.py) — The limits Telegram imposes and the facts about Home Assistant
domains, in one place. Imported by every other module of the front end; imports nothing itself.

### Module constants

| Name | Value | Purpose |
|---|---|---|
| `LIGHT_DOMAINS` | `('light', 'switch')` | Domains `/accendi` and `/spegni` may target by name. Bulk operations are narrower — see `HassBot._bulk_targets`. |
| `RUN_SERVICES` | `{'script': 'turn_on', 'automation': 'trigger'}` | Domains `/esegui` may execute, and the service each one is run with. |
| `RUN_DOMAINS` | `tuple(RUN_SERVICES)` | The same domains as a tuple, for filtering a state list. |
| `RUN_ICONS` | `{'script': '📜', 'automation': '⚙️'}` | Per-domain icon in listings and buttons. |
| `RUN_ICON_DEFAULT` | `'▶️'` | Fallback icon, so a domain added to `RUN_SERVICES` still gets a button. |
| `RUNNABLES_REFRESH_SECONDS` | `300.0` | How often the runnable catalogue is re-read; `0` disables the cycle. |
| `MAX_BUTTONS` | `24` | Cap on buttons per keyboard; the text above still lists everything. |
| `MAX_RUN_PAGES` | `20` | Most messages one `/esegui` listing may occupy. |
| `MAX_VOICE_BYTES` | `5 * 1024 * 1024` | Voice clip cap, checked before download. ~5 minutes of Opus. |
| `MAX_MESSAGE_CHARS` | `4000` | Truncation threshold, kept under Telegram's 4096 limit. |
| `MAX_TOKENS` | `2000` | Size of the callback-token LRU. |
| `MAX_CHAT_LANGS` | `500` | Size of the remembered-language LRU. |


## `views`

[`views.py`](../views.py) — Rendering: every string and every inline keyboard the bot sends.
Pure — no I/O, no bot state — which is why the rendering tests need neither a Telegram nor a
Home Assistant. It also owns the callback-token store behind `tok` and `untok`.

### Functions

#### `tok(value: str) -> str`

Store a value and return a short token that fits in `callback_data`.

#### `untok(key: str) -> str | None`

Resolve a token produced by `tok` back to its value.

#### `esc(text: Any) -> str`

Escape a value for Telegram's HTML parse mode.

#### `ha_error_text(lang: str, exc: HomeAssistantError) -> str`

Render a Home Assistant failure as a localised line.

#### `clip(text: str, lang: str = i18n.DEFAULT_LANG, limit: int = MAX_MESSAGE_CHARS) -> str`

Shorten a message so Telegram will accept it, keeping the HTML valid.

#### `runnable_entry(state: dict[str, Any], lang: str = i18n.DEFAULT_LANG) -> str`

Render one entity as its two lines of the listing.

#### `runnables_pages(runnables: list[dict[str, Any]], lang: str = i18n.DEFAULT_LANG, limit: int = MAX_MESSAGE_CHARS, per_page: int = MAX_BUTTONS) -> list[tuple[str, list[dict[str, Any]]]]`

Render the listing of everything that can be run, split into messages.

#### `run_keyboard(runnables: list[dict[str, Any]], lang: str = i18n.DEFAULT_LANG) -> InlineKeyboardMarkup`

Build a keyboard that runs one entity per button.

#### `sensor_line(s: dict[str, Any], areas: dict[str, str], lang: str, short: bool = False) -> str`

Render one sensor or thermostat as a display line.

#### `area_name(name: str, lang: str) -> str`

Render a grouping key from `entities.group_by_area` for display.

#### `areas_summary(lights: list[dict[str, Any]], areas: dict[str, str], lang: str) -> str`

Render the "N on out of M" overview that heads the light browser.

#### `areas_keyboard(states: list[dict[str, Any]], areas: dict[str, str], prefix: str = 'area') -> InlineKeyboardMarkup`

Build a keyboard of rooms, two buttons per row.

#### `lights_text(title: str, lights: list[dict[str, Any]], lang: str) -> str`

Render a list of lights with their state.

#### `lights_keyboard(lights: list[dict[str, Any]], lang: str = i18n.DEFAULT_LANG) -> InlineKeyboardMarkup`

Build a toggle keyboard for a list of lights.

#### `choice_keyboard(found: list[dict[str, Any]], areas: dict[str, str], turn_on: bool, lang: str = i18n.DEFAULT_LANG) -> InlineKeyboardMarkup`

Build the "which one did you mean?" keyboard for an ambiguous switch.


## `callbacks`

[`callbacks.py`](../callbacks.py) — Inline buttons: authorise a tap, route its `<kind>:<token>`
payload, act, re-render the message. Every handler takes the bot as its first argument.

### Functions

#### `async on_callback(bot: 'HassBot', update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None`

Entry point for every inline-button tap.

#### `async resolve_token(query, token: str, lang: str) -> str | None`

Resolve a `callback_data` token, answering the query when it is gone.

#### `async handle(bot: 'HassBot', query, data: str, lang: str = i18n.DEFAULT_LANG) -> None`

Route a callback query to its action.

#### `async refresh_message(bot: 'HassBot', query, ids: list[str], lang: str = i18n.DEFAULT_LANG) -> None`

Re-read the given entities and rewrite the message in place.


## `voice`

[`voice.py`](../voice.py) — Voice notes: download the clip, transcribe it through Home
Assistant's speech-to-text, echo it back, then run it through the same interpreter as typed
text. Takes the bot as its first argument.

### Functions

#### `async on_voice(bot: 'HassBot', update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None`

Transcribe a voice message and execute it as if it had been typed.

#### `audio_format(mime_type: str | None) -> tuple[str, str]`

Derive the container and codec to declare for a Telegram clip.


## `bot`

[`bot.py`](../bot.py) — The bot's state and its command handlers, plus process startup. The rest
of the Telegram front end lives in `views`, `callbacks`, `voice` and `constants`.

### Module constants

| Name | Value | Purpose |
|---|---|---|
| `COMMAND_LANG` | `{'luci': 'it', 'accese': 'it', 'accendi': 'it', 'spegni':…` | Command names that identify a language; shared names carry no signal. |

### Functions

#### `async on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None`

Global error handler: turn any unhandled exception into a reply.

#### `async set_command_menu(app: Application) -> None`

Publish the command menu Telegram shows next to the text box.

#### `async post_init(app: Application) -> None`

Startup hook: verify Home Assistant and choose a transcription engine.

#### `async post_shutdown(app: Application) -> None`

Shutdown hook: close the Home Assistant HTTP session.

#### `main() -> None`

Load configuration, wire the handlers and run the bot until interrupted.

### `class HassBot`

Stateful holder for the bot's handlers.

**Public interface**

| Signature | Summary |
|---|---|
| `__init__(ha: HomeAssistantClient, allowed_chats: set[int], stt_entity: str \| None = None, stt_languages: dict[str, str] \| None = None, default_lang: str = i18n.DEFAULT_LANG, runnables_refresh: float = RUNNABLES_REFRESH_SECONDS) -> None` | Wire the bot to its dependencies. |
| `async discover_stt() -> None` | Pick a speech-to-text engine when one was not configured explicitly. |
| `async refresh_runnables() -> list[dict[str, Any]]` | Re-read the scripts and automations and replace the cache. |
| `async runnables() -> list[dict[str, Any]]` | Return the runnable catalogue, reading it if the cache is empty. |
| `start_refreshing() -> None` | Start the background task that keeps the catalogue current. |
| `async stop_refreshing() -> None` | Cancel the refresh task and wait for it to finish. |
| `authorized(update: Update) -> bool` | Check whether an update comes from a permitted chat. |
| `async guard(update: Update) -> bool` | Authorise a message-bearing update, replying if it is refused. |
| `async snapshot() -> tuple[list[dict[str, Any]], dict[str, str]]` | Fetch the two views of Home Assistant that nearly every command needs. |
| `lang_of(update: Update) -> str` | Return the language currently in use for an update's chat. |
| `resolve_lang(update: Update, text: str \| None = None) -> str` | Work out which language to answer an update in, and remember it. |
| `async reply(update: Update, text: str, lang: str = i18n.DEFAULT_LANG, **kwargs: Any) -> None` | Send a message to the chat an update came from. |
| `async cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/start`, `/help` and `/aiuto`: print the command reference. |
| `async cmd_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/lingua` and `/language`: show or set the chat's language. |
| `async cmd_lights(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/luci [query]`: browse lights, by room or by name. |
| `async cmd_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/accendi <name>` and `/on <name>`: turn something on. |
| `async cmd_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/spegni <name>` and `/off <name>`: turn something off. |
| `async cmd_on_now(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/accese`: list everything currently on. |
| `async cmd_temperature(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/temperatura [room]`: report temperature and humidity. |
| `async cmd_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/stato <query>`: inspect any entity, in any domain. |
| `async cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None` | Handle `/esegui [nome]` and `/run [name]`: run a script or an automation. |
| `async on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None` | Entry point for any non-command text message. |

**Internals**

| Signature | Summary |
|---|---|
| `async _refresh_loop() -> None` | Re-read the catalogue every `runnables_refresh` seconds, forever. |
| `_lights(states: list[dict[str, Any]], areas: dict[str, str] \| None = None, area: str \| None = None, domains: tuple[str, ...] = ('light',)) -> list[dict[str, Any]]` | Select the lighting entities out of a full state snapshot. |
| `_runnables(states: list[dict[str, Any]], domains: tuple[str, ...] = RUN_DOMAINS) -> list[dict[str, Any]]` | Select the executable entities -- scripts and automations. |
| `_temp_sensors(states: list[dict[str, Any]], areas: dict[str, str] \| None = None, area: str \| None = None) -> list[dict[str, Any]]` | Select the temperature and humidity sensors out of a state snapshot. |
| `_remember_lang(update: Update, lang: str) -> None` | Record the language a chat is speaking, keeping the store bounded. |
| `async _lights_browse(update: Update, query: str, lang: str, overview_on_miss: bool = False) -> None` | Show the light browser: an overview, or the lights matching a query. |
| `async _lights_on(update: Update, lang: str) -> None` | List every light that is currently on, grouped by room. |
| `async _run(update: Update, query: str, lang: str) -> None` | Resolve what the user wants to run, and run it. |
| `async _areas_or_empty() -> dict[str, str]` | Return the areas mapping, or an empty one when it cannot be read. |
| `async _execute(update: Update, target: dict[str, Any], lang: str) -> None` | Run one entity and confirm it in the chat. |
| `async _switch(update: Update, query: str, turn_on: bool, lang: str) -> None` | Resolve what the user meant and turn it on or off. |
| `_bulk_targets(lights: list[dict[str, Any]], areas: dict[str, str], area: str \| None = None) -> list[dict[str, Any]]` | Select the lights a bulk operation should act on. |
| `async _call_on_ids(ids: list[str], turn_on: bool) -> None` | Turn a set of entities on or off with one service call per domain. |
| `async _run_ids(ids: list[str]) -> None` | Execute a set of scripts or automations. |
| `async _apply(update: Update, targets: list[dict[str, Any]], turn_on: bool, lang: str, title: str \| None = None) -> None` | Execute a switch operation and confirm it in the chat. |
| `async _temperature(update: Update, query: str, lang: str) -> None` | Report temperature and humidity, for one room or for the whole house. |
| `_is_home(query: str) -> bool` | Decide whether a query refers to the whole house. |
| `_match_area(query: str, areas: dict[str, str]) -> str \| None` | Match a query against the names of the rooms that exist. |
| `async _dispatch_text(update: Update, text: str, spoken: bool = False) -> None` | Interpret a sentence and run the command it describes. |
