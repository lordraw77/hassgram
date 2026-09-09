"""Rendering: every string and every inline keyboard the bot sends.

This module is pure. Nothing here performs I/O, holds bot state or knows what a
:class:`~bot.HassBot` is: a function takes Home Assistant states and a language
and returns text or a keyboard. That is what makes the presentation layer
testable without a Telegram or a Home Assistant in the loop, and it is why this
module never imports :mod:`bot`.

It also owns the primitives every rendered string goes through -- :func:`esc`,
which escapes Home Assistant's text for Telegram's HTML parse mode, and
:func:`clip`, which keeps a message inside Telegram's size cap -- plus the
:func:`tok` / :func:`untok` pair that lets a button carry more than the 64 bytes
``callback_data`` allows.
"""

from __future__ import annotations

import hashlib
import html
from collections import OrderedDict
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import entities as ent
import i18n
from constants import (
    MAX_BUTTONS,
    MAX_MESSAGE_CHARS,
    MAX_RUN_PAGES,
    MAX_TOKENS,
    RUN_DOMAINS,
    RUN_ICON_DEFAULT,
    RUN_ICONS,
)
from ha_client import HomeAssistantError
from i18n import t


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


def ha_error_text(lang: str, exc: HomeAssistantError) -> str:
    """Render a Home Assistant failure as a localised line.

    :mod:`ha_client` cannot phrase its own errors: it has no idea which chat
    triggered the request, and therefore which of the two languages to use. It
    raises a structured :class:`ha_client.HomeAssistantError` instead, and this is
    where ``kind``/``status``/``detail`` become a sentence.

    Args:
        lang: Language to render in.
        exc: The error. An exception raised by something other than the client --
            it is typed loosely on purpose, since :func:`on_error` sees whatever
            was raised -- falls back to its own ``str()`` as the detail.

    Returns:
        A plain sentence with no icon and no markup, meant to be interpolated into
        ``ha_down`` or ``stt_failed`` as ``{error}``, or shown on its own in a
        callback alert. The caller escapes it when the destination is HTML.
    """
    kind = getattr(exc, "kind", "generic")
    key = f"ha_error_{kind}" if f"ha_error_{kind}" in i18n.MESSAGES else "ha_error_generic"
    return t(lang, key, detail=getattr(exc, "detail", None) or str(exc), status=getattr(exc, "status", ""))


