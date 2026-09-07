# Languages

Hassgram speaks Italian and English. There is nothing to configure: it works out
which language a message is in and answers in the same one.

## How the language is chosen

Three sources of evidence, checked in this order:

1. **An explicit `/language` command.** `/lingua it`, `/language en`, and
   anything `i18n.normalize_lang` recognises (`en-GB`, `italiano`, `EN`). This
   pins the chat until it is changed.
2. **The command name.** `/luci` is Italian, `/lights` is English. Names the two
   languages share — `/start`, `/help`, `/on`, `/off`, `/temp` — carry no signal
   and leave the language alone: switching a conversation to English because
   someone typed `/on` would be worse than doing nothing.
3. **The words themselves.** `i18n.detect` counts marker words from each
   language and picks the winner.

Whatever wins becomes the chat's remembered language, which matters for
everything that carries no language of its own:

- **Button taps.** A callback query is just a token; the keyboard is re-rendered
  in the chat's language.
- **Voice messages.** Audio has no detectable language before it is transcribed,
  so the chat's language decides which tag is sent to the speech-to-text engine.
- **Error messages**, including those raised before any text was parsed.

## Detection, concretely

`i18n.MARKERS` holds one regular expression per language, built from words that
occur in one language and not the other — `accendi`, `luci`, `stanza`, `quanti`
against `turn`, `lights`, `room`, `which`. No word appears in both lists, so the
two counts are independent evidence rather than a tug of war.

The higher count wins. **A tie, or no marker at all, keeps the chat where it
was.** This is deliberate:

```
"salone"                    → no markers      → language unchanged
"accendi il salone"         → it:2, en:0      → Italian
"turn on the living room"   → it:0, en:4      → English
```

Short messages are legitimately ambiguous, and flipping the language on one weak
signal is more annoying than staying put. `/language` is always available when
you want to be explicit.

Everything is compared after `entities.normalize`: lowercase, accents folded,
`_ . '` turned into spaces. Patterns are written in that form — `perche`, never
`perché`, and `what s` for the elided `what's`.

## Commands

Every command has a name in each language. They are the same handler.

| Italian | English | |
|---|---|---|
| `/luci` | `/lights` | browse lights |
| `/accese` | `/whatson` | what is on right now |
| `/accendi` | `/on` | turn on |
| `/spegni` | `/off` | turn off |
| `/temperatura` | `/temperature`, `/temp` | temperature and humidity |
| `/stato` | `/state` | any entity's state |
| `/aiuto` | `/help`, `/start` | command reference |
| `/lingua` | `/language` | show or set the language |

## Natural language

The two grammars live in `i18n.RULES` and produce the same
language-independent intents, so every handler below the parser is shared.

| Intent | Italian | English |
|---|---|---|
| `temperature` | «quanti gradi in salone» | “how warm is it in the bedroom” |
| `on` | «accendi la luce dello studio» | “turn on the light in the study” |
| `off` | «spegni tutte le luci» | “turn everything off” |
| `lights_on` | «quali luci sono accese» | “which lights are on” |
| `lights` | «luci cucina» | “kitchen lights” |

Rule *order* differs between the two, and the reason is worth knowing.

Italian can check the switching verbs first, because `accendi` (turn on) and
`accese` (are on) are different words. English cannot: **`on` is both the
imperative particle and the state**, so “which lights are on” would parse as a
turn-on command. The English listing rule therefore comes first, guarded by an
interrogative (`what`, `which`, `is`, `are`), and only then are `off` and `on`
checked.

Whole-house words are accepted in both languages regardless of the
conversation's language — `casa`, `tutto`, `ovunque` alongside `house`, `home`,
`everything`, `everywhere`. The word has already been typed; refusing to
understand `everything` because the chat is in Italian would be pedantry.

## What is *not* translated

Room and entity names come from Home Assistant and are shown, and matched,
exactly as it spells them. The bot translates its own words, not your house.

So on an installation whose areas are named in Italian, an English sentence must
still use those names:

```
"turn on the study"    → nothing found   (the area is called "Studio")
"turn on the studio"   → works
```

This is the right behaviour — a bot guessing that "study" means "Studio" would
guess wrong at least as often — but it is worth knowing before wondering why an
English command found nothing. If you want both languages to feel native, name
your areas in Home Assistant in whichever language you will actually speak, or
add an alias entity per room.

The fuzzy search helps across the gap more often than you would expect, since it
matches on prefixes and approximate similarity: "bagno" finds "Bagno grande",
and "bedroom" finds "Bedroom lamp".

## Voice

The tag sent to Home Assistant's speech-to-text engine follows the chat:

| Chat language | Tag | Override |
|---|---|---|
| Italian | `it-IT` | `STT_LANGUAGE_IT`, or the older `STT_LANGUAGE` |
| English | `en-US` | `STT_LANGUAGE_EN` |

**The engine must advertise the language**, or it rejects the request with a 400
before decoding any audio. Check what yours supports:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://homeassistant.local:8123/api/stt/stt.google_ai_stt | jq .languages
```

To dictate in the other language, send one text message in it — or run
`/language` — before recording. The bot does not retry a failed transcription in
the other language: that would double the calls, and the cost, on every voice
message it did not understand.

## Adding a third language

The work is contained, and it is all in [`i18n.py`](../i18n.py):

1. Add the language code to `LANGS`.
2. Add its string to every entry in `MESSAGES`. A missing key raises; a missing
   *language* inside an existing key silently falls back to `DEFAULT_LANG`, so
   a partial translation degrades rather than breaking.
3. Add a `MARKERS` entry — words that occur in that language and in none of the
   others already listed.
4. Add a pattern set and a `RULES` entry, ordered so that ambiguous words are
   disambiguated before they are used (the English `on` problem above).
5. Add command names to `COMMAND_LANG` and register them in `bot.main`.
6. Add an STT tag to the `stt_languages` mapping built in `bot.main`.

Nothing outside `i18n.py` and `bot.main` needs to change: the handlers only ever
see intents and catalogue keys.
