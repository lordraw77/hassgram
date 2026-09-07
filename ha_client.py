"""Minimal asynchronous client for the Home Assistant REST API.

Hassgram only needs a handful of endpoints, so this module deliberately stops
far short of a general-purpose SDK. It owns one :class:`httpx.AsyncClient` with
the bearer token pre-applied, turns every transport or HTTP failure into a
single exception type, and adds the two caches the bot cannot work without.

Endpoints used
--------------

``GET /api/``
    Liveness probe, called once at startup.
``GET /api/states``
    Every entity and its current state. This is the workhorse: Hassgram never
    queries entities one by one, it filters a full snapshot in memory.
``POST /api/services/<domain>/<service>``
    Turning things on and off.
``POST /api/template``
    Renders Jinja on the Home Assistant side. Used to reach the area registry,
    which the REST API does not expose in any other way.
``POST /api/stt/<entity_id>`` and ``GET /api/stt/<entity_id>``
    Speech-to-text for Telegram voice messages.

Caching
-------

Two caches with very different lifetimes:

* **States**, 5 seconds by default. A single command can trigger several
  lookups (states, then areas, then a keyboard render); without the cache each
  one would be a separate round trip. The window is short enough that a light
  toggled from the Home Assistant app shows up almost immediately, and it is
  invalidated explicitly whenever this client calls a service, so the bot never
  renders a keyboard from a snapshot it has just made stale itself.
* **Areas**, unbounded. The area registry changes when the user reconfigures
  their house, which is rare enough that a process restart is an acceptable way
  to refresh it. Rendering the template is far more expensive than fetching
  states, so caching it forever is the right trade.

Concurrency
-----------

The client is meant to be shared by every Telegram handler in one event loop.
:meth:`HomeAssistantClient.states` serialises its refresh behind an
:class:`asyncio.Lock`, so concurrent commands cause one HTTP request rather than
one each. No other method needs coordination.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class HomeAssistantError(RuntimeError):
    """Raised for every failure while talking to Home Assistant.

    Collapsing transport errors (DNS, connection refused, timeouts) and HTTP error
    responses (401 on an expired token, 404 on an unknown entity, 500 from a
    failing integration) into one type keeps the call sites in :mod:`bot` simple:
    they either handle the failure locally or let it reach the global error
    handler, which turns it into a message to the user.

    The message is user-facing: it is shown in Telegram verbatim, and therefore
    includes the HTTP status and a truncated response body when there is one.
    """
    pass


class HomeAssistantClient:
    """Async facade over the subset of the Home Assistant REST API that Hassgram uses.

    One instance is created in :func:`bot.main` and shared by every handler for the
    lifetime of the process. It owns an :class:`httpx.AsyncClient`, so it must be
    closed with :meth:`aclose` on shutdown -- ``bot.post_shutdown`` does this.

    Attributes:
        base_url: The API root, trailing slash removed, e.g.
            ``http://192.168.0.220:8123/api``.

    Example:
        >>> ha = HomeAssistantClient("http://ha.local:8123/api/", token)  # doctest: +SKIP
        >>> lights = [s for s in await ha.states()                        # doctest: +SKIP
        ...           if s["entity_id"].startswith("light.")]
        >>> await ha.call_service("light", "turn_on",                     # doctest: +SKIP
        ...                       {"entity_id": ["light.studio"]})
    """
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        """Build the client and its shared HTTP session.

        No network traffic happens here; the first request is issued by whoever calls
        a method first (in practice :meth:`ping`, from ``bot.post_init``).

        Args:
            base_url: Root of the REST API, with or without a trailing slash. It must
                include the ``/api`` suffix, e.g. ``http://192.168.0.220:8123/api``.
                Every other method passes root-relative paths such as ``/states``.
            token: A Home Assistant long-lived access token, sent as
                ``Authorization: Bearer ...`` on every request. It grants full access
                to the instance, so it belongs in ``.env`` and nowhere else.
            timeout: Per-request timeout in seconds, applied to connect, read and
                write alike. The default of 15 s is generous for a LAN instance but
                leaves room for a Home Assistant that is busy starting up.
        """
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        self._states_cache: list[dict[str, Any]] | None = None
        self._states_cache_at: float = 0.0
        self._areas_cache: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close the underlying HTTP session and release its connection pool.

        Safe to call once at shutdown; the client must not be used afterwards.
        """
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform one HTTP request and normalise its outcome.

        This is the single choke point through which every call goes, which is what
        makes the error contract of this module uniform.

        Args:
            method: HTTP verb, e.g. ``"GET"`` or ``"POST"``.
            path: Path relative to ``base_url``, starting with a slash.
            **kwargs: Passed straight to :meth:`httpx.AsyncClient.request` --
                ``json=`` for JSON bodies, ``content=`` plus ``headers=`` for the raw
                audio upload of :meth:`speech_to_text`.

        Returns:
            The decoded JSON body when the response advertises
            ``Content-Type: application/json``, otherwise the raw text. Callers that
            need a specific shape check it themselves, because Home Assistant answers
            ``POST /api/template`` with ``text/plain``.

        Raises:
            HomeAssistantError: On any transport failure, and on any response with
                status >= 400. The message carries the status code and the first 200
                characters of the body -- enough to recognise an expired token or an
                unknown entity without flooding a Telegram message.
        """
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"Errore di rete verso Home Assistant: {exc}") from exc
        if resp.status_code >= 400:
            raise HomeAssistantError(f"Home Assistant ha risposto {resp.status_code}: {resp.text[:200]}")
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    async def ping(self) -> str:
        """Check that the instance is reachable and the token is accepted.

        Called once at startup so that a bad URL or a revoked token fails loudly in
        the log instead of surfacing later as a puzzling command failure.

        Returns:
            The ``message`` field of ``GET /api/`` ("API running."), or the raw body
            if the response is not the expected object.

        Raises:
            HomeAssistantError: If the instance is unreachable or rejects the token.
        """
        data = await self._request("GET", "/")
        return data.get("message", "") if isinstance(data, dict) else str(data)

    async def states(self, max_age: float = 5.0) -> list[dict[str, Any]]:
        """Return every entity state, served from a short-lived cache.

        The refresh is serialised behind an :class:`asyncio.Lock`, so several handlers
        waking up at once produce a single HTTP request rather than one each.

        Args:
            max_age: Maximum age of the cached snapshot, in seconds. The default of 5
                collapses the multiple lookups of a single command into one request
                while keeping the view fresh enough that a light toggled elsewhere is
                reflected almost immediately. Pass ``0`` to force a refresh.

        Returns:
            The parsed ``GET /api/states`` payload: a list of state dictionaries. The
            list is the cached object itself, not a copy -- callers filter it and must
            not mutate it.

        Raises:
            HomeAssistantError: If the refresh fails. Note that a failure leaves the
                previous cache in place only if it had not expired; an expired cache
                is refetched on the next call.
        """
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._states_cache is None or now - self._states_cache_at > max_age:
                self._states_cache = await self._request("GET", "/states")
                self._states_cache_at = now
            return self._states_cache

    def invalidate_states(self) -> None:
        """Drop the cached snapshot so the next :meth:`states` call refetches.

        Called automatically by :meth:`call_service` -- after the bot itself changes
        something, the cached view is known to be stale and must not be used to render
        the confirmation keyboard -- and explicitly by ``HassBot._refresh_message``
        when the user taps the refresh button.

        Both fields are reset because :meth:`states` decides on cache *and* timestamp
        together. Plain attribute assignment is atomic with respect to the event loop,
        so no lock is needed here.
        """
        self._states_cache = None
        self._states_cache_at = 0.0

    async def state(self, entity_id: str) -> dict[str, Any]:
        """Fetch a single entity's state, bypassing the cache.

        Unused by the current command set -- the bot always filters a full snapshot --
        but kept because it is the cheapest way to check one entity when debugging or
        extending the bot.

        Args:
            entity_id: Full entity id, e.g. ``light.studio``.

        Returns:
            The state dictionary for that entity.

        Raises:
            HomeAssistantError: With a 404 in the message if the entity does not exist.
        """
        return await self._request("GET", f"/states/{entity_id}")

    async def render_template(self, template: str) -> str:
        """Render a Jinja template inside Home Assistant and return its output.

        This is an escape hatch for everything the REST API does not expose directly:
        the template engine runs with full access to the registries, so a template can
        reach data no endpoint offers. Hassgram uses it for exactly one thing, the
        entity-to-area mapping built by :meth:`areas`.

        Args:
            template: Jinja source, evaluated with Home Assistant's template
                environment (``areas()``, ``area_entities()``, ``states``, ...).

        Returns:
            The rendered text. The endpoint answers ``text/plain``, so the result is a
            string even when the template produces something that looks like JSON.

        Raises:
            HomeAssistantError: If the template fails to render; the message contains
                Home Assistant's own error, which is usually enough to spot the typo.
        """
        return await self._request("POST", "/template", json={"template": template})

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """Invoke a Home Assistant service and invalidate the state cache.

        Args:
            domain: Service domain, e.g. ``"light"`` or ``"switch"``. It must match the
                domain of the target entities: Home Assistant will not accept a
                ``light.*`` id under the ``switch`` domain, which is why the bot groups
                its targets by domain before calling.
            service: Service name, e.g. ``"turn_on"`` or ``"turn_off"``.
            data: Service payload. ``{"entity_id": [...]}`` accepts a list, so one call
                can act on many entities of the same domain at once.

        Returns:
            The list of states the service changed, as reported by Home Assistant.
            Hassgram ignores it and re-reads the states instead, because a service call
            can have effects beyond the entities it names.

        Raises:
            HomeAssistantError: If the service call fails.

        Note:
            The cache is invalidated even though the return value is discarded: the
            caller almost always re-renders a keyboard immediately afterwards, and it
            must not do so from a snapshot taken before the change.
        """
        result = await self._request("POST", f"/services/{domain}/{service}", json=data)
        self.invalidate_states()
        return result

    async def stt_entities(self) -> list[str]:
        """List the speech-to-text entities exposed by Home Assistant.

        Used at startup to auto-select an engine when ``HA_STT_ENTITY`` is not set.
        It reads the ordinary state snapshot rather than a dedicated endpoint, because
        STT entities appear in ``/api/states`` like everything else.

        Returns:
            Sorted entity ids in the ``stt.`` domain, e.g.
            ``["stt.google_ai_stt"]``. An empty list means no speech-to-text
            integration is configured, and the bot will decline voice messages with an
            explanation instead of failing.

        Raises:
            HomeAssistantError: If the state snapshot cannot be fetched.
        """
        return sorted(s["entity_id"] for s in await self.states() if s["entity_id"].startswith("stt."))

    async def speech_to_text(
        self,
        audio: bytes,
        entity_id: str,
        language: str = "it-IT",
        audio_format: str = "ogg",
        codec: str = "opus",
        sample_rate: int = 16000,
    ) -> str:
        """Transcribe an audio clip with a Home Assistant speech-to-text entity.

        The audio is sent as the raw request body to ``POST /api/stt/<entity_id>``;
        the metadata that describes it travels in the ``X-Speech-Content`` header,
        not in the body. Home Assistant validates that header against the values the
        provider advertises on ``GET /api/stt/<entity_id>`` and rejects anything else
        with a 400 before the audio is ever decoded.

        That validation is why ``sample_rate`` defaults to 16000 while Telegram voice
        messages are 48 kHz: the provider only declares support for 16000, but the
        Ogg container carries its own sample rate and the decoder honours it. The
        declared value satisfies the check, the container drives the decoding, and no
        resampling -- and therefore no ffmpeg dependency -- is needed.

        Args:
            audio: The complete clip as bytes. The whole thing is held in memory, so
                callers must cap the size beforehand (``bot.MAX_VOICE_BYTES``).
            entity_id: The STT entity to use, e.g. ``stt.google_ai_stt``.
            language: BCP-47 language tag passed to the provider, e.g. ``it-IT``. The
                provider must advertise it, otherwise the request is rejected.
            audio_format: Container, ``ogg`` for Telegram voice notes, ``wav`` for a
                forwarded audio file.
            codec: Codec inside the container, ``opus`` for Ogg, ``pcm`` for WAV.
            sample_rate: Sample rate declared in the header. See the discussion above
                before changing it.

        Returns:
            The transcribed text, stripped. May be an empty string when the clip
            contains no recognisable speech, which callers must handle: it is not an
            error, it just means there is nothing to execute.

        Raises:
            HomeAssistantError: If the request fails, or if the provider answers
                without ``result == "success"`` (a rejected format, an unsupported
                language, or an engine that is momentarily unavailable).
        """
        headers = {
            "X-Speech-Content": (
                f"format={audio_format}; codec={codec}; sample_rate={sample_rate}; "
                f"bit_rate=16; channel=1; language={language}"
            ),
            "Content-Type": "application/octet-stream",
        }
        data = await self._request("POST", f"/stt/{entity_id}", content=audio, headers=headers)
        if not isinstance(data, dict) or data.get("result") != "success":
            raise HomeAssistantError(f"Trascrizione fallita: {data}")
        return (data.get("text") or "").strip()

    async def stt_options(self, entity_id: str) -> dict[str, Any]:
        """Read the formats a speech-to-text entity accepts.

        Diagnostic helper, not used on the normal path: when a transcription is
        rejected, this endpoint tells you exactly which containers, codecs, sample
        rates, channel counts and languages the provider advertises, which is the
        fastest way to fix an ``X-Speech-Content`` header.

        Args:
            entity_id: The STT entity to interrogate.

        Returns:
            The provider's declared capabilities.

        Raises:
            HomeAssistantError: If the entity does not exist or the request fails.
        """
        return await self._request("GET", f"/stt/{entity_id}")

    async def areas(self) -> dict[str, str]:
        """Return the entity-to-room mapping, rendered once and cached forever.

        The REST API exposes no area registry: ``/api/states`` knows nothing about
        rooms, and there is no ``/api/areas``. The template engine, however, does have
        ``areas()``, ``area_entities()`` and ``area_name()``, so the mapping is built
        by rendering a Jinja template server-side and parsing its output. The template
        emits one ``entity_id<TAB>area name`` line per entity, a format chosen because
        neither field can contain a tab while both can contain spaces and commas.

        This mapping is what makes every room-aware feature work: the ``/luci``
        summary, area keyboards, ``/accendi salone``, per-room temperatures.

        Returns:
            Mapping ``entity_id -> area name``. Entities that belong to no area are
            absent, and callers treat a missing key as "Senza stanza". The dictionary
            is the cached object itself; callers must not mutate it.

        Raises:
            HomeAssistantError: If the template cannot be rendered.

        Note:
            The cache has no expiry, so rooms added or renamed in Home Assistant are
            only picked up after a restart of the bot. This is a deliberate trade: the
            registry changes rarely, and the render is much more expensive than a state
            fetch. Malformed lines are skipped rather than raising, so one odd entity
            cannot break room support for the whole house.
        """
        if self._areas_cache is None:
            raw = await self.render_template(
                "{% set ns = namespace(rows=[]) %}"
                "{% for a in areas() %}"
                "{% for e in area_entities(a) %}"
                "{% set ns.rows = ns.rows + [e ~ '\t' ~ area_name(a)] %}"
                "{% endfor %}{% endfor %}"
                "{{ ns.rows | join('\n') }}"
            )
            mapping: dict[str, str] = {}
            for line in str(raw).splitlines():
                if "\t" in line:
                    entity_id, area = line.split("\t", 1)
                    mapping[entity_id.strip()] = area.strip()
            self._areas_cache = mapping
        return self._areas_cache