def clip(text: str, lang: str = i18n.DEFAULT_LANG, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Shorten a message so Telegram will accept it, keeping the HTML valid.

    Telegram rejects anything over 4096 characters, and lists such as "every light
    in the house" or "every sensor per room" pass that on a large installation.
    Truncating at an arbitrary offset would risk cutting a message in the middle of
    a ``<b>`` tag and getting the whole message rejected for malformed markup, so
    the cut is made at the last newline that fits: every line the bot emits is
    self-contained HTML, which makes line boundaries safe cut points.

    Args:
        text: The message body, HTML included.
        lang: Language of the truncation notice.
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
    return (text[:cut] if cut > 0 else text[:limit]) + "\n" + t(lang, "truncated")


def runnable_entry(state: dict[str, Any], lang: str = i18n.DEFAULT_LANG) -> str:
    """Render one entity as its two lines of the listing.

    Args:
        state: The entity.
        lang: Language for the "disabled" marker.

    Returns:
        The name -- marked when it is a disabled automation, since a disabled
        automation can still be triggered by hand and the user should know that
        is what they are doing -- above its entity id in a code span. Both
        values come from Home Assistant, so both are escaped.
    """
    entity_id = state["entity_id"]
    off = entity_id.startswith("automation.") and not ent.is_on(state)
    suffix = f" <i>({t(lang, 'automation_disabled')})</i>" if off else ""
    return f"\u2022 {esc(ent.friendly_name(state))}{suffix}\n  <code>{esc(entity_id)}</code>"


def runnables_pages(
    runnables: list[dict[str, Any]],
    lang: str = i18n.DEFAULT_LANG,
    limit: int = MAX_MESSAGE_CHARS,
    per_page: int = MAX_BUTTONS,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Render the listing of everything that can be run, split into messages.

    Grouped by domain rather than by room, which is the only grouping that means
    anything here: a script and an automation are different kinds of thing,
    and almost none of them are assigned to an area.

    A house with a few dozen automations produces a listing past Telegram's
    4096-character cap, and past what a single inline keyboard can usefully
    hold. Rather than truncating it -- which is what :func:`clip` would do,
    silently hiding half the catalogue -- the listing is split into pages, each
    sent as its own message with its own keyboard.

    A page is closed on whichever limit is reached first:

    * ``limit`` characters, so Telegram accepts the message;
    * ``per_page`` entities, so the keyboard stays usable and, more importantly,
      so **every entity named in a page has a button in that page**. Text and
      keyboard are built from the same list, which is what keeps them in step
      however the catalogue is split.

    A domain interrupted by a page break repeats its heading on the next page,
    marked as a continuation, so no page opens with an unlabelled list.

    Args:
        runnables: The entities to list, already sorted by :meth:`bot.HassBot._runnables`.
        lang: Language for the headings.
        limit: Character budget per page.
        per_page: Maximum entities per page.

    Returns:
        ``[(text, entities)]``, one pair per message to send, in order. The
        entities are exactly those named in that page's text. Never empty: a
        caller with an empty catalogue is expected to have said so already.

        At most :data:`MAX_RUN_PAGES` pages. Beyond that the listing does stop,
        but it says so and says how many entities it did not name -- the one
        thing :func:`clip` would not have done.
    """
    pages: list[tuple[str, list[dict[str, Any]]]] = []
    lines: list[str] = [t(lang, "runnables_title"), ""]
    shown: list[dict[str, Any]] = []
    size = sum(len(line) + 1 for line in lines)

    def flush() -> None:
        """Close the current page; the next one starts empty."""
        nonlocal lines, shown, size
        if shown:
            pages.append(("\n".join(lines), shown))
        lines, shown, size = [], [], 0

    def add(line: str) -> None:
        """Append a line, keeping the running page length in step with it."""
        nonlocal size
        lines.append(line)
        size += len(line) + 1  # the newline that will join it to the line above

    for domain in RUN_DOMAINS:
        group = [s for s in runnables if s["entity_id"].startswith(f"{domain}.")]
        if not group:
            continue
        add(f"{RUN_ICONS[domain]} <b>{t(lang, f'domain_{domain}')}</b>")
        for state in group:
            entry = runnable_entry(state, lang)
            if shown and (size + len(entry) > limit or len(shown) >= per_page):
                flush()
                add(t(lang, "domain_continued", icon=RUN_ICONS[domain],
                      domain=t(lang, f"domain_{domain}")))
            add(entry)
            shown.append(state)
        add("")

    add(t(lang, "run_tap_hint"))
    flush()

    if len(pages) > MAX_RUN_PAGES:
        listed = sum(len(page) for _, page in pages[:MAX_RUN_PAGES])
        pages = pages[:MAX_RUN_PAGES]
        text, page = pages[-1]
        pages[-1] = (text + "\n" + t(lang, "run_list_capped", count=len(runnables) - listed), page)
    return pages


def run_keyboard(runnables: list[dict[str, Any]], lang: str = i18n.DEFAULT_LANG) -> InlineKeyboardMarkup:
    """Build a keyboard that runs one entity per button.

    Unlike :func:`lights_keyboard` there is no bulk button and no refresh
    button: running everything at once is never the intent, and there is no
    state to refresh -- a script is not something that is "on".

    Args:
        runnables: The entities to offer, capped at :data:`MAX_BUTTONS`.
        lang: Unused today, accepted so the signature matches the other keyboard
            builders and stays stable if a trailing button is ever added.

    Returns:
        The keyboard, one ``run:<token>`` button per entity.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{RUN_ICONS.get(s['entity_id'].split('.')[0], RUN_ICON_DEFAULT)} {ent.friendly_name(s)[:28]}",
                    callback_data=f"run:{tok(s['entity_id'])}",
                )
            ]
            for s in runnables[:MAX_BUTTONS]
        ]
    )


