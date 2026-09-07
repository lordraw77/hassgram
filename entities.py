"""Search, ranking and presentation helpers for Home Assistant entities.

This is the domain layer of Hassgram: pure functions over plain dictionaries,
with no I/O, no Telegram types and no knowledge of the HTTP client. Everything
here is synchronous and side-effect free, which makes it the natural place to
put logic that needs to be reasoned about or tested in isolation.

Vocabulary used throughout the module:

``state``
    One entry of the ``GET /api/states`` payload, e.g.::

        {"entity_id": "light.studio",
         "state": "on",
         "attributes": {"friendly_name": "Luce studio",
                        "device_class": "temperature",
                        "unit_of_measurement": "°C"}}

    Only ``entity_id`` is assumed to be always present; every other key is
    accessed defensively, because Home Assistant omits attributes freely and a
    restarting integration can publish half-populated states.

``entity_id``
    ``<domain>.<object_id>``, e.g. ``light.studio``. The domain prefix is what
    decides which service can be called on the entity.

``areas``
    Mapping ``entity_id -> area name`` (``{"light.studio": "Studio"}``) produced
    by :meth:`ha_client.HomeAssistantClient.areas`. The REST API does not expose
    the area registry, so this mapping is rendered by a Jinja template on the
    Home Assistant side and is the only link between an entity and its room.
    Entities with no area are simply absent from the mapping.

User-facing strings returned by this module are Italian on purpose: they are
sent verbatim to Telegram, which is an Italian-language bot.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

OFF_STATES = {"off", "unavailable", "unknown", "none", ""}


def normalize(text: str) -> str:
    """Fold a string into the canonical form used for every comparison.

    The transformation chain is, in order:

    1. Unicode NFKD decomposition, then removal of combining marks, so accented
       letters lose their diacritics (``è`` becomes ``e``, ``perché`` becomes
       ``perche``). This matters because speech-to-text output and hand-typed
       messages disagree constantly about accents.
    2. Lowercasing.
    3. Replacement of ``_``, ``.`` and ``'`` with spaces, which flattens the three
       different shapes the same room can arrive in: ``camera_da_letto`` (object
       id), ``light.camera`` (entity id) and ``l'accendi`` (elided article).
    4. Whitespace collapsing and stripping.

    Args:
        text: Any string; ``None`` is tolerated and treated as an empty string,
            because callers routinely pass ``state.get(...)`` results straight in.

    Returns:
        The normalized string, possibly empty.

    Examples:
        >>> normalize("Camera_da_Letto")
        'camera da letto'
        >>> normalize("light.studio")
        'light studio'
        >>> normalize("Perché l'accendi?")
        'perche l accendi?'

    Note:
        Punctuation other than ``.`` and ``'`` survives: callers that build regular
        expressions over the result strip ``?``, ``!`` and commas themselves.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("_", " ").replace(".", " ").replace("'", " ")
    return re.sub(r"\s+", " ", text).strip()


def friendly_name(state: dict[str, Any]) -> str:
    """Return the human-readable name of an entity.

    Args:
        state: A Home Assistant state dictionary.

    Returns:
        ``attributes.friendly_name`` when Home Assistant provides one, otherwise
        the raw ``entity_id`` as a last resort so the caller always has something
        printable. An entity that has just been added, or whose integration is
        still loading, frequently has no friendly name.
    """
    return state.get("attributes", {}).get("friendly_name") or state["entity_id"]


def label(state: dict[str, Any], areas: dict[str, str]) -> str:
    """Return a display name disambiguated by room when that adds information.

    Many installations name entities after the room they live in ("Luce studio"
    in area "Studio"), and repeating the area would produce "Luce studio
    (Studio)". The area is therefore appended only when its normalized form is
    not already contained in the normalized entity name.

    Args:
        state: A Home Assistant state dictionary.
        areas: Mapping ``entity_id -> area name``; entities missing from it are
            rendered without any suffix.

    Returns:
        Either ``"Name"`` or ``"Name (Area)"``.

    Examples:
        Given ``areas = {"light.studio": "Studio", "light.x": "Cucina"}``,
        an entity named "Luce studio" renders as ``Luce studio`` while one named
        "Faretto" renders as ``Faretto (Cucina)``.
    """
    area = areas.get(state["entity_id"])
    name = friendly_name(state)
    return f"{name} ({area})" if area and normalize(area) not in normalize(name) else name


def is_on(state: dict[str, Any]) -> bool:
    """Report whether an entity counts as active.

    The check is deliberately negative -- anything that is not in
    :data:`OFF_STATES` is considered on -- so that domains with richer state
    machines behave sensibly: a ``climate`` entity in ``heat`` and a ``media_player``
    in ``playing`` both count as on without needing a per-domain table.

    Args:
        state: A Home Assistant state dictionary.

    Returns:
        ``True`` when the raw state is not one of ``off``, ``unavailable``,
        ``unknown``, ``none`` or the empty string.

    Warning:
        ``unavailable`` and ``unknown`` are folded into "off" here. That is right
        for counters such as "3 lights on out of 12", but wrong when deciding what
        to act on: an unreachable light must be *excluded*, not turned off. Bulk
        operations therefore filter on the raw state instead of using this
        function -- see ``HassBot._bulk_targets`` in :mod:`bot`.
    """
    return state.get("state") not in OFF_STATES


