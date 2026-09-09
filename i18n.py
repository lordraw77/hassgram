"""Message catalogue, language detection and the bilingual command grammar.

Hassgram speaks Italian and English. This module holds everything that differs
between the two, so the rest of the codebase can stay language-agnostic: it
resolves a language, parses a sentence into an intent, and renders every
user-facing string. Like :mod:`entities` it is pure -- no I/O, no Telegram, no
HTTP -- which makes the parser and the detector testable on their own.

Three things live here:

Catalogue
    :data:`MESSAGES` maps a key to one string per language, rendered by
    :func:`t`. No user-facing text is written anywhere else in the codebase.

Detection
    :func:`detect` guesses the language of a sentence by counting marker words.
    The bot uses it on every incoming message, so a user can switch language
    mid-conversation without configuring anything.

Grammar
    :data:`RULES` and :func:`parse` turn a normalized sentence into an
    ``(intent, target)`` pair. The intents are language-independent, which is
    what lets one handler serve both languages.

Everything here operates on :func:`entities.normalize` output: lowercase, no
accents, with ``_``, ``.`` and ``'`` folded to spaces. Patterns must therefore
be written in that form -- ``perche``, never ``perché``, and ``what s`` for the
elided ``what's``.
"""

from __future__ import annotations

import re
from typing import Literal

Lang = Literal["it", "en"]
LANGS: tuple[Lang, ...] = ("it", "en")
DEFAULT_LANG: Lang = "it"


# --------------------------------------------------------------- catalogue

