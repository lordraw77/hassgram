"""Telegram front end for Home Assistant.

Hassgram lets a small, trusted set of Telegram chats drive a Home Assistant
installation by typing commands, speaking them, or tapping inline buttons.

This module owns the bot's state and its command handlers: it holds the Home
Assistant client, the allow-list, the per-chat language and the catalogue of
runnables, and it wires everything together in :func:`main`. The rest of the
Telegram front end is split off by concern -- rendering in :mod:`views`, inline
buttons in :mod:`callbacks`, voice notes in :mod:`voice`, shared limits and
domain constants in :mod:`constants`. Domain logic lives in :mod:`entities`, the
message catalogue and the two grammars in :mod:`i18n`, and all HTTP traffic in
:mod:`ha_client`.

Those three handler modules take the bot as their first argument rather than
being methods on it, which is what keeps the dependency one-way: they import
:mod:`bot` for typing only, and :func:`main` binds them to the live instance
with :func:`functools.partial`.

Request flow
------------

Three entry points converge on the same execution path::

    /accendi studio  ---> CommandHandler ------+
    "accendi lo studio" -> on_text --+         |
    voice note -> voice.on_voice --+--> _dispatch_text --> _switch --> _apply
                        (STT)                                             |
    button tap -> callbacks.on_callback -> callbacks.handle --------------+

Every switching path ends in :meth:`HassBot._call_on_ids`, which groups entity
ids by domain and calls one Home Assistant service per domain. The executing
path -- ``/esegui``, "esegui lo script buonanotte", a ``run:`` button -- ends
in :meth:`HassBot._run_ids` instead, which does the same grouping but picks the
service per domain: a script is started with ``turn_on``, an automation with
``trigger``.

Cross-cutting rules
-------------------

Authorisation
    Every handler starts with :meth:`HassBot.guard` (or, for callbacks,
    :meth:`HassBot.authorized`). ``TELEGRAM_CHAT_ID`` holds the allow-list; an
    empty list means the bot answers anyone, which is only reasonable while
    testing.

Message size
    Telegram rejects messages longer than 4096 characters, and a large house
    easily exceeds that. :func:`views.clip` truncates on a line boundary -- every
    line the bot emits is self-contained HTML, so cutting between lines leaves
    the markup balanced -- and every outbound message goes through
    :meth:`HassBot.reply`, which applies it.

Callback payloads
    ``callback_data`` is capped at 64 bytes by Telegram, far too small for a
    list of entity ids. Buttons therefore carry a 12-character token, resolved
    against the in-memory LRU behind :func:`views.tok` and :func:`views.untok`.

Startup state
    Two things are read once at startup and then kept: the speech-to-text engine
    (:meth:`HassBot.discover_stt`) and the catalogue of scripts and automations
    (:meth:`HassBot.refresh_runnables`), the latter refreshed every
    ``RUNNABLES_REFRESH_SECONDS`` by a background task. ``/esegui`` is therefore
    answered from memory: the catalogue reflects the user's Home Assistant
    configuration, which changes when they edit it, not from minute to minute.
    The command menu Telegram shows next to the text box is published at the same
    point, from :data:`i18n.COMMAND_MENU`.

Failure handling
    Handlers do not defend against Home Assistant being down. :func:`on_error`
    is registered as the global error handler and turns any unhandled
    exception into a message to the user, so a failure is never silent.

Localisation
    The bot answers in Italian or English, following the chat (see
    :meth:`HassBot.resolve_lang`). No user-facing string is written in this
    module: every one of them comes from :func:`i18n.t`, including the failure
    lines built by :func:`views.ha_error_text` out of a
    :class:`ha_client.HomeAssistantError`.
    Code, comments and docstrings are English.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections import OrderedDict
import functools
from typing import Any

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import callbacks
import entities as ent
import i18n
import views
import voice
from constants import (
    LIGHT_DOMAINS,
    MAX_BUTTONS,
    MAX_CHAT_LANGS,
    RUN_DOMAINS,
    RUN_ICON_DEFAULT,
    RUN_ICONS,
    RUN_SERVICES,
    RUNNABLES_REFRESH_SECONDS,
)
from i18n import t
from ha_client import HomeAssistantClient, HomeAssistantError
from views import clip, esc, ha_error_text

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hassgram")

# Command names that identify a language on their own, used to follow the user
# when they type /lights instead of /luci. Names shared by both languages
# (start, help, on, off, temp) are absent on purpose: switching a conversation
# to English because someone typed /on would be worse than doing nothing.
COMMAND_LANG: dict[str, str] = {
    "luci": "it", "accese": "it", "accendi": "it", "spegni": "it",
    "temperatura": "it", "stato": "it", "aiuto": "it", "lingua": "it", "esegui": "it",
    "lights": "en", "whatson": "en", "state": "en", "language": "en", "temperature": "en",
    "run": "en",
}


class HassBot:
    """Stateful holder for the bot's handlers.

    One instance is created in :func:`main`, stored in ``Application.bot_data`` and
    shared by every handler. It holds the Home Assistant client, the chat
    allow-list and the speech-to-text configuration; it deliberately holds no
    per-conversation state, so restarting the process loses nothing but the
    callback token store in :mod:`views`.

    Method naming follows a strict convention:

    ``cmd_*``
        Bound to a Telegram command. Calls :meth:`guard` first, then delegates.
    ``on_*``
        Bound to a non-command update. Also guards. Only :meth:`on_text` lives here;
        the voice and callback entry points are :func:`voice.on_voice` and
        :func:`callbacks.on_callback`, which take the bot as their first argument.
    ``_*``
        Internal. Assumes authorisation has already been checked, which is what
        lets ``/accese`` and the sentence "quali luci sono accese" share
        :meth:`_lights_on` without checking twice.

    Attributes:
        ha: The shared :class:`ha_client.HomeAssistantClient`.
        allowed_chats: Chat ids permitted to use the bot. An empty set disables the
            check entirely and lets anyone in.
        stt_entity: Entity id of the speech-to-text engine, or ``None`` when none
            is configured or discovered; voice messages are then declined with an
            explanation.
        stt_languages: BCP-47 tag per language, e.g. ``{"it": "it-IT", "en": "en-US"}``.
            The chat's current language picks the entry (see :func:`voice.on_voice`).
        default_lang: Language for a chat that has not said anything recognisable yet.
        runnables_refresh: Seconds between two reads of the runnable catalogue;
            zero or less disables the background cycle.
        chat_lang: Remembered language per chat id, an LRU capped at
            :data:`MAX_CHAT_LANGS`. Evicting an entry costs nothing: the chat simply
            falls back to :attr:`default_lang` until it writes something the detector
            recognises again.
    """
    def __init__(
        self,
        ha: HomeAssistantClient,
        allowed_chats: set[int],
        stt_entity: str | None = None,
        stt_languages: dict[str, str] | None = None,
        default_lang: str = i18n.DEFAULT_LANG,
        runnables_refresh: float = RUNNABLES_REFRESH_SECONDS,
    ) -> None:
        """Wire the bot to its dependencies.

        Args:
            ha: Home Assistant client, already configured. Its lifetime is managed by
                the caller: ``post_shutdown`` closes it.
            allowed_chats: Chat ids allowed to use the bot. Pass an empty set to
                disable authorisation -- only sensible while testing, since the token
                gives full control of the house to anyone who finds the bot.
            stt_entity: Speech-to-text entity to use. ``None`` asks
                :meth:`discover_stt` to pick one at startup.
            stt_languages: Language tag per supported language. The provider must
                advertise the tag or voice messages in that language are rejected.
            default_lang: Language used until a chat reveals its own.
            runnables_refresh: Seconds between two reads of the runnable catalogue.
                Zero or less disables the cycle, leaving the list to be read once at
                startup and then on demand.
        """
        self.ha = ha
        self.allowed_chats = allowed_chats
        self.stt_entity = stt_entity
        self.stt_languages = stt_languages or {"it": "it-IT", "en": "en-US"}
        self.default_lang = i18n.normalize_lang(default_lang)
        self.chat_lang: OrderedDict[int, str] = OrderedDict()
        self.runnables_refresh = runnables_refresh
        self._runnables_cache: list[dict[str, Any]] | None = None
        self._refresh_task: asyncio.Task | None = None

    async def discover_stt(self) -> None:
        """Pick a speech-to-text engine when one was not configured explicitly.

        Called once from ``post_init``. An explicit ``HA_STT_ENTITY`` always wins;
        otherwise the first ``stt.`` entity Home Assistant exposes is used, which on a
        typical installation is the only one.

        Finding nothing is not an error: text commands keep working and voice messages
        are answered with an explanation, so a house without an STT integration still
        gets a fully functional bot. The outcome is logged either way, because "voice
        messages do nothing" is otherwise hard to diagnose.

        Raises:
            ha_client.HomeAssistantError: If the state snapshot cannot be read. At
                startup this is fatal by design -- the process should not come up
                pretending to work against an unreachable instance.
        """
        if self.stt_entity:
            return
        available = await self.ha.stt_entities()
        self.stt_entity = available[0] if available else None
        if self.stt_entity:
            log.info("Speech-to-text: %s (languages: %s)", self.stt_entity, self.stt_languages)
        else:
            log.warning("No stt. entity in Home Assistant: voice messages will not be transcribed.")

    # ------------------------------------------------- the runnable catalogue
    async def refresh_runnables(self) -> list[dict[str, Any]]:
        """Re-read the scripts and automations and replace the cache.

        Called once from ``post_init`` and then by :meth:`_refresh_loop`.

        Returns:
            The fresh catalogue, also stored in the cache.

        Raises:
            ha_client.HomeAssistantError: If the snapshot cannot be read. The caller
                decides what that means: fatal at startup, merely logged inside the
                refresh loop.
        """
        found = self._runnables(await self.ha.states())
        self._runnables_cache = found
        counts = ", ".join(
            f"{len([s for s in found if s['entity_id'].startswith(f'{d}.')])} {d}" for d in RUN_DOMAINS
        )
        log.info("Runnable catalogue: %s", counts)
        return found

    async def runnables(self) -> list[dict[str, Any]]:
        """Return the runnable catalogue, reading it if the cache is empty.

        ``/esegui`` goes through here rather than through :meth:`snapshot`, so the
        menu is built from the cached catalogue instead of a live read: the list of
        scripts and automations changes when the user edits their Home Assistant
        configuration, not from one minute to the next.

        Returns:
            The cached catalogue. The cache is only ``None`` when the startup read
            failed and the refresh cycle has not yet succeeded -- an unreachable
            instance at boot, typically -- in which case one read is attempted here
            so the command still works as soon as Home Assistant comes back.

        Raises:
            ha_client.HomeAssistantError: From the on-demand read only. A populated
                cache never raises, which is the point: ``/esegui`` keeps answering
                from the last known catalogue while Home Assistant is briefly down.

        Note:
            An automation enabled or disabled outside the bot keeps its old marking
            in the listing until the next refresh. Triggering it by hand works
            regardless, so the staleness is cosmetic.
        """
        if self._runnables_cache is None:
            return await self.refresh_runnables()
        return self._runnables_cache

    def start_refreshing(self) -> None:
        """Start the background task that keeps the catalogue current.

        Called from ``post_init``, after the first read, so a house whose Home
        Assistant is reachable at boot never waits on this task for its first
        ``/esegui``. Does nothing when :attr:`runnables_refresh` is not positive, or
        when a task is already running -- calling it twice is harmless.
        """
        if self.runnables_refresh <= 0 or (self._refresh_task and not self._refresh_task.done()):
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop_refreshing(self) -> None:
        """Cancel the refresh task and wait for it to finish.

        Called from ``post_shutdown``. Awaiting the cancellation rather than merely
        requesting it is what keeps the shutdown quiet: a task still pending when the
        loop closes is reported as "Task was destroyed but it is pending".
        """
        task, self._refresh_task = self._refresh_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _refresh_loop(self) -> None:
        """Re-read the catalogue every :attr:`runnables_refresh` seconds, forever.

        Sleeps first, since ``post_init`` has just read the catalogue.

        A failed read is logged and the cycle continues with the previous catalogue
        still in place: Home Assistant restarting must not leave the bot unable to
        list its scripts, and there is nothing a user could do about it anyway. Any
        other exception is logged with its traceback for the same reason -- a
        background task that dies silently is the worst possible outcome, because
        the catalogue would then quietly freeze for the lifetime of the process.
        """
        while True:
            await asyncio.sleep(self.runnables_refresh)
            try:
                await self.refresh_runnables()
            except HomeAssistantError as exc:
                log.warning("Could not refresh the runnable catalogue: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- see the docstring: never die silently
                log.exception("Unexpected error while refreshing the runnable catalogue")

    # ------------------------------------------------------------------ utils
    def authorized(self, update: Update) -> bool:
        """Check whether an update comes from a permitted chat.

        Args:
            update: The incoming update.

        Returns:
            ``True`` if the allow-list is empty (authorisation disabled) or the chat id
            is on it. An update with no chat -- which the API allows -- is refused.

        Note:
            This is the only access control in the bot. The Home Assistant token is
            all-powerful, so this check is the sole thing standing between a stranger
            who guessed the bot's name and the user's house.
        """
        chat = update.effective_chat
        return not self.allowed_chats or (chat is not None and chat.id in self.allowed_chats)

    async def guard(self, update: Update) -> bool:
        """Authorise a message-bearing update, replying if it is refused.

        The standard first line of every ``cmd_*`` and ``on_*`` handler::

            if not await self.guard(update):
                return

        Args:
            update: The incoming update.

        Returns:
            ``True`` when the handler may proceed. When it returns ``False`` the
            refusal has already been logged with the offending chat id -- useful for
            noticing that the bot has been found -- and answered, so the caller only
            has to return.
        """
        if self.authorized(update):
            return True
        log.warning("Access denied for chat %s", update.effective_chat.id if update.effective_chat else "?")
        lang = self.lang_of(update)
        if update.effective_message:
            await self.reply(update, t(lang, "unauthorized"), lang)
        return False

    async def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Fetch the two views of Home Assistant that nearly every command needs.

        Returns:
            A ``(states, areas)`` tuple: the full state list and the
            ``entity_id -> area`` mapping. Both come from caches in the client, so
            calling this repeatedly within one command is cheap.

        Note:
            Fetched sequentially rather than with :func:`asyncio.gather`, because the
            areas mapping is rendered once per process and everything after the first
            call is a dictionary lookup. There is no concurrency to win.
        """
        return await self.ha.states(), await self.ha.areas()

    @staticmethod
    def _lights(
        states: list[dict[str, Any]],
        areas: dict[str, str] | None = None,
        area: str | None = None,
        domains: tuple[str, ...] = ("light",),
    ) -> list[dict[str, Any]]:
        """Select the lighting entities out of a full state snapshot.

        The one place the "what counts as a light" question is answered, so the
        browser, the listings, the bulk actions and the callbacks cannot drift
        apart on it.

        Args:
            states: A full snapshot from :meth:`snapshot`.
            areas: Mapping ``entity_id -> area name``. Required only when ``area``
                is given.
            area: Restrict to one room, by exact area name.
            domains: Which domains count. The default is ``light`` alone, which is
                what every *browsing* caller wants: a smart plug listed among the
                lamps is confusing. :meth:`_switch` passes
                :data:`LIGHT_DOMAINS` so a plug can still be named explicitly.

        Returns:
            The matching entities, in snapshot order.
        """
        prefixes = tuple(f"{d}." for d in domains)
        return [
            s
            for s in states
            if s["entity_id"].startswith(prefixes)
            and (area is None or (areas or {}).get(s["entity_id"]) == area)
        ]

    @staticmethod
    def _runnables(
        states: list[dict[str, Any]],
        domains: tuple[str, ...] = RUN_DOMAINS,
    ) -> list[dict[str, Any]]:
        """Select the executable entities -- scripts and automations.

        The counterpart of :meth:`_lights` for the ``/esegui`` side of the bot, and
        the one place the "what can be run" question is answered, so the listing,
        the search and the callbacks cannot drift apart on it.

        Args:
            states: A full snapshot from :meth:`snapshot`.
            domains: Which domains count. Defaults to every key of
                :data:`RUN_SERVICES`; a caller can narrow it to, say, ``("script",)``.

        Returns:
            The matching entities sorted by domain and then by friendly name, so a
            listing groups the scripts together and the order does not follow the
            arbitrary order of a Home Assistant snapshot.
        """
        prefixes = tuple(f"{d}." for d in domains)
        found = [s for s in states if s["entity_id"].startswith(prefixes)]
        order = {d: i for i, d in enumerate(RUN_DOMAINS)}
        return sorted(found, key=lambda s: (order.get(s["entity_id"].split(".")[0], 99), ent.friendly_name(s)))

    @staticmethod
    def _temp_sensors(
        states: list[dict[str, Any]],
        areas: dict[str, str] | None = None,
        area: str | None = None,
    ) -> list[dict[str, Any]]:
        """Select the temperature and humidity sensors out of a state snapshot.

        Sensors are picked by ``device_class`` rather than by name, so the answer
        does not depend on how the user named them, and by domain as well: a
        ``binary_sensor`` or a ``number`` helper can carry
        ``device_class: temperature`` without being a reading anyone wants to see
        in a room summary.

        Args:
            states: A full snapshot from :meth:`snapshot`.
            areas: Mapping ``entity_id -> area name``. Required only when ``area``
                is given.
            area: Restrict to one room, by exact area name.

        Returns:
            The matching entities, in snapshot order. ``climate`` entities are
            *not* included: they carry their reading in an attribute rather than
            in their state and are added separately, and only for a specific room
            (see :meth:`_temperature`).
        """
        return [
            s
            for s in states
            if s["entity_id"].startswith("sensor.")
            and s.get("attributes", {}).get("device_class") in ("temperature", "humidity")
            and (area is None or (areas or {}).get(s["entity_id"]) == area)
        ]

    def lang_of(self, update: Update) -> str:
        """Return the language currently in use for an update's chat.

        Reads the remembered preference, which is set whenever the user writes
        something the detector recognises or runs an unambiguous command. Used
        by everything that has no text of its own to go on: button taps, voice
        transcription, error messages.

        Args:
            update: The update whose chat to look up.

        Returns:
            The chat's language, or the process default for a chat that has not
            said anything recognisable yet.
        """
        chat = getattr(update, "effective_chat", None)
        return self.chat_lang.get(chat.id, self.default_lang) if chat else self.default_lang

    def _remember_lang(self, update: Update, lang: str) -> None:
        """Record the language a chat is speaking, keeping the store bounded.

        The only writer of :attr:`chat_lang`. It is an LRU rather than a plain
        dictionary for the same reason :func:`views.tok` is: the bot runs for months
        under systemd, and with an empty allow-list any stranger who finds it can
        otherwise add an entry per chat, forever.

        Args:
            update: The update whose chat to record. One without a chat is ignored.
            lang: The language to remember.
        """
        chat = getattr(update, "effective_chat", None)
        if chat is None:
            return
        self.chat_lang[chat.id] = lang
        self.chat_lang.move_to_end(chat.id)
        while len(self.chat_lang) > MAX_CHAT_LANGS:
            self.chat_lang.popitem(last=False)

    def resolve_lang(self, update: Update, text: str | None = None) -> str:
        """Work out which language to answer an update in, and remember it.

        Two sources of evidence, in order:

        1. **The command name**, when the message is one. ``/luci`` is Italian,
           ``/lights`` is English, and names the two languages share carry no
           signal (see :data:`COMMAND_LANG`).
        2. **The words themselves**, via :func:`i18n.detect`.

        Either way the answer becomes the chat's preference, so a conversation
        that switches to English stays in English for the button taps and voice
        messages that follow -- neither of which carries any language of its own.

        Args:
            update: The update being handled.
            text: Text to detect from, when the caller already has it (a voice
                transcription, for instance). Defaults to the message text.

        Returns:
            The language to answer in. Ambiguous input keeps the chat where it
            already was rather than flipping it on weak evidence.
        """
        current = self.lang_of(update)
        raw = text if text is not None else getattr(getattr(update, "effective_message", None), "text", None)
        lang = current
        if raw:
            stripped = raw.strip()
            if stripped.startswith("/"):
                name = re.split(r"[\s@]", stripped[1:], maxsplit=1)[0].lower()
                lang = COMMAND_LANG.get(name, current)
            else:
                lang = i18n.detect(ent.normalize(stripped), fallback=current)
        self._remember_lang(update, lang)
        return lang

    async def reply(self, update: Update, text: str, lang: str = i18n.DEFAULT_LANG, **kwargs: Any) -> None:
        """Send a message to the chat an update came from.

        The single exit point towards Telegram, which is what makes two guarantees
        hold everywhere instead of per call site: HTML parse mode is the default, and
        the body is always run through :func:`views.clip` so an oversized list degrades into
        a truncated one rather than a rejected message.

        Args:
            update: The update to reply to.
            text: Message body, already localised by the caller through
                :func:`i18n.t`. May contain Telegram-flavoured HTML; any
                interpolated value that did not originate here must be passed
                through :func:`views.esc`.
            lang: Language, used for the truncation notice.
            **kwargs: Forwarded to ``reply_text`` -- typically ``reply_markup`` for an
                inline keyboard. ``parse_mode`` can be overridden if a caller ever
                needs plain text.
        """
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        await update.effective_message.reply_text(clip(text, lang), **kwargs)

    # -------------------------------------------------------------- commands
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/start``, ``/help`` and ``/aiuto``: print the command reference.

        The text doubles as the bot's documentation for users who never read the
        README, so it lists every command with an example and mentions both the
        natural-language and the voice entry points.
        """
        if not await self.guard(update):
            return
        lang = self.resolve_lang(update)
        await self.reply(update, t(lang, "help"), lang)

    async def cmd_language(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/lingua`` and ``/language``: show or set the chat's language.

        With no argument it reports the current language and explains that
        writing in the other one is enough to switch. With ``it`` or ``en`` --
        or anything :func:`i18n.normalize_lang` recognises, such as ``en-GB`` or
        ``italiano`` -- it pins the chat to that language.

        Pinning matters for input that carries no language of its own: button
        labels, and above all the language handed to the speech-to-text engine.
        """
        if not await self.guard(update):
            return
        arg = " ".join(ctx.args or []).strip()
        if not arg:
            lang = self.resolve_lang(update)
            await self.reply(update, t(lang, "language_current"), lang)
            return
        chosen = i18n.normalize_lang(arg, fallback="")
        if not chosen:
            lang = self.lang_of(update)
            await self.reply(update, t(lang, "language_unknown"), lang)
            return
        self._remember_lang(update, chosen)
        await self.reply(update, t(chosen, "language_set"), chosen)

    async def cmd_lights(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/luci [query]``: browse lights, by room or by name.

        Three behaviours depending on the argument:

        * No argument, or a word meaning "the whole house": a per-room summary of how
          many lights are on, plus a keyboard of rooms to drill into.
        * A room or entity name: the matching lights, each as a toggle button.
        * No match: a message saying so, rather than an empty keyboard.

        Only the ``light`` domain is searched here. Switches are reachable through
        ``/accendi`` and ``/spegni`` but are kept out of the light *browser*, where a
        smart plug listed among the lamps would be confusing.
        """
        if not await self.guard(update):
            return
        await self._lights_browse(update, " ".join(ctx.args or []).strip(), self.resolve_lang(update))

    async def _lights_browse(self, update: Update, query: str, lang: str, overview_on_miss: bool = False) -> None:
        """Show the light browser: an overview, or the lights matching a query.

        Shared by ``/luci`` and by sentences such as "fammi vedere le luci", which
        is why authorisation is not checked here.

        Args:
            update: The update to reply to.
            query: A room, an entity name, a whole-house word, or the empty string.
            lang: Language to answer in.
            overview_on_miss: What to do when the query matches nothing. A command
                says so explicitly -- the user typed a name and deserves to know it
                was not found. A sentence falls back to the overview instead:
                "accendi le luci" leaves a target the parser could not reduce to
                anything useful, and answering "no light called X" would blame the
                user for the parser's residue.
        """
        states, areas = await self.snapshot()
        lights = self._lights(states)
        found = (
            ent.search(query, lights, areas, domains=("light",), limit=MAX_BUTTONS)
            if query and not self._is_home(query)
            else []
        )
        if found:
            await self.reply(
                update,
                views.lights_text(query, found, lang),
                lang,
                reply_markup=views.lights_keyboard(found, lang),
            )
            return
        if query and not self._is_home(query) and not overview_on_miss:
            await self.reply(update, t(lang, "no_light_found", query=esc(query)), lang)
            return
        await self.reply(
            update,
            views.areas_summary(lights, areas, lang),
            lang,
            reply_markup=views.areas_keyboard(lights, areas),
        )

    async def cmd_on(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/accendi <name>`` and ``/on <name>``: turn something on.

        Thin wrapper over :meth:`_switch`, which does the target resolution. The
        argument may name one light, a room, or the whole house.
        """
        if not await self.guard(update):
            return
        await self._switch(update, " ".join(ctx.args or []), turn_on=True, lang=self.resolve_lang(update))

    async def cmd_off(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/spegni <name>`` and ``/off <name>``: turn something off.

        Thin wrapper over :meth:`_switch`; see :meth:`cmd_on`.
        """
        if not await self.guard(update):
            return
        await self._switch(update, " ".join(ctx.args or []), turn_on=False, lang=self.resolve_lang(update))

    async def cmd_on_now(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/accese``: list everything currently on.

        Guards, then delegates to :meth:`_lights_on`, which is shared with the
        natural-language path.
        """
        if not await self.guard(update):
            return
        await self._lights_on(update, self.resolve_lang(update))

    async def _lights_on(self, update: Update, lang: str) -> None:
        """List every light that is currently on, grouped by room.

        Shared by the ``/accese`` command and by sentences such as "quali luci sono
        accese", which is why authorisation is *not* checked here: both callers have
        already done it.

        The reply carries a keyboard of the lights that are on, capped at
        :data:`MAX_BUTTONS`, so the usual follow-up ("turn that one off") is a tap
        rather than another command. The list itself is not capped -- it is text, and
        :func:`views.clip` handles the extreme case.

        Args:
            update: The update to reply to.
            lang: Language to answer in.
        """
        states, areas = await self.snapshot()
        on = [s for s in self._lights(states) if ent.is_on(s)]
        if not on:
            await self.reply(update, t(lang, "all_lights_off"), lang)
            return
        lines = [t(lang, "lights_on_title"), ""]
        for area, group in ent.group_by_area(on, areas).items():
            lines.append(f"<b>{esc(views.area_name(area, lang))}</b>")
            lines += [f"  🟡 {esc(ent.friendly_name(s))}" for s in group]
            lines.append("")
        await self.reply(
            update,
            "\n".join(lines).strip(),
            lang,
            reply_markup=views.lights_keyboard(on[:MAX_BUTTONS], lang),
        )

    async def cmd_temperature(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/temperatura [room]``: report temperature and humidity.

        Guards, then delegates to :meth:`_temperature`, which is shared with the
        natural-language path.
        """
        if not await self.guard(update):
            return
        await self._temperature(update, " ".join(ctx.args or []).strip(), self.resolve_lang(update))

    async def cmd_state(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/stato <query>``: inspect any entity, in any domain.

        The generic escape hatch of the bot: unlike every other command it does not
        filter by domain, so sensors, plugs, thermostats and media players are all
        reachable. Each result shows the display name, the raw state with its unit, and
        the entity id in a code span -- the id being what the user needs when writing
        an automation or reporting a problem.

        Read-only by design: no buttons are attached, because "toggle whatever this is"
        is not a safe offer to make for an arbitrary domain.
        """
        if not await self.guard(update):
            return
        lang = self.resolve_lang(update)
        query = " ".join(ctx.args or []).strip()
        if not query:
            await self.reply(update, t(lang, "state_usage"), lang)
            return
        states, areas = await self.snapshot()
        found = ent.search(query, states, areas, limit=10)
        if not found:
            await self.reply(update, t(lang, "no_entity_found", query=esc(query)), lang)
            return
        lines = [t(lang, "state_results_title", query=esc(query)), ""]
        for s in found:
            unit = s.get("attributes", {}).get("unit_of_measurement", "")
            lines.append(
                f"• <b>{esc(ent.label(s, areas))}</b>: {esc(s['state'])}{esc(' ' + unit if unit else '')}\n"
                f"  <code>{esc(s['entity_id'])}</code>"
            )
        await self.reply(update, "\n".join(lines), lang)

    async def cmd_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/esegui [nome]`` and ``/run [name]``: run a script or an automation.

        The only *executing* command of the bot, as opposed to the switching ones:
        it starts something that then runs on its own. With no argument it lists
        what is available, since nobody remembers the name of every automation they
        wrote.
        """
        if not await self.guard(update):
            return
        await self._run(update, " ".join(ctx.args or []).strip(), self.resolve_lang(update))

    async def _run(self, update: Update, query: str, lang: str) -> None:
        """Resolve what the user wants to run, and run it.

        Targets are resolved with the same ladder as :meth:`_switch`, minus the room
        and whole-house rules -- a script has no area, and "run the whole house"
        means nothing:

        1. **No query** -- list everything runnable, with a button per entity. A
           catalogue that does not fit one Telegram message is sent as several,
           split by :func:`views.runnables_pages`.
        2. **A single entity**, when the search returns one result or the top
           result's name matches the query exactly.
        3. **Several candidates** -- a keyboard, one button each. Nothing runs until
           the user picks. There is deliberately no "all of them" button: firing
           every matching automation at once is never what someone meant.

        The catalogue comes from :meth:`runnables` -- read at startup and refreshed
        on a slow cycle -- not from a live snapshot: the menu is a list of things the
        user configured, not a reading of the house, so it does not need to be
        current to the second. The areas mapping is fetched separately because it is
        cached for the lifetime of the process by the client.

        Args:
            update: The update to reply to.
            query: What to run: a script or automation name. Empty lists.
            lang: Language to answer in.

        Note:
            Authorisation is *not* checked here, as for every other ``_*`` method:
            both callers -- ``/esegui`` and the natural-language path -- have already
            done it.
        """
        runnables = await self.runnables()
        areas = await self._areas_or_empty()
        if not runnables:
            await self.reply(update, t(lang, "no_runnables"), lang)
            return

        if not query:
            # One message per page, each carrying the buttons for the entities it
            # names: a catalogue too big for one Telegram message is split, never
            # truncated.
            for text, page in views.runnables_pages(runnables, lang):
                await self.reply(update, text, lang, reply_markup=views.run_keyboard(page, lang))
            return

        found = ent.search(query, runnables, areas, domains=RUN_DOMAINS, limit=MAX_BUTTONS)
        if not found:
            await self.reply(update, t(lang, "nothing_to_run", query=esc(query)), lang)
            return
        if len(found) == 1 or ent.normalize(ent.friendly_name(found[0])) == ent.normalize(query):
            await self._execute(update, found[0], lang)
            return
        await self.reply(
            update,
            t(lang, "which_to_run"),
            lang,
            reply_markup=views.run_keyboard(found, lang),
        )

    async def _areas_or_empty(self) -> dict[str, str]:
        """Return the areas mapping, or an empty one when it cannot be read.

        Only ``/esegui`` uses this. Everywhere else a missing areas mapping would
        gut the answer -- ``/luci`` is a per-room summary -- but scripts and
        automations are almost never assigned to a room, so for them the mapping
        only contributes a third haystack to :func:`entities.search`. Losing it
        degrades the fuzzy matching slightly; refusing to answer would lose the
        whole command.

        That matters because the catalogue itself is cached: without this, an
        instance that went down after startup would still break ``/esegui`` on the
        areas call alone, defeating the point of caching the catalogue.

        Returns:
            The mapping, or ``{}`` when Home Assistant is unreachable. The real
            client caches it for the lifetime of the process, so this is a live call
            only the first time.
        """
        try:
            return await self.ha.areas()
        except HomeAssistantError as exc:
            log.warning("Areas unavailable, running the menu without them: %s", exc)
            return {}

    async def _execute(self, update: Update, target: dict[str, Any], lang: str) -> None:
        """Run one entity and confirm it in the chat.

        Args:
            update: The update to reply to.
            target: The entity to run.
            lang: Language to confirm in.

        Note:
            The confirmation says the run was *started*, not that it finished:
            Home Assistant returns as soon as it has accepted the call, and a script
            with a ten-minute delay in it is still running long after the user has
            read the reply.
        """
        entity_id = target["entity_id"]
        await self._run_ids([entity_id])
        icon = RUN_ICONS.get(entity_id.split(".")[0], RUN_ICON_DEFAULT)
        await self.reply(update, t(lang, "result_run", icon=icon, what=esc(ent.friendly_name(target))), lang)

    # --------------------------------------------------------------- actions
    async def _switch(self, update: Update, query: str, turn_on: bool, lang: str) -> None:
        """Resolve what the user meant and turn it on or off.

        The heart of the bot, shared by ``/accendi``, ``/spegni`` and every equivalent
        sentence. Targets are resolved in order of decreasing specificity, and the
        first rule that yields anything wins:

        1. **The whole house** (:meth:`_is_home`) -- every reachable light.
        2. **A room** (:meth:`_match_area`) -- every reachable light in it.
        3. **A single entity**, when the search returns exactly one result or the top
           result's name matches the query exactly. The exact-name rule matters on
           installations where one lamp's name is a prefix of another's.
        4. **Several candidates** -- a keyboard is offered, one button per candidate
           plus an "all of them" button. Nothing is switched until the user picks.

        Rules 1 and 2 act on the ``light`` domain only and skip unreachable entities
        (see :meth:`_bulk_targets`); rule 3 and 4 also accept ``switch``, so a smart
        plug can be named explicitly.

        Args:
            update: The update to reply to.
            query: What to act on: a light name, a room, or a whole-house word. Empty
                input produces a usage hint.
            turn_on: ``True`` to turn on, ``False`` to turn off.
            lang: Language to answer in.

        Note:
            Authorisation is *not* checked here, as for every other ``_*`` method:
            both callers -- ``/accendi`` and the natural-language path -- have
            already done it.
        """
        verb = t(lang, "verb_on" if turn_on else "verb_off")
        if not query.strip():
            command = t(lang, "command_on" if turn_on else "command_off")
            await self.reply(update, t(lang, "switch_usage", command=command), lang)
            return

        states, areas = await self.snapshot()
        lights = self._lights(states, domains=LIGHT_DOMAINS)

        # "casa" means every room; a room means every light in it.
        if self._is_home(query):
            targets = self._bulk_targets(lights, areas)
            if targets:
                title = t(lang, i18n.plural("whole_house", len(targets)), count=len(targets))
                await self._apply(update, targets, turn_on, lang, title=title)
                return

        area_match = self._match_area(query, areas)
        if area_match:
            targets = self._bulk_targets(lights, areas, area=area_match)
            if targets:
                title = t(lang, i18n.plural("area_count", len(targets)), area=area_match, count=len(targets))
                await self._apply(update, targets, turn_on, lang, title=title)
                return

        found = ent.search(query, lights, areas, domains=LIGHT_DOMAINS, limit=MAX_BUTTONS)
        if not found:
            await self.reply(update, t(lang, "nothing_to_switch", verb=verb, query=esc(query)), lang)
            return
        if len(found) == 1 or ent.normalize(ent.friendly_name(found[0])) == ent.normalize(query):
            await self._apply(update, found[:1], turn_on, lang)
            return

        await self.reply(
            update,
            t(lang, "which_one", verb=verb),
            lang,
            reply_markup=views.choice_keyboard(found, areas, turn_on, lang),
        )

    @staticmethod
    def _bulk_targets(lights: list[dict[str, Any]], areas: dict[str, str], area: str | None = None) -> list[dict[str, Any]]:
        """Select the lights a bulk operation should act on.

        Two filters, both deliberate:

        * **``light`` domain only.** "Turn off the whole house" must not cut power to
          the fridge or the router because they happen to be behind smart plugs. A
          switch can still be targeted by naming it.
        * **Reachable entities only.** ``unavailable`` and ``unknown`` entities are
          dropped, so the count reported back to the user ("Salone (3 luci) accese")
          is the number of lights that actually received the command, not the number
          that were asked to.

        Args:
            lights: Candidate entities, typically already restricted to
                :data:`LIGHT_DOMAINS`.
            areas: Mapping ``entity_id -> area name``.
            area: Restrict to one room by exact area name; ``None`` means the whole
                house.

        Returns:
            The entities to act on, possibly empty -- which the caller must handle by
            falling through to a name search rather than reporting success on nothing.
        """
        return [
            s
            for s in HassBot._lights(lights, areas, area=area)
            if s.get("state") not in ("unavailable", "unknown")
        ]

    async def _call_on_ids(self, ids: list[str], turn_on: bool) -> None:
        """Turn a set of entities on or off with one service call per domain.

        Home Assistant accepts a list of entity ids in a single service call, but the
        service is addressed as ``<domain>/turn_on``, so mixed domains cannot share a
        call. The ids are therefore bucketed by their domain prefix and one call is
        issued per bucket -- two at most in practice (``light`` and ``switch``).

        Args:
            ids: Full entity ids. Ids of unknown domains are passed through untouched
                and will simply fail on the Home Assistant side.
            turn_on: ``True`` for ``turn_on``, ``False`` for ``turn_off``.

        Raises:
            ha_client.HomeAssistantError: If any call fails. Earlier calls are not
                rolled back -- there is no transaction to roll back to -- so a partial
                failure leaves the house in a mixed state and the user is told
                something went wrong.
        """
        service = "turn_on" if turn_on else "turn_off"
        by_domain: dict[str, list[str]] = {}
        for entity_id in ids:
            by_domain.setdefault(entity_id.split(".")[0], []).append(entity_id)
        for domain, group in by_domain.items():
            await self.ha.call_service(domain, service, {"entity_id": group})

    async def _run_ids(self, ids: list[str]) -> None:
        """Execute a set of scripts or automations.

        Like :meth:`_call_on_ids` this buckets by domain and issues one call per
        bucket, but the service is not the same for every bucket: a script is
        *started* with ``turn_on``, whereas an automation needs ``trigger``. ``automation.turn_on`` would merely enable it -- the automation
        would then fire at its own trigger, minutes or days later, which reads as
        "nothing happened" to whoever asked.

        Args:
            ids: Full entity ids. Ids outside :data:`RUN_SERVICES` are skipped rather
                than guessed at: there is no safe default service for an unknown
                domain, and silently calling ``turn_on`` on one could do anything.

        Raises:
            ha_client.HomeAssistantError: If any call fails. As in
                :meth:`_call_on_ids`, earlier calls are not rolled back.
        """
        by_domain: dict[str, list[str]] = {}
        for entity_id in ids:
            domain = entity_id.split(".")[0]
            if domain in RUN_SERVICES:
                by_domain.setdefault(domain, []).append(entity_id)
            else:
                log.warning("Not a runnable entity, skipped: %s", entity_id)
        for domain, group in by_domain.items():
            await self.ha.call_service(domain, RUN_SERVICES[domain], {"entity_id": group})

    async def _apply(self, update: Update, targets: list[dict[str, Any]], turn_on: bool, lang: str, title: str | None = None) -> None:
        """Execute a switch operation and confirm it in the chat.

        Args:
            update: The update to reply to.
            targets: The entities to act on. Must not be empty; callers check.
            turn_on: ``True`` to turn on, ``False`` to turn off.
            lang: Language to confirm in.
            title: What to call the operation in the confirmation. Bulk callers pass
                something like ``"Salone (3 luci)"``; when omitted, the confirmation
                names the single entity, or falls back to a count for several.

        Note:
            The confirmation is sent after the service call returns, so it reports what
            Home Assistant accepted, not what the bot intended. A failing call raises
            before anything is confirmed.
        """
        await self._call_on_ids([s["entity_id"] for s in targets], turn_on)

        icon = "🟡" if turn_on else "⚫"
        what = title or ent.friendly_name(targets[0])
        # Italian inflects the confirmation for number, and so does the title the
        # caller built: both follow the real count, so "Studio (1 luce) accesa".
        many = len(targets) > 1
        if many and not title:
            what = t(lang, i18n.plural("n_entities", len(targets)), count=len(targets))
        key = f"result_{'on' if turn_on else 'off'}_{'many' if many else 'one'}"
        await self.reply(update, t(lang, key, icon=icon, what=esc(what)), lang)

    async def _temperature(self, update: Update, query: str, lang: str) -> None:
        """Report temperature and humidity, for one room or for the whole house.

        Shared by ``/temperatura`` and by sentences such as "quanti gradi in salone",
        which is why authorisation is not checked here.

        Sensors are selected by :meth:`_temp_sensors`, on ``device_class`` rather
        than on name, so the answer does not depend on how the user named their
        sensors. ``climate`` entities are handled separately: they carry their
        reading in ``attributes.current_temperature`` instead of in their state, and
        they are only included when a specific room was asked for -- a thermostat in
        every room summary would bury the actual sensor readings.

        Four outcomes:

        * Whole house: one block per room, plus a keyboard to drill into a room.
        * A recognised room: that room's sensors, no keyboard.
        * Unrecognised text: a fuzzy search over sensors, capped at six results.
        * Nothing found: a message saying so.

        Args:
            update: The update to reply to.
            query: A room, a sensor name, a whole-house word, or the empty string
                (equivalent to the whole house).
            lang: Language to answer in.
        """
        states, areas = await self.snapshot()
        sensors = self._temp_sensors(states)
        climates = [s for s in states if s["entity_id"].startswith("climate.")]

        if self._is_home(query):
            query = ""
        area = self._match_area(query, areas) if query else None
        if query and not area:
            found = ent.search(query, sensors, areas, limit=6)
            if not found:
                await self.reply(update, t(lang, "no_temp_sensors_for", query=esc(query)), lang)
                return
            lines = [t(lang, "temp_title", title=esc(query)), ""] + [views.sensor_line(s, areas, lang) for s in found]
            await self.reply(update, "\n".join(lines), lang)
            return

        pool = [s for s in sensors if not area or areas.get(s["entity_id"]) == area]
        pool += [c for c in climates if area and areas.get(c["entity_id"]) == area]
        if not pool:
            key = "no_temp_sensors_in" if area else "no_temp_sensors"
            await self.reply(update, t(lang, key, area=esc(area or "")), lang)
            return

        if area:
            lines = [t(lang, "temp_title", title=esc(area)), ""] + [views.sensor_line(s, areas, lang) for s in pool]
        else:
            lines = [t(lang, "temp_by_room_title"), ""]
            for name, group in ent.group_by_area(pool, areas).items():
                if name == ent.NO_AREA:
                    continue
                lines.append(f"<b>{esc(name)}</b>")
                lines += [f"  {views.sensor_line(s, areas, lang, short=True)}" for s in group]
                lines.append("")
        await self.reply(
            update,
            "\n".join(lines).strip(),
            lang,
            reply_markup=None if area else views.areas_keyboard(pool, areas, prefix="temp"),
        )

    # ----------------------------------------------------- target resolution
    @staticmethod
    def _is_home(query: str) -> bool:
        """Decide whether a query refers to the whole house.

        Args:
            query: Raw user text.

        Returns:
            ``True`` when the normalized query is one of :data:`i18n.HOME_WORDS`,
            in either language ("casa", "tutta la casa", "ovunque", "house",
            "everything", "everywhere", ...).

        Note:
            Matching is exact on the normalized string, not substring-based: "casa"
            must be the whole query. Otherwise a room legitimately named "Casetta" or a
            sentence mentioning the house in passing would trigger a whole-house
            operation, which is the single most destructive thing the bot can do.
        """
        return i18n.is_home(ent.normalize(query))

    def _match_area(self, query: str, areas: dict[str, str]) -> str | None:
        """Match a query against the names of the rooms that exist.

        Exact match on the normalized name first; failing that, a containment match in
        either direction, so "camera" finds "Camera da letto" and "camera da letto"
        finds "Camera".

        Args:
            query: Raw user text.
            areas: Mapping ``entity_id -> area name``; its values are the room names.

        Returns:
            The room name as Home Assistant spells it -- the caller compares it against
            ``areas`` values, so the original casing must be preserved -- or ``None``.

        Note:
            An ambiguous partial match returns ``None`` rather than guessing, and the
            candidates are logged at debug level. The caller then falls through to an
            entity search, which typically offers the user a choice of buttons. That is
            the right failure mode: better to ask than to switch the wrong room.
        """
        q = ent.normalize(query)
        if not q:
            return None
        names = sorted(set(areas.values()))
        exact = [n for n in names if ent.normalize(n) == q]
        if exact:
            return exact[0]
        partial = [n for n in names if q in ent.normalize(n) or ent.normalize(n) in q]
        if len(partial) == 1:
            return partial[0]
        if partial:
            log.debug("Ambiguous area for %r: %s -- falling through to entity search", query, partial)
        return None

    # ------------------------------------------------------ natural language
    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for any non-command text message.

        Guards, then hands the text to :meth:`_dispatch_text`.
        """
        if not await self.guard(update):
            return
        await self._dispatch_text(update, (update.effective_message.text or "").strip())


    async def _dispatch_text(self, update: Update, text: str, spoken: bool = False) -> None:
        """Interpret a sentence and run the command it describes.

        The natural-language front end, shared by typed text and by transcribed voice
        messages: both converge here, so the two channels can never drift apart in what
        they understand.

        The language is detected first (:meth:`resolve_lang`), then the sentence
        is parsed with that language's grammar by :func:`i18n.parse`, which
        returns a language-independent intent. Everything below this point is
        therefore identical for both languages -- only the vocabulary differs.

        The six intents are ``temperature``, ``on``, ``off``, ``lights_on``
        (list what is currently on), ``lights`` (browse) and ``run`` (execute a
        script or an automation). A sentence matching
        none of them is quoted back so the user can see how it was understood --
        especially useful after a transcription -- along with a pointer to the
        commands.

        There is no model and no external service here: the vocabulary is fixed,
        which makes the bot predictable, instant, and functional without an
        internet connection.

        Args:
            update: The update to reply to.
            text: The sentence, raw. Normalisation and detection happen inside.
            spoken: ``True`` when the text came from a voice message, which only
                changes the wording of the "did not understand" reply.
        """
        low = ent.normalize(text)
        lang = self.resolve_lang(update, text)
        if not low:
            await self.reply(update, t(lang, "not_understood_short"), lang)
            return

        intent, target = i18n.parse(low, lang)

        if intent == "temperature":
            await self._temperature(update, target, lang)
            return
        if intent in ("on", "off"):
            await self._switch(update, target, turn_on=intent == "on", lang=lang)
            return
        if intent == "lights_on":
            await self._lights_on(update, lang)
            return
        if intent == "lights":
            await self._lights_browse(update, target, lang, overview_on_miss=True)
            return
        if intent == "run":
            await self._run(update, target, lang)
            return

        hint = t(lang, "voice_hint") if spoken else ""
        await self.reply(update, t(lang, "not_understood", text=esc(text), hint=hint), lang)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler: turn any unhandled exception into a reply.

    Registered on the ``Application``, so it catches whatever the handlers do not.
    Without it, a Home Assistant that is down or a bug in a handler would produce a
    log line and complete silence in the chat, which reads to the user as a bot
    that has stopped working for no reason.

    Home Assistant failures get a specific message including the underlying error,
    localised by :func:`views.ha_error_text` -- "connection refused" and "401" tell the
    user immediately whether the instance is down or the token has expired -- while
    anything else gets a generic apology, since its message is not meant for users.
    The full traceback goes to the log either way.

    Args:
        update: The update being processed. It may not be an ``Update`` at all, and
            it may carry no message (an error raised while processing a callback,
            for instance), in which case there is nowhere to reply and only the log
            entry remains.
        ctx: The context, whose ``error`` attribute holds the exception.

    Note:
        The reply is itself wrapped: if Telegram is the thing that is failing, the
        handler must not raise from inside the error path.
    """
    log.error("Unhandled error", exc_info=ctx.error)
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    hass = ctx.application.bot_data.get("hass") if ctx.application else None
    lang = hass.lang_of(update) if hass else i18n.DEFAULT_LANG
    if isinstance(ctx.error, HomeAssistantError):
        text = t(lang, "ha_down", error=esc(ha_error_text(lang, ctx.error)))
    else:
        text = t(lang, "generic_error")
    try:
        await message.reply_text(clip(text, lang), parse_mode=ParseMode.HTML)
    except Exception:  # if Telegram itself is failing there is nothing left to try
        log.debug("Could not notify the user of the error", exc_info=True)


async def set_command_menu(app: Application) -> None:
    """Publish the command menu Telegram shows next to the text box.

    Three lists are sent: one per supported language, scoped with
    ``language_code`` so a client set to Italian sees ``/luci`` and one set to
    English sees ``/lights``, plus an unscoped default for every other locale,
    which uses the bot's own :attr:`HassBot.default_lang`. The contents come from
    :data:`i18n.COMMAND_MENU`.

    Args:
        app: The application, whose ``bot_data`` carries the :class:`HassBot`.

    Note:
        A failure here is logged and swallowed, unlike the Home Assistant check in
        :func:`post_init`. The menu is a convenience -- every command works when
        typed whether or not Telegram ever accepted the list -- so a rate limit or a
        transient API error must not stop the bot from starting.
    """
    hass: HassBot = app.bot_data["hass"]

    def menu(lang: str) -> list[BotCommand]:
        return [BotCommand(name, description) for name, description in i18n.COMMAND_MENU[lang]]

    try:
        await app.bot.set_my_commands(menu(hass.default_lang))
        for lang in i18n.COMMAND_MENU:
            await app.bot.set_my_commands(menu(lang), language_code=lang)
    except TelegramError as exc:
        log.warning("Could not publish the command menu: %s", exc)
    else:
        log.info("Command menu published for: %s", ", ".join(i18n.COMMAND_MENU))


async def post_init(app: Application) -> None:
    """Startup hook: verify Home Assistant and choose a transcription engine.

    Runs after the ``Application`` is built but before polling starts, so a bad URL
    or a revoked token stops the process immediately with a clear log message
    instead of surfacing later as commands that mysteriously do nothing.

    Four steps, in order: ping Home Assistant, pick a speech-to-text engine, read
    the runnable catalogue once and start the cycle that keeps it current, then
    publish the command menu. The catalogue is read here rather than on the first
    ``/esegui`` so that the very first use of the command is as fast as every
    later one.

    Args:
        app: The application, whose ``bot_data`` carries the :class:`HassBot`.

    Raises:
        ha_client.HomeAssistantError: If the instance is unreachable or rejects the
            token. Failing here is intentional; under systemd the unit restarts and
            retries, which is the desired behaviour when the bot boots before Home
            Assistant does. The command menu is the one step allowed to fail
            quietly -- see :func:`set_command_menu`.
    """
    bot: HassBot = app.bot_data["hass"]
    msg = await bot.ha.ping()
    log.info("Home Assistant: %s", msg)
    await bot.discover_stt()
    await bot.refresh_runnables()
    bot.start_refreshing()
    await set_command_menu(app)


async def post_shutdown(app: Application) -> None:
    """Shutdown hook: close the Home Assistant HTTP session.

    Stops the catalogue refresh task before closing the session, so the task cannot
    wake up to find the client already gone.

    Args:
        app: The application, whose ``bot_data`` carries the :class:`HassBot`.
    """
    hass: HassBot = app.bot_data["hass"]
    await hass.stop_refreshing()
    await hass.ha.aclose()


def main() -> None:
    """Load configuration, wire the handlers and run the bot until interrupted.

    Configuration comes from the environment, with ``.env`` loaded first:

    ======================================= ====================================
    Variable                                Meaning
    ======================================= ====================================
    ``TELEGRAM_BOT_TOKEN``                  Bot token from @BotFather. Required.
    ``HOME_ASSISTANT_API_URL``              API root, ending in ``/api/``.
                                            Required.
    ``HOME_ASSISTANT_API_ACCESS_TOKEN``     Long-lived access token. Required.
    ``TELEGRAM_CHAT_ID``                    Allowed chat ids, comma separated.
                                            Empty means anyone may use the bot.
    ``HA_STT_ENTITY``                       Speech-to-text entity. Optional;
                                            auto-detected when unset.
    ``BOT_LANGUAGE``                        Language for a chat that has not
                                            said anything recognisable yet,
                                            ``it`` or ``en``. Default ``it``.
    ``STT_LANGUAGE_IT`` / ``STT_LANGUAGE``  Transcription tag for Italian,
                                            default ``it-IT``.
    ``STT_LANGUAGE_EN``                     Transcription tag for English,
                                            default ``en-US``.
    ``RUNNABLES_REFRESH_SECONDS``           How often the script and automation
                                            catalogue is re-read.
                                            Default 300; ``0`` disables the
                                            cycle and reads it once at startup.
    ======================================= ====================================

    Chat ids are extracted with a regular expression rather than split on commas,
    so extra spaces, trailing separators and quotes are all tolerated; the pattern
    keeps the leading minus that group chat ids carry. An empty allow-list is
    permitted but logged as a warning, because it means anyone who finds the bot
    controls the house.

    Handler order matters: commands are registered first, and the catch-all text
    handler explicitly excludes commands, so an unknown ``/command`` is not fed to
    the natural-language parser. The error handler is registered last.

    Every command is registered under both an Italian and an English name; the
    name the user types is itself a language signal (see :data:`COMMAND_LANG`).

    Blocks in ``run_polling`` until the process is interrupted.

    Raises:
        SystemExit: If any of the three required variables is missing. The message
            names them, which is the most common first-run mistake.
    """
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    ha_url = os.getenv("HOME_ASSISTANT_API_URL")
    ha_token = os.getenv("HOME_ASSISTANT_API_ACCESS_TOKEN")
    if not (token and ha_url and ha_token):
        sys.exit("Missing TELEGRAM_BOT_TOKEN, HOME_ASSISTANT_API_URL or HOME_ASSISTANT_API_ACCESS_TOKEN in .env")

    allowed = {int(c) for c in re.findall(r"-?\d+", os.getenv("TELEGRAM_CHAT_ID", ""))}
    if not allowed:
        log.warning("TELEGRAM_CHAT_ID is not set: the bot will answer anyone.")

    # STT_LANGUAGE stays supported as the Italian tag, so existing .env files
    # keep working now that there are two languages to configure.
    stt_languages = {
        "it": os.getenv("STT_LANGUAGE_IT") or os.getenv("STT_LANGUAGE") or "it-IT",
        "en": os.getenv("STT_LANGUAGE_EN") or "en-US",
    }

    # An unparsable value is a typo, not a reason to refuse to start: the catalogue
    # simply falls back to the default cycle, and the log says so.
    raw_refresh = os.getenv("RUNNABLES_REFRESH_SECONDS")
    try:
        refresh = float(raw_refresh) if raw_refresh else RUNNABLES_REFRESH_SECONDS
    except ValueError:
        log.warning("RUNNABLES_REFRESH_SECONDS=%r is not a number, using %s", raw_refresh, RUNNABLES_REFRESH_SECONDS)
        refresh = RUNNABLES_REFRESH_SECONDS

    ha = HomeAssistantClient(ha_url, ha_token)
    hass = HassBot(
        ha,
        allowed,
        stt_entity=os.getenv("HA_STT_ENTITY") or None,
        stt_languages=stt_languages,
        default_lang=os.getenv("BOT_LANGUAGE", i18n.DEFAULT_LANG),
        runnables_refresh=refresh,
    )

    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data["hass"] = hass

    app.add_handler(CommandHandler(["start", "help", "aiuto"], hass.cmd_start))
    app.add_handler(CommandHandler(["luci", "lights"], hass.cmd_lights))
    app.add_handler(CommandHandler(["accese", "whatson"], hass.cmd_on_now))
    app.add_handler(CommandHandler(["accendi", "on"], hass.cmd_on))
    app.add_handler(CommandHandler(["spegni", "off"], hass.cmd_off))
    app.add_handler(CommandHandler(["temperatura", "temperature", "temp"], hass.cmd_temperature))
    app.add_handler(CommandHandler(["stato", "state"], hass.cmd_state))
    app.add_handler(CommandHandler(["esegui", "run"], hass.cmd_run))
    app.add_handler(CommandHandler(["lingua", "language"], hass.cmd_language))
    app.add_handler(CallbackQueryHandler(functools.partial(callbacks.on_callback, hass)))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
            functools.partial(voice.on_voice, hass),
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, hass.on_text))
    app.add_error_handler(on_error)

    log.info("Bot started (allowed chats: %s, default language: %s)", allowed or "all", hass.default_lang)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
