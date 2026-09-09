"""Values shared by every module of the Telegram front end.

They live here rather than in :mod:`bot` because the presentation layer
(:mod:`views`), the callback router (:mod:`callbacks`) and the voice handler
(:mod:`voice`) all need them, and importing them from :mod:`bot` would close an
import cycle. Nothing here does any work: these are the limits Telegram imposes
and the facts about Home Assistant domains, in one place so that changing one is
a one-line change.
"""

from __future__ import annotations

LIGHT_DOMAINS = ("light", "switch")

# Domains the bot can *execute*, as opposed to switch. They are kept out of
# LIGHT_DOMAINS on purpose: a script is not a lamp, and "spegni casa" must never
# reach one. The service each domain is run with differs -- calling
# ``automation.turn_on`` would only *enable* the automation, not run it, which is
# the single most confusing thing this feature could do.
#
# Scenes are deliberately absent: a scene is a set of states to apply, closer to
# the switching commands than to the executing ones, and listing them here put
# them in the same menu as the automations without behaving like them.
RUN_SERVICES: dict[str, str] = {
    "script": "turn_on",
    "automation": "trigger",
}
RUN_DOMAINS: tuple[str, ...] = tuple(RUN_SERVICES)
RUN_ICONS: dict[str, str] = {"script": "\U0001f4dc", "automation": "\u2699\ufe0f"}
RUN_ICON_DEFAULT = "\u25b6\ufe0f"  # a domain added to RUN_SERVICES without an icon still gets a button

# How often the list of scripts and automations is re-read, in seconds.
# Unlike lights, this list is a *catalogue*: it only changes when the user edits
# their Home Assistant configuration, so it is read once at startup and then
# refreshed on a slow cycle instead of on every /esegui.
RUNNABLES_REFRESH_SECONDS = 300.0

MAX_BUTTONS = 24

# Most messages one /esegui listing may occupy. Telegram rate-limits a chat at
# roughly one message per second and reacts badly to a burst, so a pathological
# installation is cut off here rather than being blasted at the user. With
# MAX_BUTTONS entities per page this is a few hundred entities: far past what
# anyone browses by scrolling, and /esegui <nome> is the answer beyond it.
MAX_RUN_PAGES = 20
MAX_VOICE_BYTES = 5 * 1024 * 1024  # ~5 minutes of ogg/opus: past that it is almost certainly not a command
MAX_MESSAGE_CHARS = 4000  # Telegram stops at 4096: leave room for the truncation notice
MAX_TOKENS = 2000  # keyboards stay usable without letting the map grow forever
MAX_CHAT_LANGS = 500  # remembered languages: an LRU, for the same reason as the tokens
