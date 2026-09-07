"""Telegram front end for Home Assistant.

Hassgram lets a small, trusted set of Telegram chats drive a Home Assistant
installation by typing commands, speaking them, or tapping inline buttons. This
module owns everything Telegram-shaped: handler registration, message
formatting, inline keyboards, callback routing, voice transcription and the
Italian natural-language parser. Domain logic lives in :mod:`entities` and all
HTTP traffic in :mod:`ha_client`.

Request flow
------------

Three entry points converge on the same execution path::

    /accendi studio  ---> CommandHandler ------+
    "accendi lo studio" -> on_text --+         |
    voice note --> on_voice --(STT)--+--> _dispatch_text --> _switch --> _apply
                                                                          |
    button tap ---> on_callback -> _handle_callback ----------------------+

Every path ends in :meth:`HassBot._call_on_ids`, which groups entity ids by
domain and calls one Home Assistant service per domain.

Cross-cutting rules
-------------------

Authorisation
    Every handler starts with :meth:`HassBot.guard` (or, for callbacks,
    :meth:`HassBot.authorized`). ``TELEGRAM_CHAT_ID`` holds the allow-list; an
    empty list means the bot answers anyone, which is only reasonable while
    testing.

Message size
    Telegram rejects messages longer than 4096 characters, and a large house
    easily exceeds that. :func:`clip` truncates on a line boundary -- every
    line the bot emits is self-contained HTML, so cutting between lines leaves
    the markup balanced -- and every outbound message goes through
    :meth:`HassBot.reply`, which applies it.

Callback payloads
    ``callback_data`` is capped at 64 bytes by Telegram, far too small for a
    list of entity ids. Buttons therefore carry a 12-character token, resolved
    against the in-memory LRU behind :func:`tok` and :func:`untok`.

Failure handling
    Handlers do not defend against Home Assistant being down. :func:`on_error`
    is registered as the global error handler and turns any unhandled
    exception into a message to the user, so a failure is never silent.

All strings shown to the user are Italian; code, comments and docstrings are
English.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sys
from collections import OrderedDict
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import entities as ent
from ha_client import HomeAssistantClient, HomeAssistantError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hassgram")

LIGHT_DOMAINS = ("light", "switch")
# Words that mean "the whole house", i.e. every room at once.
HOME_WORDS = {"casa", "tutta casa", "tutta la casa", "tutte le stanze", "ovunque", "appartamento", "tutto"}
MAX_BUTTONS = 24
MAX_VOICE_BYTES = 5 * 1024 * 1024  # ~5 minutes of ogg/opus: past that it is almost certainly not a command
MAX_MESSAGE_CHARS = 4000  # Telegram stops at 4096: leave room for the truncation notice
MAX_TOKENS = 2000  # keyboards stay usable without letting the map grow forever

# Telegram caps callback_data at 64 bytes, so buttons carry a token and the real
# value lives here. An LRU, not a plain dict: the bot runs for months under systemd.
# Eviction is a supported outcome -- callers already treat None as an expired session.
_tokens: OrderedDict[str, str] = OrderedDict()


def tok(value: str) -> str:
    """Store a value and return a short token that fits in ``callback_data``.

    Telegram caps ``callback_data`` at 64 bytes, which cannot hold a list of entity
    ids -- a single "turn all these off" button may reference two dozen. Buttons
    therefore carry the first 12 hex characters of the value's SHA-1, and the
    mapping back to the real value lives in this process.

    The store is an LRU capped at :data:`MAX_TOKENS`: the bot runs for months under
    systemd, and an unbounded dictionary would only ever grow. Re-registering an
    existing value refreshes its position rather than adding a duplicate, because
    the key is derived from the value.

    Args:
        value: What the button should resolve to: a single entity id, several ids
            joined by ``"|"``, or an area name.

    Returns:
        A 12-character hex token, safe to embed in ``callback_data``.

    Note:
        Eviction is not an error condition. When a token has fallen out of the
        store, :func:`untok` returns ``None`` and the caller answers "sessione
        scaduta", asking the user to re-issue the command -- the same path taken by
        a button from a previous run of the process.
    """
    key = hashlib.sha1(value.encode()).hexdigest()[:12]
    _tokens[key] = value
    _tokens.move_to_end(key)
    while len(_tokens) > MAX_TOKENS:
        _tokens.popitem(last=False)
    return key


def untok(key: str) -> str | None:
    """Resolve a token produced by :func:`tok` back to its value.

    A successful lookup refreshes the entry's LRU position, so a keyboard that is
    still being used stays alive regardless of how long ago it was rendered.

    Args:
        key: The token extracted from ``callback_data``.

    Returns:
        The stored value, or ``None`` if the token was evicted or belongs to a
        previous run of the process. Callers must treat ``None`` as an expired
        session, never as a bug.
    """
    value = _tokens.get(key)
    if value is not None:
        _tokens.move_to_end(key)
    return value


def esc(text: Any) -> str:
    """Escape a value for Telegram's HTML parse mode.

    Every message is sent with ``parse_mode=HTML``, so any text that is not markup
    the bot itself wrote must be escaped. That includes entity names and area names
    -- which come from Home Assistant and can legitimately contain ``&`` or ``<``
    -- and, more importantly, the user's own query text, which is echoed back in
    several error messages.

    Args:
        text: Any value; it is stringified first, so ``None`` and numbers are fine.

    Returns:
        The value with ``&``, ``<`` and ``>`` escaped.
    """
    return html.escape(str(text))


def clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Shorten a message so Telegram will accept it, keeping the HTML valid.

    Telegram rejects anything over 4096 characters, and lists such as "every light
    in the house" or "every sensor per room" pass that on a large installation.
    Truncating at an arbitrary offset would risk cutting a message in the middle of
    a ``<b>`` tag and getting the whole message rejected for malformed markup, so
    the cut is made at the last newline that fits: every line the bot emits is
    self-contained HTML, which makes line boundaries safe cut points.

    Args:
        text: The message body, HTML included.
        limit: Maximum length before the notice is appended. Defaults to
            :data:`MAX_MESSAGE_CHARS`, which leaves room under Telegram's own cap
            for the notice itself.

    Returns:
        The text unchanged when it fits, otherwise a prefix ending on a line
        boundary followed by an italic "elenco troncato" notice. A single line
        longer than the limit -- which the bot never produces, but which a hostile
        entity name could -- is cut at the limit as a last resort.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return (text[:cut] if cut > 0 else text[:limit]) + "\n<i>… elenco troncato.</i>"


class HassBot:
    """Stateful holder for the bot's handlers.

    One instance is created in :func:`main`, stored in ``Application.bot_data`` and
    shared by every handler. It holds the Home Assistant client, the chat
    allow-list and the speech-to-text configuration; it deliberately holds no
    per-conversation state, so restarting the process loses nothing but the
    callback token store.

    Method naming follows a strict convention:

    ``cmd_*``
        Bound to a Telegram command. Calls :meth:`guard` first, then delegates.
    ``on_*``
        Bound to a non-command update (text, voice, callback query). Also guards.
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
        stt_language: BCP-47 tag passed to the STT provider, e.g. ``it-IT``.
    """
    def __init__(
        self,
        ha: HomeAssistantClient,
        allowed_chats: set[int],
        stt_entity: str | None = None,
        stt_language: str = "it-IT",
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
            stt_language: Language tag for transcription. The provider must advertise
                it or every voice message will be rejected.
        """
        self.ha = ha
        self.allowed_chats = allowed_chats
        self.stt_entity = stt_entity
        self.stt_language = stt_language

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
            log.info("Speech-to-text: %s (lingua %s)", self.stt_entity, self.stt_language)
        else:
            log.warning("Nessuna entità stt. in Home Assistant: i vocali non saranno trascritti.")

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
        log.warning("Accesso negato per chat %s", update.effective_chat.id if update.effective_chat else "?")
        if update.effective_message:
            await self.reply(update, "⛔️ Non sei autorizzato a usare questo bot.")
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
    async def reply(update: Update, text: str, **kwargs: Any) -> None:
        """Send a message to the chat an update came from.

        The single exit point towards Telegram, which is what makes two guarantees
        hold everywhere instead of per call site: HTML parse mode is the default, and
        the body is always run through :func:`clip` so an oversized list degrades into
        a truncated one rather than a rejected message.

        Args:
            update: The update to reply to.
            text: Message body. May contain Telegram-flavoured HTML; any interpolated
                value that did not originate here must be passed through :func:`esc`.
            **kwargs: Forwarded to ``reply_text`` -- typically ``reply_markup`` for an
                inline keyboard. ``parse_mode`` can be overridden if a caller ever
                needs plain text.
        """
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        await update.effective_message.reply_text(clip(text), **kwargs)

    # -------------------------------------------------------------- commands
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/start``, ``/help`` and ``/aiuto``: print the command reference.

        The text doubles as the bot's documentation for users who never read the
        README, so it lists every command with an example and mentions both the
        natural-language and the voice entry points.
        """
        if not await self.guard(update):
            return
        await self.reply(
            update,
            "🏠 <b>Hassgram</b> — controllo di Home Assistant\n\n"
            "<b>Comandi</b>\n"
            "/luci — stanze e luci, con accensione/spegnimento a bottoni\n"
            "/luci <i>stanza</i> — solo le luci di quella stanza\n"
            "/accese — tutto ciò che è acceso in questo momento\n"
            "/accendi <i>nome</i> — es. <code>/accendi studio</code>, <code>/accendi casa</code>\n"
            "/spegni <i>nome</i> — es. <code>/spegni luciCucina</code>\n"
            "/temperatura [<i>stanza</i>] — es. <code>/temperatura salone</code>\n"
            "<i>«casa» vale come tutte le stanze insieme.</i>\n"
            "/stato <i>nome</i> — stato di una qualsiasi entità\n\n"
            "Puoi anche scrivermi in linguaggio naturale: "
            "<i>«accendi la luce dello studio»</i>, <i>«che temperatura c'è in camera?»</i>\n"
            "🎙 <b>Oppure mandami un vocale</b> con lo stesso comando: lo trascrivo con "
            "Home Assistant e lo eseguo.",
        )

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
        query = " ".join(ctx.args or []).strip()
        states, areas = await self.snapshot()
        lights = [s for s in states if s["entity_id"].startswith("light.")]

        if not query or self._is_home(query):
            await self.reply(
                update,
                self._areas_summary(lights, areas),
                reply_markup=self._areas_keyboard(lights, areas),
            )
            return

        found = ent.search(query, lights, areas, domains=("light",), limit=MAX_BUTTONS)
        if not found:
            await self.reply(update, f"Nessuna luce trovata per «{esc(query)}».")
            return
        await self.reply(
            update,
            self._lights_text(query, found),
            reply_markup=self._lights_keyboard(found),
        )

    async def cmd_on(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/accendi <name>`` and ``/on <name>``: turn something on.

        Thin wrapper over :meth:`_switch`, which does the target resolution. The
        argument may name one light, a room, or the whole house.
        """
        await self._switch(update, " ".join(ctx.args or []), turn_on=True)

    async def cmd_off(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/spegni <name>`` and ``/off <name>``: turn something off.

        Thin wrapper over :meth:`_switch`; see :meth:`cmd_on`.
        """
        await self._switch(update, " ".join(ctx.args or []), turn_on=False)

    async def cmd_on_now(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/accese``: list everything currently on.

        Guards, then delegates to :meth:`_lights_on`, which is shared with the
        natural-language path.
        """
        if not await self.guard(update):
            return
        await self._lights_on(update)

    async def _lights_on(self, update: Update) -> None:
        """List every light that is currently on, grouped by room.

        Shared by the ``/accese`` command and by sentences such as "quali luci sono
        accese", which is why authorisation is *not* checked here: both callers have
        already done it.

        The reply carries a keyboard of the lights that are on, capped at
        :data:`MAX_BUTTONS`, so the usual follow-up ("turn that one off") is a tap
        rather than another command. The list itself is not capped -- it is text, and
        :func:`clip` handles the extreme case.

        Args:
            update: The update to reply to.
        """
        states, areas = await self.snapshot()
        on = [s for s in states if s["entity_id"].startswith("light.") and ent.is_on(s)]
        if not on:
            await self.reply(update, "Tutte le luci sono spente. 🌙")
            return
        lines = ["💡 <b>Luci accese</b>", ""]
        for area, group in ent.group_by_area(on, areas).items():
            lines.append(f"<b>{esc(area)}</b>")
            lines += [f"  🟡 {esc(ent.friendly_name(s))}" for s in group]
            lines.append("")
        await self.reply(
            update,
            "\n".join(lines).strip(),
            reply_markup=self._lights_keyboard(on[:MAX_BUTTONS]),
        )

    async def cmd_temperature(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle ``/temperatura [room]``: report temperature and humidity.

        Guards, then delegates to :meth:`_temperature`, which is shared with the
        natural-language path.
        """
        if not await self.guard(update):
            return
        await self._temperature(update, " ".join(ctx.args or []).strip())

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
        query = " ".join(ctx.args or []).strip()
        if not query:
            await self.reply(update, "Uso: <code>/stato nome entità</code>")
            return
        states, areas = await self.snapshot()
        found = ent.search(query, states, areas, limit=10)
        if not found:
            await self.reply(update, f"Nessuna entità trovata per «{esc(query)}».")
            return
        lines = [f"🔎 <b>Risultati per «{esc(query)}»</b>", ""]
        for s in found:
            unit = s.get("attributes", {}).get("unit_of_measurement", "")
            lines.append(
                f"• <b>{esc(ent.label(s, areas))}</b>: {esc(s['state'])}{esc(' ' + unit if unit else '')}\n"
                f"  <code>{esc(s['entity_id'])}</code>"
            )
        await self.reply(update, "\n".join(lines))

    # --------------------------------------------------------------- actions
    async def _switch(self, update: Update, query: str, turn_on: bool) -> None:
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
        """
        if not await self.guard(update):
            return
        verb = "accendere" if turn_on else "spegnere"
        if not query.strip():
            await self.reply(update, f"Uso: <code>/{'accendi' if turn_on else 'spegni'} nome luce o stanza</code>")
            return

        states, areas = await self.snapshot()
        lights = [s for s in states if s["entity_id"].startswith(tuple(f"{d}." for d in LIGHT_DOMAINS))]

        # "casa" means every room; a room means every light in it.
        if self._is_home(query):
            targets = self._bulk_targets(lights, areas)
            if targets:
                await self._apply(update, targets, turn_on, title=f"Tutta la casa ({len(targets)} luci)")
                return

        area_match = self._match_area(query, areas)
        if area_match:
            targets = self._bulk_targets(lights, areas, area=area_match)
            if targets:
                await self._apply(update, targets, turn_on, title=f"{area_match} ({len(targets)} luci)")
                return

        found = ent.search(query, lights, areas, domains=LIGHT_DOMAINS, limit=MAX_BUTTONS)
        if not found:
            await self.reply(update, f"Non ho trovato niente da {verb} per «{esc(query)}».")
            return
        if len(found) == 1 or ent.normalize(ent.friendly_name(found[0])) == ent.normalize(query):
            await self._apply(update, found[:1], turn_on)
            return

        action = "on" if turn_on else "off"
        rows = [
            [InlineKeyboardButton(f"{ent.state_icon(s)} {ent.label(s, areas)}", callback_data=f"do:{action}:{tok(s['entity_id'])}")]
            for s in found
        ]
        rows.append([InlineKeyboardButton(f"⚡️ Tutte ({len(found)})", callback_data=f"all:{action}:{tok('|'.join(s['entity_id'] for s in found))}")])
        await self.reply(update, f"Quale vuoi {verb}?", reply_markup=InlineKeyboardMarkup(rows))

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
            for s in lights
            if s["entity_id"].startswith("light.")
            and s.get("state") not in ("unavailable", "unknown")
            and (area is None or areas.get(s["entity_id"]) == area)
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

    async def _apply(self, update: Update, targets: list[dict[str, Any]], turn_on: bool, title: str | None = None) -> None:
        """Execute a switch operation and confirm it in the chat.

        Args:
            update: The update to reply to.
            targets: The entities to act on. Must not be empty; callers check.
            turn_on: ``True`` to turn on, ``False`` to turn off.
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
        verb = "accesa" if turn_on else "spenta"
        if len(targets) > 1 and not title:
            what = f"{len(targets)} entità"
            verb = "accese" if turn_on else "spente"
        elif title:
            verb = "accese" if turn_on else "spente"
        await self.reply(update, f"{icon} <b>{esc(what)}</b> {verb}.")

    async def _temperature(self, update: Update, query: str) -> None:
        """Report temperature and humidity, for one room or for the whole house.

        Shared by ``/temperatura`` and by sentences such as "quanti gradi in salone",
        which is why authorisation is not checked here.

        Sensors are selected by ``device_class`` rather than by name, so the answer
        does not depend on how the user named their sensors. ``climate`` entities are
        handled separately: they carry their reading in
        ``attributes.current_temperature`` instead of in their state, and they are only
        included when a specific room was asked for -- a thermostat in every room
        summary would bury the actual sensor readings.

        Four outcomes:

        * Whole house: one block per room, plus a keyboard to drill into a room.
        * A recognised room: that room's sensors, no keyboard.
        * Unrecognised text: a fuzzy search over sensors, capped at six results.
        * Nothing found: a message saying so.

        Args:
            update: The update to reply to.
            query: A room, a sensor name, a whole-house word, or the empty string
                (equivalent to the whole house).
        """
        states, areas = await self.snapshot()
        sensors = [
            s
            for s in states
            if s["entity_id"].startswith(("sensor.", "climate."))
            and s.get("attributes", {}).get("device_class") in ("temperature", "humidity")
        ]
        climates = [s for s in states if s["entity_id"].startswith("climate.")]

        if self._is_home(query):
            query = ""
        area = self._match_area(query, areas) if query else None
        if query and not area:
            found = ent.search(query, sensors, areas, limit=6)
            if not found:
                await self.reply(update, f"Non ho trovato sensori di temperatura per «{esc(query)}».")
                return
            lines = [f"🌡 <b>{esc(query)}</b>", ""] + [self._sensor_line(s, areas) for s in found]
            await self.reply(update, "\n".join(lines))
            return

        pool = [s for s in sensors if not area or areas.get(s["entity_id"]) == area]
        pool += [c for c in climates if area and areas.get(c["entity_id"]) == area]
        if not pool:
            await self.reply(update, "Nessun sensore di temperatura trovato" + (f" in {esc(area)}." if area else "."))
            return

        if area:
            lines = [f"🌡 <b>{esc(area)}</b>", ""] + [self._sensor_line(s, areas) for s in pool]
        else:
            lines = ["🌡 <b>Temperature per stanza</b>", ""]
            for name, group in ent.group_by_area(pool, areas).items():
                if name == "Senza stanza":
                    continue
                lines.append(f"<b>{esc(name)}</b>")
                lines += [f"  {self._sensor_line(s, areas, short=True)}" for s in group]
                lines.append("")
        await self.reply(
            update,
            "\n".join(lines).strip(),
            reply_markup=None if area else self._areas_keyboard(pool, areas, prefix="temp"),
        )

    def _sensor_line(self, s: dict[str, Any], areas: dict[str, str], short: bool = False) -> str:
        """Render one sensor or thermostat as a display line.

        Args:
            s: The entity to render.
            areas: Mapping ``entity_id -> area name``, used for the long form.
            short: When ``True``, print the bare friendly name. Used inside per-room
                blocks, where the room is already in the heading and repeating it in
                every line is noise.

        Returns:
            A formatted HTML line. ``climate`` entities render as
            ``current (target, mode)`` -- with a dash when the thermostat reports no
            current temperature, and the target omitted when it has none, which happens
            while an integration is still initialising. Sensors render as
            ``name: value+unit`` with a thermometer or droplet icon chosen from
            ``device_class``.
        """
        attrs = s.get("attributes", {})
        if s["entity_id"].startswith("climate."):
            cur = attrs.get("current_temperature")
            target = attrs.get("temperature")
            bits = [f"target {esc(target)}°C"] if target is not None else []
            bits.append(esc(s["state"]))
            reading = f"{esc(cur)}°C" if cur is not None else "—"
            return f"🎛 <b>{esc(ent.friendly_name(s))}</b>: {reading} ({', '.join(bits)})"
        unit = attrs.get("unit_of_measurement", "")
        icon = "💧" if attrs.get("device_class") == "humidity" else "🌡"
        name = ent.friendly_name(s) if short else ent.label(s, areas)
        return f"{icon} {esc(name)}: <b>{esc(s['state'])}{esc(unit)}</b>"

    # ------------------------------------------------------------- keyboards
    @staticmethod
    def _is_home(query: str) -> bool:
        """Decide whether a query refers to the whole house.

        Args:
            query: Raw user text.

        Returns:
            ``True`` when the normalized query is one of :data:`HOME_WORDS`
            ("casa", "tutta la casa", "ovunque", "appartamento", "tutto", ...).

        Note:
            Matching is exact on the normalized string, not substring-based: "casa"
            must be the whole query. Otherwise a room legitimately named "Casetta" or a
            sentence mentioning the house in passing would trigger a whole-house
            operation, which is the single most destructive thing the bot can do.
        """
        return ent.normalize(query) in HOME_WORDS

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
            log.debug("Area ambigua per %r: %s — passo alla ricerca per entità", query, partial)
        return None

    def _areas_summary(self, lights: list[dict[str, Any]], areas: dict[str, str]) -> str:
        """Render the "N on out of M" overview that heads the light browser.

        Args:
            lights: The lights to summarise.
            areas: Mapping ``entity_id -> area name``.

        Returns:
            An HTML block with a house-wide total and one line per room. Unreachable
            lights are counted in the totals but not as "on" (see
            :func:`entities.is_on`), so a room whose lights are all unavailable shows
            ``0/3``, which is exactly what the user should see.
        """
        grouped = ent.group_by_area(lights, areas)
        total_on = sum(1 for s in lights if ent.is_on(s))
        lines = [f"💡 <b>Luci</b> — {total_on} accese su {len(lights)}", "", "Scegli una stanza:"]
        for name, group in grouped.items():
            on = sum(1 for s in group if ent.is_on(s))
            lines.append(f"• <b>{esc(name)}</b>: {on}/{len(group)} accese")
        return "\n".join(lines)

    def _areas_keyboard(self, states: list[dict[str, Any]], areas: dict[str, str], prefix: str = "area") -> InlineKeyboardMarkup:
        """Build a keyboard of rooms, two buttons per row.

        Args:
            states: Entities whose rooms should appear. The room list is derived from
                what is actually present, so a room with no relevant entity is not
                offered -- there would be nothing to show behind the button.
            areas: Mapping ``entity_id -> area name``.
            prefix: Callback kind, ``"area"`` to drill into lights or ``"temp"`` to
                drill into sensors. It is what :meth:`_handle_callback` dispatches on.

        Returns:
            The keyboard. Rooms are ordered as :func:`entities.group_by_area` orders
            them -- alphabetically, deterministically -- and ``"Senza stanza"`` is
            dropped: it is not a room the user can think about.
        """
        names = [n for n in ent.group_by_area(states, areas) if n != "Senza stanza"]
        rows, row = [], []
        for name in names:
            row.append(InlineKeyboardButton(name, callback_data=f"{prefix}:{tok(name)}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    def _lights_text(self, title: str, lights: list[dict[str, Any]]) -> str:
        """Render a list of lights with their state.

        Args:
            title: Heading, typically a room name or the query that produced the list.
                It is escaped here, so callers pass raw text.
            lights: The lights to list, in the order they will appear on the keyboard
                that accompanies the message.

        Returns:
            An HTML block, one line per light, ending with the hint that tapping a
            light toggles it.
        """
        lines = [f"💡 <b>{esc(title)}</b>", ""]
        for s in lights:
            lines.append(f"{ent.state_icon(s)} {esc(ent.friendly_name(s))} — {esc(ent.state_text(s))}")
        lines.append("\n<i>Tocca una luce per invertirne lo stato.</i>")
        return "\n".join(lines)

    def _lights_keyboard(self, lights: list[dict[str, Any]]) -> InlineKeyboardMarkup:
        """Build a toggle keyboard for a list of lights.

        One button per light, each carrying the action *opposite* to its current state,
        so a single tap does the obvious thing; the button label shows the current
        state, so the keyboard also reads as a status display. Two bulk buttons and a
        refresh button close the keyboard.

        Names are cut at 28 characters to keep buttons on one line on a phone.

        Args:
            lights: The lights to offer. Only the first :data:`MAX_BUTTONS` are used --
                Telegram accepts more, but a keyboard longer than that is unusable, and
                the accompanying text lists everything anyway.

        Returns:
            The keyboard. All three trailing buttons share one token holding the ids
            joined by ``"|"``, so bulk actions and refresh always act on exactly what
            is displayed.
        """
        rows = [
            [
                InlineKeyboardButton(
                    f"{ent.state_icon(s)} {ent.friendly_name(s)[:28]}",
                    callback_data=f"do:{'off' if ent.is_on(s) else 'on'}:{tok(s['entity_id'])}",
                )
            ]
            for s in lights[:MAX_BUTTONS]
        ]
        ids = tok("|".join(s["entity_id"] for s in lights[:MAX_BUTTONS]))
        rows.append(
            [
                InlineKeyboardButton("🟡 Accendi tutte", callback_data=f"all:on:{ids}"),
                InlineKeyboardButton("⚫ Spegni tutte", callback_data=f"all:off:{ids}"),
            ]
        )
        rows.append([InlineKeyboardButton("🔄 Aggiorna", callback_data=f"refresh:{ids}")])
        return InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------ callbacks
    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for every inline-button tap.

        Authorises the query, then delegates the routing to
        :meth:`_handle_callback`. Home Assistant failures are caught here and shown as
        a Telegram alert rather than being left to the global error handler: a callback
        query must be answered within seconds or the client shows a spinner until it
        times out, and an alert is also the only place a callback failure can be made
        visible without rewriting the message.
        """
        query = update.callback_query
        if not self.authorized(update):
            await query.answer("Non autorizzato", show_alert=True)
            return
        data = query.data or ""
        try:
            await self._handle_callback(query, data)
        except HomeAssistantError as exc:
            await query.answer(str(exc)[:190], show_alert=True)

    async def _handle_callback(self, query, data: str) -> None:
        """Route a callback query to its action.

        ``callback_data`` is ``<kind>:<rest>``, where ``rest`` is one or more
        :func:`tok` tokens:

        ===================== =========================================================
        Payload               Action
        ===================== =========================================================
        ``area:<t>``          Show the lights of a room, with a toggle keyboard.
        ``temp:<t>``          Show the sensors of a room.
        ``do:<on|off>:<t>``   Switch one entity, then re-render the message.
        ``all:<on|off>:<t>``  Switch every entity in the token, then re-render.
        ``refresh:<t>``       Re-read the states and re-render the message.
        ===================== =========================================================

        Every branch answers the query -- Telegram keeps a spinner on the button until
        it is answered -- including the fall-through for payloads this version does not
        recognise, which is how buttons from a previous run of the process behave.

        An expired token is reported as a stale session and asks the user to re-issue
        the command, since the ids it referred to are gone.

        Args:
            query: The ``CallbackQuery`` to act on.
            data: Its payload, passed separately because the caller has already read it.

        Raises:
            ha_client.HomeAssistantError: Propagated to :meth:`on_callback`, which
                turns it into an alert.
        """
        kind, _, rest = data.partition(":")

        if kind in ("area", "temp"):
            area = untok(rest)
            if area is None:
                await query.answer("Sessione scaduta, rilancia il comando.", show_alert=True)
                return
            await query.answer()
            states, areas = await self.snapshot()
            if kind == "temp":
                sensors = [
                    s for s in states
                    if areas.get(s["entity_id"]) == area
                    and s.get("attributes", {}).get("device_class") in ("temperature", "humidity")
                ]
                text = "\n".join([f"🌡 <b>{esc(area)}</b>", ""] + [self._sensor_line(s, areas, short=True) for s in sensors])
                await query.edit_message_text(clip(text) or "Nessun sensore.", parse_mode=ParseMode.HTML)
                return
            lights = [s for s in states if s["entity_id"].startswith("light.") and areas.get(s["entity_id"]) == area]
            await query.edit_message_text(
                clip(self._lights_text(area, lights)),
                parse_mode=ParseMode.HTML,
                reply_markup=self._lights_keyboard(lights),
            )
            return

        if kind in ("do", "all"):
            action, _, token = rest.partition(":")
            raw = untok(token)
            if raw is None:
                await query.answer("Sessione scaduta, rilancia il comando.", show_alert=True)
                return
            ids = raw.split("|")
            turn_on = action == "on"
            await self._call_on_ids(ids, turn_on)
            await query.answer(("🟡 Acceso" if turn_on else "⚫ Spento") + (f" ({len(ids)})" if len(ids) > 1 else ""))
            await self._refresh_message(query, ids)
            return

        if kind == "refresh":
            ids_raw = untok(rest)
            if ids_raw is None:
                await query.answer("Sessione scaduta, rilancia il comando.", show_alert=True)
                return
            await query.answer("Aggiornato")
            await self._refresh_message(query, ids_raw.split("|"))
            return

        # Payload from an older build of the bot: answer anyway so the spinner stops.
        log.debug("Callback sconosciuto: %r", data)
        await query.answer()

    async def _refresh_message(self, query, ids: list[str]) -> None:
        """Re-read the given entities and rewrite the message in place.

        Called after every switch action and by the refresh button, so the keyboard the
        user is looking at reflects the house rather than the moment the message was
        first sent. The state cache is invalidated first: a service call that returned
        a moment ago has already made it stale.

        Entities that have disappeared from Home Assistant since the message was built
        are dropped, and a message left with nothing to show is deliberately not
        touched -- rewriting it into an empty list would destroy the context the user
        was working in.

        Args:
            query: The ``CallbackQuery`` whose message should be rewritten.
            ids: Entity ids to display, in the order they should appear.

        Raises:
            telegram.error.BadRequest: For any edit failure except "message is not
                modified", which is expected whenever the new rendering is identical to
                the old one -- tapping refresh on an unchanged house, for instance --
                and is therefore swallowed. Every other ``BadRequest`` is a real
                problem and is left to the global error handler.
        """
        self.ha.invalidate_states()
        states, areas = await self.snapshot()
        index = {s["entity_id"]: s for s in states}
        shown = [index[i] for i in ids if i in index]
        if not shown:
            return
        title = areas.get(shown[0]["entity_id"], "Luci") if len(shown) > 1 else ent.friendly_name(shown[0])
        try:
            await query.edit_message_text(
                clip(self._lights_text(title, shown)),
                parse_mode=ParseMode.HTML,
                reply_markup=self._lights_keyboard(shown),
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():  # everything else is a real error
                raise
            log.debug("Messaggio identico, modifica ignorata da Telegram")

    # ------------------------------------------------------ natural language
    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for any non-command text message.

        Guards, then hands the text to :meth:`_dispatch_text`.
        """
        if not await self.guard(update):
            return
        await self._dispatch_text(update, (update.effective_message.text or "").strip())

    async def _dispatch_text(self, update: Update, text: str, spoken: bool = False) -> None:
        """Interpret an Italian sentence and run the command it describes.

        The natural-language front end, shared by typed text and by transcribed voice
        messages: both converge here, so the two channels can never drift apart in what
        they understand.

        The parser is a deliberately small ladder of regular expressions over
        :func:`entities.normalize` output -- accents folded, punctuation mostly gone --
        evaluated in this order:

        1. **Temperature**, on words like "temperatura", "gradi", "umidita", "caldo".
           Checked first because "quanti gradi in salone" also contains a room name and
           would otherwise be read as a lighting query.
        2. **Turn on**, on "accendi", "attiva" and their inflections.
        3. **Turn off**, on "spegni", "disattiva".
        4. **What is on**, when the sentence mentions lights *and* "acceso".
        5. **Show lights**, when it mentions lights at all: a named target if one can
           be extracted, otherwise the room overview.
        6. **Nothing matched**: the sentence is quoted back so the user can see how it
           was understood -- especially useful after a transcription -- along with a
           pointer to the commands.

        There is no model and no external service here: the vocabulary is fixed, which
        makes the bot predictable, instant, and functional without an internet
        connection.

        Args:
            update: The update to reply to.
            text: The sentence, raw. Normalisation happens inside.
            spoken: ``True`` when the text came from a voice message, which only
                changes the wording of the "did not understand" reply.
        """
        low = ent.normalize(text)
        if not low:
            await self.reply(update, "Non ho capito. Prova con /luci, /temperatura oppure /help.")
            return

        if re.search(r"\b(temperatur\w*|caldo|freddo|umidit\w*|gradi)\b", low):
            target = re.sub(
                r"\b(che|qual\w*|quanti|quanto|e|c\W?e|ci sono|mi dici|dimmi|dammi|la|il|lo|le|in|nel|nella|del|della|di|a|ad|su|adesso|ora|gradi|temperatura|temperature|umidita|caldo|freddo|fa|per favore)\b",
                " ",
                low,
            )
            await self._temperature(update, re.sub(r"[?!.,]", " ", target).strip())
            return

        if re.search(r"\b(accendi|accende|accendere|attiva|attivare)\b", low):
            await self._switch(update, self._strip_verbs(low), turn_on=True)
            return
        if re.search(r"\b(spegni|spegnere|spenta|disattiva|disattivare)\b", low):
            await self._switch(update, self._strip_verbs(low), turn_on=False)
            return
        if re.search(r"\b(luci|luce|lampad\w*)\b", low) and re.search(r"\b(acces\w+)\b", low):
            await self._lights_on(update)
            return
        if re.search(r"\b(luci|luce|lampad\w*)\b", low):
            states, areas = await self.snapshot()
            lights = [s for s in states if s["entity_id"].startswith("light.")]
            target = self._strip_verbs(low)
            found = ent.search(target, lights, areas, domains=("light",), limit=MAX_BUTTONS) if target else []
            if found:
                await self.reply(
                    update,
                    self._lights_text(target, found),
                    reply_markup=self._lights_keyboard(found),
                )
            else:
                await self.reply(
                    update,
                    self._areas_summary(lights, areas),
                    reply_markup=self._areas_keyboard(lights, areas),
                )
            return

        hint = " Ripeti pure il vocale." if spoken else ""
        await self.reply(update, f"Non ho capito «{esc(text)}».{hint}\nProva con /luci, /temperatura oppure /help.")

    # ------------------------------------------------------- voice messages
    async def on_voice(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Transcribe a voice message and execute it as if it had been typed.

        Accepts voice notes, audio files and video notes. The clip is downloaded from
        Telegram, sent to Home Assistant's speech-to-text engine, echoed back to the
        user, and then run through the same :meth:`_dispatch_text` as typed text.

        Echoing the transcription before acting is a deliberate design choice: speech
        recognition is imperfect, and seeing "spegni la cucina" when you said "spegni
        la camera" explains a surprising outcome instantly. The typing action shown
        while the clip uploads is the only feedback available during what can be a
        couple of seconds of network work.

        Three refusals, each with its own explanation: no STT engine configured, a clip
        larger than :data:`MAX_VOICE_BYTES`, or a transcription that came back empty.
        None of them is an error -- text commands remain available throughout.

        Note:
            Transcription is charged to the configured provider, and the size cap is
            the only thing standing between an accidental long recording and a large
            bill, so it is enforced before the clip is downloaded.
        """
        if not await self.guard(update):
            return
        message = update.effective_message
        media = message.voice or message.audio or message.video_note
        if media is None:
            return
        if not self.stt_entity:
            await self.reply(
                update,
                "🎙 Nessun motore speech-to-text configurato in Home Assistant.\n"
                "Aggiungi un'integrazione STT (Assist) oppure imposta <code>HA_STT_ENTITY</code> nel .env.",
            )
            return
        if getattr(media, "file_size", 0) and media.file_size > MAX_VOICE_BYTES:
            await self.reply(update, f"🎙 Vocale troppo lungo (oltre {MAX_VOICE_BYTES // (1024 * 1024)} MB, circa 5 minuti).")
            return

        await message.chat.send_action(ChatAction.TYPING)
        try:
            audio_file = await media.get_file()
            audio = bytes(await audio_file.download_as_bytearray())
            fmt, codec = self._audio_format(getattr(media, "mime_type", None))
            text = await self.ha.speech_to_text(
                audio, self.stt_entity, language=self.stt_language, audio_format=fmt, codec=codec
            )
        except HomeAssistantError as exc:
            log.warning("STT fallito: %s", exc)
            await self.reply(update, f"🎙 Non sono riuscito a trascrivere il vocale.\n<i>{esc(exc)}</i>")
            return

        if not text:
            await self.reply(update, "🎙 Non ho sentito nulla di comprensibile, riprova.")
            return

        log.info("Vocale trascritto: %r", text)
        await self.reply(update, f"🎙 <i>«{esc(text)}»</i>")
        await self._dispatch_text(update, text, spoken=True)

    @staticmethod
    def _audio_format(mime_type: str | None) -> tuple[str, str]:
        """Derive the container and codec to declare for a Telegram clip.

        Args:
            mime_type: The ``mime_type`` Telegram reports, if any.

        Returns:
            A ``(format, codec)`` pair: ``("wav", "pcm")`` for a forwarded WAV file,
            ``("ogg", "opus")`` otherwise. Ogg/Opus is what voice notes and video notes
            always are, and it is what Home Assistant's STT providers accept natively,
            which is why Hassgram needs no ffmpeg and no transcoding step.
        """
        if mime_type and "wav" in mime_type:
            return "wav", "pcm"
        return "ogg", "opus"

    @staticmethod
    def _strip_verbs(low: str) -> str:
        """Reduce a sentence to the thing it talks about.

        Removes verbs, articles, prepositions, politeness and filler, leaving the
        target for :func:`entities.search` to match: "accendi la luce dello studio"
        becomes "studio".

        Quantifiers ("tutto", "tutte") are stripped along with everything else, which
        would silently turn "accendi tutto" into an empty query. When the result is
        empty but the original sentence contained a whole-house word, ``"casa"`` is
        returned instead, so the sentence keeps the meaning it obviously had.

        Args:
            low: The sentence, already normalized by :func:`entities.normalize`.

        Returns:
            The residual target, or ``"casa"`` for a whole-house sentence, or the empty
            string when nothing usable is left -- which the caller treats as "show the
            room overview" rather than as an error.

        Note:
            The word list is Italian and closed. Extending it is the usual way to teach
            the bot a new phrasing, and it is safe to do so as long as the added words
            can never be part of a room or entity name.
        """
        cleaned = re.sub(
            r"\b(accendi|accende|accendere|attiva|attivare|spegni|spegnere|disattiva|disattivare|"
            r"la|le|lo|il|l|luce|luci|lampada|lampade|del|della|dello|dei|delle|di|in|nel|nella|"
            r"al|alla|allo|a|dell|nell|all|sull|per favore|grazie|mi|puoi|potresti|tutte|tutti|"
            r"stanza|adesso|ora|tutta|tutto)\b",
            " ",
            low,
        )
        target = re.sub(r"\s+", " ", re.sub(r"[?!.,;:]", " ", cleaned)).strip()
        # "accendi tutto", "spegni tutte le luci": the quantifiers were stripped above,
        # but the sentence was about the whole house.
        if not target and re.search(r"\b(tutt\w*|casa|ovunque|appartamento)\b", low):
            return "casa"
        return target


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler: turn any unhandled exception into a reply.

    Registered on the ``Application``, so it catches whatever the handlers do not.
    Without it, a Home Assistant that is down or a bug in a handler would produce a
    log line and complete silence in the chat, which reads to the user as a bot
    that has stopped working for no reason.

    Home Assistant failures get a specific message including the underlying error
    -- "connection refused" and "401" tell the user immediately whether the
    instance is down or the token has expired -- while anything else gets a generic
    apology, since its message is not meant for users. The full traceback goes to
    the log either way.

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
    log.error("Errore non gestito", exc_info=ctx.error)
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    if isinstance(ctx.error, HomeAssistantError):
        text = f"⚠️ Home Assistant non risponde.\n<i>{esc(ctx.error)}</i>"
    else:
        text = "⚠️ Qualcosa è andato storto, riprova."
    try:
        await message.reply_text(clip(text), parse_mode=ParseMode.HTML)
    except Exception:  # if Telegram itself is failing there is nothing left to try
        log.debug("Impossibile notificare l'errore all'utente", exc_info=True)


async def post_init(app: Application) -> None:
    """Startup hook: verify Home Assistant and choose a transcription engine.

    Runs after the ``Application`` is built but before polling starts, so a bad URL
    or a revoked token stops the process immediately with a clear log message
    instead of surfacing later as commands that mysteriously do nothing.

    Args:
        app: The application, whose ``bot_data`` carries the :class:`HassBot`.

    Raises:
        ha_client.HomeAssistantError: If the instance is unreachable or rejects the
            token. Failing here is intentional; under systemd the unit restarts and
            retries, which is the desired behaviour when the bot boots before Home
            Assistant does.
    """
    bot: HassBot = app.bot_data["hass"]
    msg = await bot.ha.ping()
    log.info("Home Assistant: %s", msg)
    await bot.discover_stt()


async def post_shutdown(app: Application) -> None:
    """Shutdown hook: close the Home Assistant HTTP session.

    Args:
        app: The application, whose ``bot_data`` carries the :class:`HassBot`.
    """
    await app.bot_data["hass"].ha.aclose()


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
    ``STT_LANGUAGE``                        Transcription language, default
                                            ``it-IT``.
    ======================================= ====================================

    Chat ids are extracted with a regular expression rather than split on commas,
    so extra spaces, trailing separators and quotes are all tolerated; the pattern
    keeps the leading minus that group chat ids carry. An empty allow-list is
    permitted but logged as a warning, because it means anyone who finds the bot
    controls the house.

    Handler order matters: commands are registered first, and the catch-all text
    handler explicitly excludes commands, so an unknown ``/command`` is not fed to
    the natural-language parser. The error handler is registered last.

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
        sys.exit("Mancano TELEGRAM_BOT_TOKEN, HOME_ASSISTANT_API_URL o HOME_ASSISTANT_API_ACCESS_TOKEN nel .env")

    allowed = {int(c) for c in re.findall(r"-?\d+", os.getenv("TELEGRAM_CHAT_ID", ""))}
    if not allowed:
        log.warning("TELEGRAM_CHAT_ID non impostato: il bot risponderà a chiunque.")

    ha = HomeAssistantClient(ha_url, ha_token)
    hass = HassBot(
        ha,
        allowed,
        stt_entity=os.getenv("HA_STT_ENTITY") or None,
        stt_language=os.getenv("STT_LANGUAGE", "it-IT"),
    )

    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data["hass"] = hass

    app.add_handler(CommandHandler(["start", "help", "aiuto"], hass.cmd_start))
    app.add_handler(CommandHandler(["luci", "lights"], hass.cmd_lights))
    app.add_handler(CommandHandler("accese", hass.cmd_on_now))
    app.add_handler(CommandHandler(["accendi", "on"], hass.cmd_on))
    app.add_handler(CommandHandler(["spegni", "off"], hass.cmd_off))
    app.add_handler(CommandHandler(["temperatura", "temp"], hass.cmd_temperature))
    app.add_handler(CommandHandler(["stato", "state"], hass.cmd_state))
    app.add_handler(CallbackQueryHandler(hass.on_callback))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, hass.on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, hass.on_text))
    app.add_error_handler(on_error)

    log.info("Bot avviato (chat autorizzate: %s)", allowed or "tutte")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
