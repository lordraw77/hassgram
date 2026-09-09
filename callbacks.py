"""Inline buttons: authorise a tap, route its payload, act, re-render.

Every button the bot draws carries a ``<kind>:<token>`` payload, and every tap
lands in :func:`on_callback`. Routing them is a self-contained concern -- it
shares no code with the command handlers, only the actions they both call -- so
it lives in its own module.

Handlers take the bot as their first argument rather than being methods on it:
what each one needs from :class:`~bot.HassBot` is then visible in the signature,
and the module stays importable without one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import entities as ent
import i18n
import views
from ha_client import HomeAssistantError
from i18n import t
from views import clip, esc, ha_error_text, untok

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for type checkers only
    from bot import HassBot

log = logging.getLogger("hassgram.callbacks")


async def on_callback(bot: "HassBot", update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for every inline-button tap.

    Authorises the query, then delegates the routing to
    :func:`handle`. Home Assistant failures are caught here and shown as
    a Telegram alert rather than being left to the global error handler: a callback
    query must be answered within seconds or the client shows a spinner until it
    times out, and an alert is also the only place a callback failure can be made
    visible without rewriting the message.
    """
    query = update.callback_query
    lang = bot.lang_of(update)
    if not bot.authorized(update):
        await query.answer(t(lang, "unauthorized_toast"), show_alert=True)
        return
    data = query.data or ""
    try:
        await handle(bot, query, data, lang)
    except HomeAssistantError as exc:
        await query.answer(ha_error_text(lang, exc)[:190], show_alert=True)


async def resolve_token(query, token: str, lang: str) -> str | None:
    """Resolve a ``callback_data`` token, answering the query when it is gone.

    Every callback branch starts this way, and the failure is always handled
    identically: a token that has fallen out of the LRU -- evicted, or left over
    from a previous run of the process -- is a stale session, not a bug.

    Args:
        query: The ``CallbackQuery`` being handled.
        token: The token extracted from the payload.
        lang: Language for the alert.

    Returns:
        The stored value, or ``None`` after having already told the user the
        session expired. A ``None`` return means the caller must simply return:
        the query has been answered and the spinner stopped.
    """
    value = untok(token)
    if value is None:
        await query.answer(t(lang, "session_expired"), show_alert=True)
    return value


async def handle(bot: "HassBot", query, data: str, lang: str = i18n.DEFAULT_LANG) -> None:
    """Route a callback query to its action.

    ``callback_data`` is ``<kind>:<rest>``, where ``rest`` is one or more
    :func:`views.tok` tokens:

    ===================== =========================================================
    Payload               Action
    ===================== =========================================================
    ``area:<t>``          Show the lights of a room, with a toggle keyboard.
    ``temp:<t>``          Show the sensors of a room.
    ``do:<on|off>:<t>``   Switch one entity, then re-render the message.
    ``all:<on|off>:<t>``  Switch every entity in the token, then re-render.
    ``refresh:<t>``       Re-read the states and re-render the message.
    ``run:<t>``           Run one script or automation. The message is
                          left untouched: the keyboard is a menu, not a status
                          display, so re-rendering it would only take the other
                          options away.
    ===================== =========================================================

    Every branch answers the query -- Telegram keeps a spinner on the button until
    it is answered -- including the fall-through for payloads this version does not
    recognise, which is how buttons from a previous run of the process behave.

    An expired token is reported as a stale session and asks the user to re-issue
    the command, since the ids it referred to are gone.

    Args:
        query: The ``CallbackQuery`` to act on.
        data: Its payload, passed separately because the caller has already read it.
        lang: Language to answer in. Button taps carry no language of their
            own, so this is the chat's remembered preference -- the same one
            in force when the keyboard was rendered.

    Raises:
        ha_client.HomeAssistantError: Propagated to :func:`on_callback`, which
            turns it into an alert.
    """
    kind, _, rest = data.partition(":")

    if kind in ("area", "temp"):
        area = await resolve_token(query, rest, lang)
        if area is None:
            return
        await query.answer()
        states, areas = await bot.snapshot()
        if kind == "temp":
            sensors = bot._temp_sensors(states, areas, area=area)
            if sensors:
                lines = [t(lang, "temp_title", title=esc(area)), ""]
                lines += [views.sensor_line(s, areas, lang, short=True) for s in sensors]
                text = "\n".join(lines)
            else:
                text = t(lang, "no_sensors")
            await query.edit_message_text(clip(text, lang), parse_mode=ParseMode.HTML)
            return
        lights = bot._lights(states, areas, area=area)
        await query.edit_message_text(
            clip(views.lights_text(area, lights, lang), lang),
            parse_mode=ParseMode.HTML,
            reply_markup=views.lights_keyboard(lights, lang),
        )
        return

    if kind in ("do", "all"):
        action, _, token = rest.partition(":")
        raw = await resolve_token(query, token, lang)
        if raw is None:
            return
        ids = raw.split("|")
        turn_on = action == "on"
        await bot._call_on_ids(ids, turn_on)
        toast = t(lang, "toast_on" if turn_on else "toast_off")
        await query.answer(toast + (f" ({len(ids)})" if len(ids) > 1 else ""))
        await refresh_message(bot, query, ids, lang)
        return

    if kind == "run":
        entity_id = await resolve_token(query, rest, lang)
        if entity_id is None:
            return
        await bot._run_ids([entity_id])
        await query.answer(t(lang, "toast_run"))
        return

    if kind == "refresh":
        ids_raw = await resolve_token(query, rest, lang)
        if ids_raw is None:
            return
        await query.answer(t(lang, "toast_refreshed"))
        await refresh_message(bot, query, ids_raw.split("|"), lang)
        return

    # Payload from an older build of the bot: answer anyway so the spinner stops.
    log.debug("Unknown callback payload: %r", data)
    await query.answer()


async def refresh_message(bot: "HassBot", query, ids: list[str], lang: str = i18n.DEFAULT_LANG) -> None:
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
        lang: Language to re-render in.

    Raises:
        telegram.error.BadRequest: For any edit failure except "message is not
            modified", which is expected whenever the new rendering is identical to
            the old one -- tapping refresh on an unchanged house, for instance --
            and is therefore swallowed. Every other ``BadRequest`` is a real
            problem and is left to the global error handler.
    """
    bot.ha.invalidate_states()
    states, areas = await bot.snapshot()
    index = {s["entity_id"]: s for s in states}
    shown = [index[i] for i in ids if i in index]
    if not shown:
        return
    fallback = t(lang, "lights_title")
    title = areas.get(shown[0]["entity_id"], fallback) if len(shown) > 1 else ent.friendly_name(shown[0])
    try:
        await query.edit_message_text(
            clip(views.lights_text(title, shown, lang), lang),
            parse_mode=ParseMode.HTML,
            reply_markup=views.lights_keyboard(shown, lang),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():  # everything else is a real error
            raise
        log.debug("Identical message, edit ignored by Telegram")