MESSAGES: dict[str, dict[str, str]] = {
    # --- authorisation
    "unauthorized": {
        "it": "⛔️ Non sei autorizzato a usare questo bot.",
        "en": "⛔️ You are not authorised to use this bot.",
    },
    "unauthorized_toast": {"it": "Non autorizzato", "en": "Not authorised"},
    # --- help
    "help": {
        "it": (
            "🏠 <b>Hassgram</b> — controllo di Home Assistant\n\n"
            "<b>Comandi</b>\n"
            "/luci — stanze e luci, con accensione/spegnimento a bottoni\n"
            "/luci <i>stanza</i> — solo le luci di quella stanza\n"
            "/accese — tutto ciò che è acceso in questo momento\n"
            "/accendi <i>nome</i> — es. <code>/accendi studio</code>, <code>/accendi casa</code>\n"
            "/spegni <i>nome</i> — es. <code>/spegni luciCucina</code>\n"
            "/temperatura [<i>stanza</i>] — es. <code>/temperatura salone</code>\n"
            "<i>«casa» vale come tutte le stanze insieme.</i>\n"
            "/stato <i>nome</i> — stato di una qualsiasi entità\n"
            "/esegui [<i>nome</i>] — scene, script e automazioni; senza nome te le elenco\n"
            "/lingua <i>it|en</i> — cambia lingua\n\n"
            "Puoi anche scrivermi in linguaggio naturale: "
            "<i>«accendi la luce dello studio»</i>, <i>«che temperatura c'è in camera?»</i>, "
            "<i>«esegui la scena cinema»</i>\n"
            "🎙 <b>Oppure mandami un vocale</b> con lo stesso comando: lo trascrivo con "
            "Home Assistant e lo eseguo.\n\n"
            "🇬🇧 <i>I speak English too — just write to me in English.</i>"
        ),
        "en": (
            "🏠 <b>Hassgram</b> — Home Assistant control\n\n"
            "<b>Commands</b>\n"
            "/lights — rooms and lights, with on/off buttons\n"
            "/lights <i>room</i> — only that room's lights\n"
            "/whatson — everything that is on right now\n"
            "/on <i>name</i> — e.g. <code>/on study</code>, <code>/on house</code>\n"
            "/off <i>name</i> — e.g. <code>/off kitchen lights</code>\n"
            "/temperature [<i>room</i>] — e.g. <code>/temperature living room</code>\n"
            "<i>«house» means every room at once.</i>\n"
            "/state <i>name</i> — state of any entity\n"
            "/run [<i>name</i>] — scenes, scripts and automations; with no name I list them\n"
            "/language <i>it|en</i> — switch language\n\n"
            "You can also just talk to me: "
            "<i>«turn on the light in the study»</i>, <i>«how warm is it in the bedroom?»</i>, "
            "<i>«run the movie scene»</i>\n"
            "🎙 <b>Or send me a voice message</b> with the same command: I transcribe it "
            "with Home Assistant and run it.\n\n"
            "🇮🇹 <i>Parlo anche italiano — scrivimi pure in italiano.</i>"
        ),
    },
    # --- lights
    "no_light_found": {
        "it": "Nessuna luce trovata per «{query}».",
        "en": "No light found for “{query}”.",
    },
    "all_lights_off": {"it": "Tutte le luci sono spente. 🌙", "en": "All lights are off. 🌙"},
    "lights_on_title": {"it": "💡 <b>Luci accese</b>", "en": "💡 <b>Lights on</b>"},
    "lights_title": {"it": "Luci", "en": "Lights"},
    "lights_summary_title_one": {
        "it": "💡 <b>Luci</b> — 1 accesa su {total}",
        "en": "💡 <b>Lights</b> — 1 on out of {total}",
    },
    "lights_summary_title_many": {
        "it": "💡 <b>Luci</b> — {on} accese su {total}",
        "en": "💡 <b>Lights</b> — {on} on out of {total}",
    },
    "choose_room": {"it": "Scegli una stanza:", "en": "Pick a room:"},
    "room_counts_one": {"it": "{on}/{total} accesa", "en": "{on}/{total} on"},
    "room_counts_many": {"it": "{on}/{total} accese", "en": "{on}/{total} on"},
    "tap_to_toggle": {
        "it": "<i>Tocca una luce per invertirne lo stato.</i>",
        "en": "<i>Tap a light to toggle it.</i>",
    },
    "turn_all_on": {"it": "🟡 Accendi tutte", "en": "🟡 Turn all on"},
    "turn_all_off": {"it": "⚫ Spegni tutte", "en": "⚫ Turn all off"},
    "refresh": {"it": "🔄 Aggiorna", "en": "🔄 Refresh"},
    # --- switching
    "switch_usage": {
        "it": "Uso: <code>/{command} nome luce o stanza</code>",
        "en": "Usage: <code>/{command} light or room name</code>",
    },
    # The command names themselves are localised: the usage hint has to name the
    # command the user actually typed, and /accendi and /on are the same handler.
    "command_on": {"it": "accendi", "en": "on"},
    "command_off": {"it": "spegni", "en": "off"},
    "verb_on": {"it": "accendere", "en": "turn on"},
    "verb_off": {"it": "spegnere", "en": "turn off"},
    "nothing_to_switch": {
        "it": "Non ho trovato niente da {verb} per «{query}».",
        "en": "I found nothing to {verb} for “{query}”.",
    },
    "which_one": {"it": "Quale vuoi {verb}?", "en": "Which one do you want to {verb}?"},
    "all_button": {"it": "⚡️ Tutte ({count})", "en": "⚡️ All of them ({count})"},
    # Counts are inflected: "Salone (1 luci)" and "Bedroom (1 lights)" are both
    # wrong, and a room with a single light is common enough to be worth the
    # extra keys. Callers pick the form with i18n.plural().
    "whole_house_one": {"it": "Tutta la casa (1 luce)", "en": "The whole house (1 light)"},
    "whole_house_many": {"it": "Tutta la casa ({count} luci)", "en": "The whole house ({count} lights)"},
    "area_count_one": {"it": "{area} (1 luce)", "en": "{area} (1 light)"},
    "area_count_many": {"it": "{area} ({count} luci)", "en": "{area} ({count} lights)"},
    "n_entities_one": {"it": "1 entità", "en": "1 entity"},
    "n_entities_many": {"it": "{count} entità", "en": "{count} entities"},
    "result_on_one": {"it": "{icon} <b>{what}</b> accesa.", "en": "{icon} <b>{what}</b> turned on."},
    "result_on_many": {"it": "{icon} <b>{what}</b> accese.", "en": "{icon} <b>{what}</b> turned on."},
    "result_off_one": {"it": "{icon} <b>{what}</b> spenta.", "en": "{icon} <b>{what}</b> turned off."},
    "result_off_many": {"it": "{icon} <b>{what}</b> spente.", "en": "{icon} <b>{what}</b> turned off."},
    # --- running scenes, scripts and automations
    "command_run": {"it": "esegui", "en": "run"},
    "domain_scene": {"it": "Scene", "en": "Scenes"},
    "domain_script": {"it": "Script", "en": "Scripts"},
    "domain_automation": {"it": "Automazioni", "en": "Automations"},
    "automation_disabled": {"it": "disattivata", "en": "disabled"},
    "runnables_title": {
        "it": "▶️ <b>Cosa posso eseguire</b>",
        "en": "▶️ <b>What I can run</b>",
    },
    "run_tap_hint": {
        "it": "<i>Tocca un bottone per eseguirlo, oppure usa <code>/esegui nome</code>.</i>",
        "en": "<i>Tap a button to run it, or use <code>/run name</code>.</i>",
    },
    "no_runnables": {
        "it": "Non ho trovato scene, script o automazioni in Home Assistant.",
        "en": "I found no scenes, scripts or automations in Home Assistant.",
    },
    "nothing_to_run": {
        "it": "Non ho trovato niente da eseguire per «{query}».",
        "en": "I found nothing to run for “{query}”.",
    },
    "which_to_run": {"it": "Quale vuoi eseguire?", "en": "Which one do you want to run?"},
    # The confirmation says "started", not "done": Home Assistant answers as soon as
    # it has accepted the call, and a script can keep running for minutes afterwards.
    "result_run": {
        "it": "{icon} <b>{what}</b> avviata.",
        "en": "{icon} <b>{what}</b> started.",
    },
    "toast_run": {"it": "▶️ Avviata", "en": "▶️ Started"},
    # --- temperature
    "no_temp_sensors_for": {
        "it": "Non ho trovato sensori di temperatura per «{query}».",
        "en": "I found no temperature sensors for “{query}”.",
    },
    "no_temp_sensors": {
        "it": "Nessun sensore di temperatura trovato.",
        "en": "No temperature sensor found.",
    },
    "no_temp_sensors_in": {
        "it": "Nessun sensore di temperatura trovato in {area}.",
        "en": "No temperature sensor found in {area}.",
    },
    "temp_title": {"it": "🌡 <b>{title}</b>", "en": "🌡 <b>{title}</b>"},
    "temp_by_room_title": {
        "it": "🌡 <b>Temperature per stanza</b>",
        "en": "🌡 <b>Temperature by room</b>",
    },
    "target_label": {"it": "target {value}°C", "en": "target {value}°C"},
    "no_sensors": {"it": "Nessun sensore.", "en": "No sensors."},
    # --- generic entity state
    "state_usage": {
        "it": "Uso: <code>/stato nome entità</code>",
        "en": "Usage: <code>/state entity name</code>",
    },
    "no_entity_found": {
        "it": "Nessuna entità trovata per «{query}».",
        "en": "No entity found for “{query}”.",
    },
    "state_results_title": {
        "it": "🔎 <b>Risultati per «{query}»</b>",
        "en": "🔎 <b>Results for “{query}”</b>",
    },
    # --- callbacks
    "session_expired": {
        "it": "Sessione scaduta, rilancia il comando.",
        "en": "Session expired, please run the command again.",
    },
    "toast_on": {"it": "🟡 Acceso", "en": "🟡 On"},
    "toast_off": {"it": "⚫ Spento", "en": "⚫ Off"},
    "toast_refreshed": {"it": "Aggiornato", "en": "Refreshed"},
    # --- natural language fallbacks
    "not_understood_short": {
        "it": "Non ho capito. Prova con /luci, /temperatura oppure /help.",
        "en": "I did not get that. Try /lights, /temperature or /help.",
    },
    "not_understood": {
        "it": "Non ho capito «{text}».{hint}\nProva con /luci, /temperatura oppure /help.",
        "en": "I did not understand “{text}”.{hint}\nTry /lights, /temperature or /help.",
    },
    "voice_hint": {"it": " Ripeti pure il vocale.", "en": " Feel free to say it again."},
    # --- voice
    "stt_missing": {
        "it": (
            "🎙 Nessun motore speech-to-text configurato in Home Assistant.\n"
            "Aggiungi un'integrazione STT (Assist) oppure imposta <code>HA_STT_ENTITY</code> nel .env."
        ),
        "en": (
            "🎙 No speech-to-text engine is configured in Home Assistant.\n"
            "Add an STT integration (Assist) or set <code>HA_STT_ENTITY</code> in .env."
        ),
    },
    "voice_too_long": {
        "it": "🎙 Vocale troppo lungo (oltre {mb} MB, circa 5 minuti).",
        "en": "🎙 Voice message too long (over {mb} MB, about 5 minutes).",
    },
    "stt_failed": {
        "it": "🎙 Non sono riuscito a trascrivere il vocale.\n<i>{error}</i>",
        "en": "🎙 I could not transcribe the voice message.\n<i>{error}</i>",
    },
    "stt_empty": {
        "it": "🎙 Non ho sentito nulla di comprensibile, riprova.",
        "en": "🎙 I did not hear anything I could make out, try again.",
    },
    "transcribed": {"it": "🎙 <i>«{text}»</i>", "en": "🎙 <i>“{text}”</i>"},
    # --- errors
    # Detail lines for a HomeAssistantError, chosen by its ``kind``. They are
    # interpolated into "ha_down" / "stt_failed" as {error}, which is why they
    # carry no icon and no markup of their own.
    "ha_error_network": {
        "it": "Rete non raggiungibile: {detail}",
        "en": "Network unreachable: {detail}",
    },
    "ha_error_http": {
        "it": "Home Assistant ha risposto {status}: {detail}",
        "en": "Home Assistant answered {status}: {detail}",
    },
    "ha_error_stt": {
        "it": "Il motore di trascrizione ha rifiutato l'audio: {detail}",
        "en": "The transcription engine rejected the audio: {detail}",
    },
    "ha_error_generic": {"it": "{detail}", "en": "{detail}"},
    "ha_down": {
        "it": "⚠️ Home Assistant non risponde.\n<i>{error}</i>",
        "en": "⚠️ Home Assistant is not responding.\n<i>{error}</i>",
    },
    "generic_error": {
        "it": "⚠️ Qualcosa è andato storto, riprova.",
        "en": "⚠️ Something went wrong, please try again.",
    },
    "truncated": {"it": "<i>… elenco troncato.</i>", "en": "<i>… list truncated.</i>"},
    # --- rooms and states
    "no_area": {"it": "Senza stanza", "en": "No room"},
    "state_on": {"it": "accesa", "en": "on"},
    "state_off": {"it": "spenta", "en": "off"},
    "state_unavailable": {"it": "non disponibile", "en": "unavailable"},
    "state_unknown": {"it": "sconosciuto", "en": "unknown"},
    # --- language switching
    "language_name": {"it": "italiano", "en": "English"},
    "language_current": {
        "it": "🌍 Lingua attuale: <b>italiano</b>.\nScrivimi in inglese e passo all'inglese, oppure usa <code>/lingua en</code>.",
        "en": "🌍 Current language: <b>English</b>.\nWrite to me in Italian and I switch, or use <code>/language it</code>.",
    },
    "language_set": {
        "it": "🌍 Lingua impostata: <b>italiano</b>.",
        "en": "🌍 Language set to <b>English</b>.",
    },
    "language_unknown": {
        "it": "🌍 Lingue disponibili: <code>it</code>, <code>en</code>.",
        "en": "🌍 Available languages: <code>it</code>, <code>en</code>.",
    },
}