def state_icon(state: dict[str, Any]) -> str:
    """Pick the emoji that represents an entity's state on a button or in a list.

    Args:
        state: A Home Assistant state dictionary.

    Returns:
        ``"⚠️"`` for ``unavailable``/``unknown`` (the entity exists but cannot be
        reached), ``"🟡"`` when on, ``"⚫"`` when off. Unlike :func:`is_on`, this
        function keeps the unreachable case visible, so the user can tell a light
        that is off from one that is not answering.
    """
    raw = state.get("state")
    if raw in ("unavailable", "unknown"):
        return "⚠️"
    return "🟡" if is_on(state) else "⚫"


def state_text(state: dict[str, Any]) -> str:
    """Translate a raw state into the Italian word shown to the user.

    Args:
        state: A Home Assistant state dictionary.

    Returns:
        The Italian label for the four states the bot displays in light lists
        (``on``, ``off``, ``unavailable``, ``unknown``). Any other value -- a
        temperature reading, a ``climate`` mode, a media state -- is returned
        unchanged, which is why sensor values can flow through this function
        untouched.
    """
    raw = state.get("state")
    return {"on": "accesa", "off": "spenta", "unavailable": "non disponibile", "unknown": "sconosciuto"}.get(raw, raw)


def search(
    query: str,
    states: list[dict[str, Any]],
    areas: dict[str, str],
    domains: tuple[str, ...] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Find the entities that best match a free-text query, best match first.

    Each entity is scored against three haystacks: its friendly name, its
    entity id, and the concatenation ``"<area> <name>"``. The last one is what
    makes "luce studio" match an entity merely named "Faretto" that happens to
    live in the "Studio" area. All comparisons happen on :func:`normalize` output.

    The scoring ladder, evaluated top to bottom, first rule that fires wins:

    ===== ==========================================================
    Score Condition
    ===== ==========================================================
    1.00  The query equals the whole name or the whole entity id.
    0.95  The query equals the entity's area name.
    0.90  Any haystack starts with the query ("sal" -> "salone").
    0.80  Any haystack contains the query anywhere.
    <0.80 Best :class:`difflib.SequenceMatcher` ratio across the
          haystacks; entities below 0.62 are dropped entirely.
    ===== ==========================================================

    The 0.62 floor is a deliberate compromise: high enough to reject unrelated
    entities on a large installation, low enough to absorb the typos and the
    missing plurals that speech-to-text produces.

    Args:
        query: Free text, typically what is left of a sentence after the verbs and
            articles have been stripped by ``HassBot._strip_verbs``.
        states: The pool of entities to search, usually already restricted to a
            domain by the caller.
        areas: Mapping ``entity_id -> area name``, used for the third haystack and
            for the exact-area rule.
        domains: When given, only entities whose id starts with one of these
            domains (``("light", "switch")``) are considered. This is a hard
            filter applied before scoring, not a ranking hint.
        limit: Maximum number of results. Callers that build inline keyboards pass
            ``MAX_BUTTONS`` so the result always fits in one Telegram keyboard.

    Returns:
        Up to ``limit`` state dictionaries, sorted by descending score and then by
        friendly name so that equally-scored results come back in a stable,
        alphabetical order. An empty or whitespace-only query returns ``[]``.
    """
    q = normalize(query)
    if not q:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for st in states:
        entity_id = st["entity_id"]
        if domains and not entity_id.startswith(tuple(f"{d}." for d in domains)):
            continue
        name = normalize(friendly_name(st))
        eid = normalize(entity_id)
        area = normalize(areas.get(entity_id, ""))
        haystacks = [name, eid, f"{area} {name}".strip()]

        if q in (name, eid):
            score = 1.0
        elif area and q == area:
            score = 0.95
        elif any(h.startswith(q) for h in haystacks):
            score = 0.9
        elif any(q in h for h in haystacks):
            score = 0.8
        else:
            score = max(difflib.SequenceMatcher(None, q, h).ratio() for h in haystacks)
            if score < 0.62:
                continue
        scored.append((score, st))

    scored.sort(key=lambda pair: (-pair[0], friendly_name(pair[1])))
    return [st for _, st in scored[:limit]]


def group_by_area(states: list[dict[str, Any]], areas: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Group entities by room for the per-area summaries and keyboards.

    Args:
        states: The entities to group.
        areas: Mapping ``entity_id -> area name``.

    Returns:
        A dictionary ``area name -> entities``. Entities with no area land in the
        ``"Senza stanza"`` bucket. Areas are ordered alphabetically,
        case-insensitively, with ``"Senza stanza"`` forced last; entities inside
        each group are sorted by friendly name. The ordering is stable on purpose:
        the keys drive the order of the inline keyboard buttons, and buttons that
        move between two renderings of the same message are a usability problem.

    Note:
        Callers that only want real rooms -- the area keyboards, the per-room
        temperature listing -- filter ``"Senza stanza"`` out themselves.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for st in states:
        grouped.setdefault(areas.get(st["entity_id"], "Senza stanza"), []).append(st)
    for group in grouped.values():
        group.sort(key=friendly_name)
    return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == "Senza stanza", kv[0].lower())))
