# Usage

The bot speaks Italian and English, and works out which one you are using from
what you write — see [languages.md](languages.md). The examples below are
Italian; every command has an English name and every sentence an English
equivalent.

## Commands

| Command | Aliases | What it does |
|---|---|---|
| `/start` | `/help`, `/aiuto` | Command reference. |
| `/luci` | `/lights` | Per-room summary of how many lights are on, plus a keyboard of rooms. |
| `/luci salone` | | Only the lights matching "salone", each as a toggle button. |
| `/accese` | | Everything currently on, grouped by room. |
| `/accendi studio` | `/on` | Turns on a light, a whole room, or the whole house with `/accendi casa`. |
| `/spegni luciCucina` | `/off` | Turns off; if the name is ambiguous, offers a choice of buttons. |
| `/temperatura` | `/temp` | Temperature and humidity for every room. |
| `/temperatura bagno` | | Only that room. |
| `/stato <name>` | `/state` | State of any entity in any domain — sensors, plugs, thermostats, media players. |
| `/lingua it\|en` | `/language` | Show or pin the chat's language. |

## The word "casa"

`casa` means *every room at once*. So do `tutto`, `tutta la casa`,
`tutte le stanze`, `ovunque` and `appartamento`.

In bulk operations — the whole house, or an entire room — only entities in the
`light` domain are touched, and `unavailable` ones are skipped. Two things
follow:

- A fridge or a router behind a smart plug is never switched off by
  "spegni casa". To act on a switch, name it.
- The count in the reply ("Tutta la casa (12 luci) spente") is the number of
  lights that actually got the command, not the number that exist.

## Natural language

Any message that is not a command goes through the same parser, so these all
work:

| You say | It does |
|---|---|
| «accendi la luce dello studio» | `/accendi studio` |
| «spegni le luci del salone» | `/spegni salone` |
| «accendi tutto» | `/accendi casa` |
| «spegni tutte le luci» | `/spegni casa` |
| «che temperatura c'è in camera da letto?» | `/temperatura camera da letto` |
| «quanti gradi in salone» | `/temperatura salone` |
| «quali luci sono accese» | `/accese` |
| «luci cucina» | `/luci cucina` |

The parser is a fixed ladder of patterns, checked in this order:

1. **Temperature** — `temperatura`, `gradi`, `umidità`, `caldo`, `freddo`.
   Checked first, because "quanti gradi in salone" also names a room and would
   otherwise read as a lighting query.
2. **Turn on** — `accendi`, `attiva` and inflections.
3. **Turn off** — `spegni`, `disattiva` and inflections.
4. **What is on** — a sentence mentioning lights *and* "acceso".
5. **Show lights** — any other sentence mentioning lights.
6. **No match** — the bot quotes your sentence back and points at `/luci`,
   `/temperatura` and `/help`.

Accents and capitalisation never matter: everything is compared after
`normalize()`, which folds accents, lowercases, and turns `_`, `.` and `'` into
spaces. «Perché» and «perche» are the same word to the bot.

There is no model behind this and no request leaves the machine. The vocabulary
is closed, which is why the bot is instant and predictable — and why it fails
cleanly on a phrasing it does not know instead of guessing.

## Buttons

Every list of lights comes with an inline keyboard:

- **One button per light.** The label shows the current state
  (🟡 on, ⚫ off, ⚠️ unreachable) and a tap applies the *opposite* action.
- **🟡 Accendi tutte / ⚫ Spegni tutte** act on exactly the lights displayed.
- **🔄 Aggiorna** re-reads the states and rewrites the message.

After any tap the message is rewritten in place, so what you see is the house as
it is now rather than as it was when the message was sent. Keyboards are capped
at 24 buttons; the text above them still lists everything.

### "Sessione scaduta, rilancia il comando"

Buttons reference entities through short tokens kept in memory, and the store
holds the last 2000. A very old message, or one from before the last restart of
the bot, has tokens that are gone — the bot says so and asks you to re-issue the
command. It is not an error, and re-sending the command always works.

## Voice messages

Send a voice note (or an audio file, or a video note) saying the same thing you
would type. The bot transcribes it, echoes what it understood, and then executes
it. It is transcribed in the chat's current language. See [voice.md](voice.md)
for setup and troubleshooting, and [languages.md](languages.md#voice) for how
that language is picked.

## Fuzzy matching

Entity search compares your text against three things: the friendly name, the
entity id, and `"<room> <name>"`. That last one is what makes "luce studio"
find an entity merely called "Faretto" that lives in the Studio area.

Matching is ranked — exact name, exact room, prefix, substring, then approximate
similarity — and anything below a similarity of 0.62 is dropped. In practice
that absorbs typos and missing plurals while still rejecting unrelated entities
on a large installation.

When several entities match equally well, the bot never guesses: it shows a
keyboard and waits.