# The command menu Telegram shows next to the text box, per language. Kept here
# with the rest of the user-facing text, and kept as data rather than as
# MESSAGES entries because a menu is an ordered list of (name, description)
# pairs, not a lookup: the order is the order the user sees.
#
# The Italian and English menus list *different command names*, not translations
# of the same ones -- /luci and /lights are both registered, and the name the
# user picks is itself a language signal (see bot.COMMAND_LANG). Descriptions
# are plain text: Telegram renders no markup here and caps them at 256
# characters.
COMMAND_MENU: dict[str, tuple[tuple[str, str], ...]] = {
    "it": (
        ("luci", "Stanze e luci, con i bottoni"),
        ("accese", "Tutto quello che è acceso adesso"),
        ("accendi", "Accendi una luce, una stanza o casa"),
        ("spegni", "Spegni una luce, una stanza o casa"),
        ("esegui", "Esegui una scena, uno script o un'automazione"),
        ("temperatura", "Temperature e umidità, per stanza"),
        ("stato", "Stato di una qualsiasi entità"),
        ("lingua", "Cambia lingua: it o en"),
        ("aiuto", "Elenco dei comandi"),
    ),
    "en": (
        ("lights", "Rooms and lights, with buttons"),
        ("whatson", "Everything that is on right now"),
        ("on", "Turn on a light, a room or the house"),
        ("off", "Turn off a light, a room or the house"),
        ("run", "Run a scene, script or automation"),
        ("temperature", "Temperature and humidity, per room"),
        ("state", "State of any entity"),
        ("language", "Switch language: it or en"),
        ("help", "List of commands"),
    ),
}