def sensor_line(s: dict[str, Any], areas: dict[str, str], lang: str, short: bool = False) -> str:
    """Render one sensor or thermostat as a display line.

    Args:
        s: The entity to render.
        areas: Mapping ``entity_id -> area name``, used for the long form.
        lang: Language for the thermostat's target label.
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
        bits = [t(lang, "target_label", value=esc(target))] if target is not None else []
        bits.append(esc(s["state"]))
        reading = f"{esc(cur)}°C" if cur is not None else "—"
        return f"🎛 <b>{esc(ent.friendly_name(s))}</b>: {reading} ({', '.join(bits)})"
    unit = attrs.get("unit_of_measurement", "")
    icon = "💧" if attrs.get("device_class") == "humidity" else "🌡"
    name = ent.friendly_name(s) if short else ent.label(s, areas)
    return f"{icon} {esc(name)}: <b>{esc(s['state'])}{esc(unit)}</b>"


def area_name(name: str, lang: str) -> str:
    """Render a grouping key from :func:`entities.group_by_area` for display.

    Real room names come from Home Assistant and are shown as they are; only
    the :data:`entities.NO_AREA` sentinel needs translating.

    Args:
        name: The grouping key.
        lang: Language to render the sentinel in.

    Returns:
        The room name, or the localised "no room" label.
    """
    return t(lang, "no_area") if name == ent.NO_AREA else name


def areas_summary(lights: list[dict[str, Any]], areas: dict[str, str], lang: str) -> str:
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
    lines = [
        t(lang, i18n.plural("lights_summary_title", total_on), on=total_on, total=len(lights)),
        "",
        t(lang, "choose_room"),
    ]
    for name, group in grouped.items():
        on = sum(1 for s in group if ent.is_on(s))
        counts = t(lang, i18n.plural("room_counts", on), on=on, total=len(group))
        lines.append(f"• <b>{esc(area_name(name, lang))}</b>: {counts}")
    return "\n".join(lines)


def areas_keyboard(states: list[dict[str, Any]], areas: dict[str, str], prefix: str = "area") -> InlineKeyboardMarkup:
    """Build a keyboard of rooms, two buttons per row.

    Args:
        states: Entities whose rooms should appear. The room list is derived from
            what is actually present, so a room with no relevant entity is not
            offered -- there would be nothing to show behind the button.
        areas: Mapping ``entity_id -> area name``.
        prefix: Callback kind, ``"area"`` to drill into lights or ``"temp"`` to
            drill into sensors. It is what :func:`callbacks.handle` dispatches on.

    Returns:
        The keyboard. Rooms are ordered as :func:`entities.group_by_area` orders
        them -- alphabetically, deterministically -- and :data:`entities.NO_AREA`
        is dropped: it is not a room the user can think about.
    """
    names = [n for n in ent.group_by_area(states, areas) if n != ent.NO_AREA]
    rows, row = [], []
    for name in names:
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}:{tok(name)}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def lights_text(title: str, lights: list[dict[str, Any]], lang: str) -> str:
    """Render a list of lights with their state.

    Args:
        title: Heading, typically a room name or the query that produced the list.
            It is escaped here, so callers pass raw text.
        lights: The lights to list, in the order they will appear on the keyboard
            that accompanies the message.
        lang: Language for the state words and the hint.

    Returns:
        An HTML block, one line per light, ending with the hint that tapping a
        light toggles it.
    """
    lines = [f"💡 <b>{esc(title)}</b>", ""]
    for s in lights:
        lines.append(f"{ent.state_icon(s)} {esc(ent.friendly_name(s))} — {esc(ent.state_text(s, lang))}")
    lines.append("\n" + t(lang, "tap_to_toggle"))
    return "\n".join(lines)


def lights_keyboard(lights: list[dict[str, Any]], lang: str = i18n.DEFAULT_LANG) -> InlineKeyboardMarkup:
    """Build a toggle keyboard for a list of lights.

    One button per light, each carrying the action *opposite* to its current state,
    so a single tap does the obvious thing; the button label shows the current
    state, so the keyboard also reads as a status display. Two bulk buttons and a
    refresh button close the keyboard.

    Names are cut at 28 characters to keep buttons on one line on a phone.

    Args:
        lang: Language for the three trailing buttons.
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
            InlineKeyboardButton(t(lang, "turn_all_on"), callback_data=f"all:on:{ids}"),
            InlineKeyboardButton(t(lang, "turn_all_off"), callback_data=f"all:off:{ids}"),
        ]
    )
    rows.append([InlineKeyboardButton(t(lang, "refresh"), callback_data=f"refresh:{ids}")])
    return InlineKeyboardMarkup(rows)


def choice_keyboard(
    found: list[dict[str, Any]],
    areas: dict[str, str],
    turn_on: bool,
    lang: str = i18n.DEFAULT_LANG,
) -> InlineKeyboardMarkup:
    """Build the "which one did you mean?" keyboard for an ambiguous switch.

    Offered when a query matched several entities and none of them matched it
    exactly. Unlike :func:`lights_keyboard`, every button here carries the *same*
    action -- the one the user asked for -- because the question being answered is
    which entity, not what to do to it. The label shows each candidate with its room
    (:func:`entities.label`), since ambiguity is usually two lamps with the same name
    in different rooms.

    Args:
        found: The candidates, already ranked and capped by the caller.
        areas: Mapping ``entity_id -> area name``, for the disambiguating labels.
        turn_on: The action every button applies.
        lang: Language for the trailing "all of them" button.

    Returns:
        The keyboard: one button per candidate, then a bulk button whose single
        token holds every candidate id, so "all of them" acts on exactly what is
        displayed.
    """
    action = "on" if turn_on else "off"
    rows = [
        [
            InlineKeyboardButton(
                f"{ent.state_icon(s)} {ent.label(s, areas)}",
                callback_data=f"do:{action}:{tok(s['entity_id'])}",
            )
        ]
        for s in found
    ]
    all_ids = tok("|".join(s["entity_id"] for s in found))
    rows.append(
        [
            InlineKeyboardButton(
                t(lang, "all_button", count=len(found)),
                callback_data=f"all:{action}:{all_ids}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)