def plural(key: str, count: int) -> str:
    """Pick the singular or plural variant of a catalogue key.

    Args:
        key: Base key, without the ``_one`` / ``_many`` suffix.
        count: How many things the message is about.

    Returns:
        ``"<key>_one"`` for exactly one, ``"<key>_many"`` otherwise -- English
        and Italian agree on treating zero as plural, so no third form is
        needed.
    """
    return f"{key}_one" if count == 1 else f"{key}_many"


def t(lang: str, key: str, **kwargs: object) -> str:
    """Render a catalogue entry in the requested language.

    Args:
        lang: ``"it"`` or ``"en"``. An unknown value silently falls back to
            :data:`DEFAULT_LANG` rather than raising: a bad language must never
            be the reason a user gets no answer.
        key: Catalogue key. Unlike the language, a missing key *does* raise --
            it is a programming error, and failing loudly at the call site is
            better than shipping a message that reads ``KeyError``.
        **kwargs: Substituted into the template with :meth:`str.format`.

    Returns:
        The formatted message.
    """
    entry = MESSAGES[key]
    return entry.get(lang, entry[DEFAULT_LANG]).format(**kwargs)


# --------------------------------------------------------------- detection

# Words that only occur in one of the two languages. Deliberately excludes
# tokens the languages share -- "in", "studio", room names, and "temperature",
# which is spelled identically in both and therefore only ever cancels itself
# out. A shared marker adds noise without ever discriminating; the intent
# patterns in RULES still match it, they just do not vote on the language.
MARKERS: dict[str, str] = {
    "it": (
        r"\b(accendi|accende|accendere|accesa|accese|acceso|attiva|attivare|spegni|spegnere|"
        r"spenta|spente|spento|disattiva|disattivare|luci|luce|lampada|lampade|temperatura|"
        r"gradi|umidita|caldo|freddo|stanza|stanze|casa|appartamento|ovunque|"
        r"tutta|tutte|tutti|tutto|della|dello|delle|dei|degli|nella|nello|quanti|quanto|"
        r"quale|quali|sono|adesso|dimmi|dammi|puoi|potresti|grazie|favore|che|cosa|"
        r"esegui|eseguire|lancia|lanciare|avvia|avviare|scena|scene|automazione|automazioni)\b"
    ),
    "en": (
        r"\b(turn|switch|lights|light|lamp|lamps|degrees|warm|cold|hot|humidity|"
        r"humid|room|rooms|house|home|apartment|flat|everything|everywhere|every|all|the|"
        r"what|whats|which|how|is|are|please|thanks|thank|you|can|could|would|tell|"
        r"anything|on|off|now|my|run|execute|trigger|launch|automation|automations)\b"
    ),
}


def detect(low: str, fallback: str = DEFAULT_LANG) -> str:
    """Guess which language a sentence is written in.

    Counts how many marker words of each language the sentence contains and
    returns the winner. Markers are chosen so that no word appears in both
    lists, so the two counts are genuinely independent evidence.

    Args:
        low: The sentence, already passed through :func:`entities.normalize`.
        fallback: What to return when the evidence is absent or tied. The
            caller passes the chat's current language, so a bare room name
            ("salone", with no marker at all) keeps the conversation in
            whatever language it was already in.

    Returns:
        ``"it"``, ``"en"``, or ``fallback``.

    Note:
        Short messages are legitimately ambiguous and will often fall back.
        That is the intended behaviour: switching language on one weak signal
        would be far more annoying than staying put, and the user can always
        be explicit with ``/language``.
    """
    scores = {lang: len(re.findall(pattern, low)) for lang, pattern in MARKERS.items()}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0 or list(scores.values()).count(scores[best]) > 1:
        return fallback
    return best


def normalize_lang(value: str | None, fallback: str = DEFAULT_LANG) -> str:
    """Coerce a user- or environment-supplied language tag to a supported one.

    Accepts anything that starts with a supported code, so ``it``, ``IT``,
    ``it-IT``, ``italiano``, ``en_US`` and ``english`` all work.

    Args:
        value: The tag to interpret; ``None`` and empty strings are allowed.
        fallback: Returned when the value matches no supported language.

    Returns:
        ``"it"``, ``"en"`` or ``fallback``.
    """
    if not value:
        return fallback
    low = value.strip().lower()
    for lang in LANGS:
        if low.startswith(lang):
            return lang
    if low.startswith(("ital", "ita")):
        return "it"
    if low.startswith(("eng", "ingl")):
        return "en"
    return fallback


# ----------------------------------------------------------------- grammar

# Words meaning "the whole house", per language. Matched exactly against the
# normalized query, never as a substring: a room named "Casetta" or "Homework"
# must not trigger the most destructive operation the bot can perform.
HOME_WORDS: dict[str, set[str]] = {
    "it": {"casa", "tutta casa", "tutta la casa", "tutte le stanze", "ovunque", "appartamento", "tutto"},
    "en": {"house", "the house", "whole house", "the whole house", "home", "everything",
           "everywhere", "all", "all rooms", "all the rooms", "every room", "apartment", "flat"},
}

# The canonical target a whole-house sentence is reduced to. Internal only:
# _is_home() accepts it in either language, so it never reaches the user.
HOME_TOKEN = "casa"

_IT = {
    "temperature": r"\b(temperatur\w*|caldo|freddo|umidit\w*|gradi)\b",
    "on": r"\b(accendi|accende|accendere|attiva|attivare)\b",
    # "esegui la scena cinema" must not be read as a turn-on command, so the run
    # verbs are their own rule, checked first. "attiva" stays with "on": it is far
    # more often said of a light than of a scene, and /esegui covers the rest.
    "run": r"\b(esegui|eseguire|esegue|lancia|lanciare|avvia|avviare|fai partire|manda in esecuzione)\b",
    "off": r"\b(spegni|spegnere|spenta|disattiva|disattivare)\b",
    "lights": r"\b(luci|luce|lampad\w*)\b",
    "on_state": r"\b(acces\w+)\b",
    "temp_strip": (
        r"\b(che|qual\w*|quanti|quanto|e|c\W?e|ci sono|mi dici|dimmi|dammi|la|il|lo|le|in|nel|"
        r"nella|del|della|di|a|ad|su|adesso|ora|gradi|temperatura|temperature|umidita|caldo|"
        r"freddo|fa|per favore)\b"
    ),
    "strip": (
        r"\b(accendi|accende|accendere|attiva|attivare|spegni|spegnere|disattiva|disattivare|"
        r"esegui|eseguire|esegue|lancia|lanciare|avvia|avviare|fai partire|manda in esecuzione|"
        r"la|le|lo|il|l|luce|luci|lampada|lampade|del|della|dello|dei|delle|di|in|nel|nella|"
        r"al|alla|allo|a|dell|nell|all|sull|per favore|grazie|mi|puoi|potresti|tutte|tutti|"
        r"stanza|adesso|ora|tutta|tutto)\b"
    ),
    "home_hint": r"\b(tutt\w*|casa|ovunque|appartamento)\b",
}

_EN = {
    "temperature": r"\b(temperature|temp|degrees|warm|cold|hot|humidity|humid|chilly|freezing)\b",
    "on": r"\bon\b",
    "off": r"\boff\b",
    # Checked before "off" and "on", or "run the good night scene" would match
    # neither and "trigger the wake up automation" would be read as a turn-on.
    "run": r"\b(run|execute|trigger|launch|activate|start|play)\b",
    "lights": r"\b(lights?|lamps?)\b",
    "on_state": r"\bon\b",
    "ask": r"\b(what|whats|which|how many|anything|is|are)\b",
    "temp_strip": (
        r"\b(how|what|whats|which|s|is|it|are|the|a|an|in|at|of|on|to|my|our|right|now|"
        r"currently|current|degrees|temperature|temp|humidity|humid|warm|cold|hot|chilly|"
        r"freezing|please|tell|me|there|do|does|you|know)\b"
    ),
    "strip": (
        r"\b(turn|turns|switch|switches|put|flip|toggle|on|off|up|down|the|a|an|all|every|"
        r"run|execute|trigger|launch|activate|start|play|"
        r"everything|everywhere|whole|house|home|apartment|flat|light|lights|lamp|lamps|"
        r"in|at|of|to|into|for|my|our|room|rooms|please|thanks|thank|you|can|could|would|"
        r"kindly|now|is|are|be|s|me)\b"
    ),
    "home_hint": r"\b(all|every|everything|everywhere|whole|house|home|apartment|flat)\b",
}

PATTERNS: dict[str, dict[str, str]] = {"it": _IT, "en": _EN}

# Ordered (intent, patterns-that-must-all-match) per language. The first rule
# that matches wins, so order encodes precedence.
#
# Italian checks the switching verbs before the "what is on" listing because
# "accendi"/"spegni" and "accese" are distinct words. English cannot do that:
# "on" is both the imperative particle and the state, so "which lights are on"
# would read as a turn-on command. The listing rule therefore comes first
# there, guarded by an interrogative.
RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "it": (
        ("temperature", (_IT["temperature"],)),
        ("run", (_IT["run"],)),
        ("on", (_IT["on"],)),
        ("off", (_IT["off"],)),
        ("lights_on", (_IT["lights"], _IT["on_state"])),
        ("lights", (_IT["lights"],)),
    ),
    "en": (
        ("temperature", (_EN["temperature"],)),
        ("lights_on", (_EN["ask"], _EN["on_state"])),
        ("run", (_EN["run"],)),
        ("off", (_EN["off"],)),
        ("on", (_EN["on"],)),
        ("lights", (_EN["lights"],)),
    ),
}


def is_home(query: str) -> bool:
    """Report whether a query means "the whole house", in either language.

    Both vocabularies are accepted regardless of the conversation's language:
    the word has already been typed, and refusing to understand "everything"
    because the chat is in Italian would be pedantry.

    Args:
        query: The query, already normalized.

    Returns:
        ``True`` for an exact match against :data:`HOME_WORDS`.
    """
    return any(query in words for words in HOME_WORDS.values())


def strip_filler(low: str, lang: str) -> str:
    """Reduce a sentence to the thing it talks about.

    Removes verbs, articles, prepositions, politeness and filler, leaving the
    target for :func:`entities.search`: "accendi la luce dello studio" becomes
    "studio", "turn on the light in the study" becomes "study".

    Quantifiers are stripped along with everything else, which would silently
    turn "accendi tutto" and "turn everything on" into empty queries. When the
    result is empty but the original sentence contained a whole-house word,
    :data:`HOME_TOKEN` is returned instead, so the sentence keeps the meaning it
    obviously had.

    Args:
        low: The sentence, already normalized.
        lang: Which vocabulary to strip with.

    Returns:
        The residual target, :data:`HOME_TOKEN` for a whole-house sentence, or
        the empty string when nothing usable is left -- which callers read as
        "show the overview" rather than as an error.

    Note:
        Never add a word to a strip list that could be part of a room or entity
        name: it would be deleted from every query in that language.
    """
    p = PATTERNS.get(lang, _IT)
    cleaned = re.sub(p["strip"], " ", low)
    target = re.sub(r"\s+", " ", re.sub(r"[?!.,;:]", " ", cleaned)).strip()
    if not target and re.search(p["home_hint"], low):
        return HOME_TOKEN
    return target


def parse(low: str, lang: str) -> tuple[str | None, str]:
    """Turn a normalized sentence into an intent and a target.

    Args:
        low: The sentence, already normalized.
        lang: The language to parse with, from :func:`detect`.

    Returns:
        ``(intent, target)`` where intent is one of ``"temperature"``,
        ``"on"``, ``"off"``, ``"lights_on"``, ``"lights"``, ``"run"``, or
        ``None`` when no rule matched. The target is the residual text -- a room,
        an entity name, :data:`HOME_TOKEN`, or the empty string.

    Examples:
        >>> parse("accendi la luce dello studio", "it")
        ('on', 'studio')
        >>> parse("turn on the light in the study", "en")
        ('on', 'study')
        >>> parse("quanti gradi in salone", "it")
        ('temperature', 'salone')
        >>> parse("how warm is it in the bedroom", "en")
        ('temperature', 'bedroom')
        >>> parse("spegni tutto", "it")
        ('off', 'casa')
        >>> parse("turn everything off", "en")
        ('off', 'casa')
        >>> parse("which lights are on", "en")
        ('lights_on', '')
        >>> parse("esegui la scena cinema", "it")
        ('run', 'scena cinema')
        >>> parse("run the good night scene", "en")
        ('run', 'good night scene')
    """
    p = PATTERNS.get(lang, _IT)
    for intent, patterns in RULES.get(lang, RULES["it"]):
        if all(re.search(pattern, low) for pattern in patterns):
            if intent == "temperature":
                target = re.sub(p["temp_strip"], " ", low)
                return intent, re.sub(r"\s+", " ", re.sub(r"[?!.,]", " ", target)).strip()
            if intent == "lights_on":
                return intent, ""
            return intent, strip_filler(low, lang)
    return None, ""
