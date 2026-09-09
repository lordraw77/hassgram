"""A house to test against, and the smallest fakes that can drive the bot.

Nothing here touches the network. :class:`FakeHA` stands in for
:class:`ha_client.HomeAssistantClient` -- the bot only ever calls five of its
methods -- and the Telegram fakes implement just the attributes the handlers
reach for, which is a short list by design: ``update.effective_message``,
``update.effective_chat``, ``message.reply_text``, ``message.chat.send_action``
and, for callbacks, ``query.answer`` / ``query.edit_message_text``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --------------------------------------------------------------- the house
#
# One installation exercised by every test, built to contain the awkward cases
# rather than the tidy ones: a light that is unreachable, a light whose name is
# its own area, a light with no friendly name and no area, a switch (which is a
# light for /accendi but not for /luci), a room whose only temperature-shaped
# entity is a decoy, a name that would break HTML if it were not escaped, and one
# runnable entity per domain -- none of which is ever a light -- and a scene,
# which is runnable in neither sense and must stay out of every listing.

HOUSE: list[dict[str, Any]] = [
    {"entity_id": "light.salone_principale", "state": "on",
     "attributes": {"friendly_name": "Luce salone"}},
    {"entity_id": "light.salone_faretti", "state": "off",
     "attributes": {"friendly_name": "Faretti"}},
    {"entity_id": "light.cucina", "state": "off",
     "attributes": {"friendly_name": "Luce cucina"}},
    {"entity_id": "light.cucina_led", "state": "unavailable",
     "attributes": {"friendly_name": "Led cucina"}},
    # friendly name equal to its own area: label() must not say "Studio (Studio)"
    {"entity_id": "light.studio", "state": "on",
     "attributes": {"friendly_name": "Studio"}},
    # no friendly name and no area at all
    {"entity_id": "light.corridoio", "state": "off", "attributes": {}},
    # a name that is hostile to HTML parse mode
    {"entity_id": "light.terrazza", "state": "off",
     "attributes": {"friendly_name": "Luce <terrazza> & co"}},
    {"entity_id": "switch.presa_tv", "state": "on",
     "attributes": {"friendly_name": "Presa TV"}},
    {"entity_id": "switch.caldaia", "state": "off",
     "attributes": {"friendly_name": "Caldaia"}},
    {"entity_id": "sensor.temp_salone", "state": "21.5",
     "attributes": {"friendly_name": "Temperatura salone",
                    "device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "sensor.umid_salone", "state": "48",
     "attributes": {"friendly_name": "Umidita salone",
                    "device_class": "humidity", "unit_of_measurement": "%"}},
    {"entity_id": "sensor.temp_cucina", "state": "19.0",
     "attributes": {"friendly_name": "Temperatura cucina",
                    "device_class": "temperature", "unit_of_measurement": "°C"}},
    # device_class temperature on a domain that is not a reading: must never be
    # listed as a sensor. Bagno therefore has no temperature at all.
    {"entity_id": "binary_sensor.gelo", "state": "on",
     "attributes": {"friendly_name": "Allarme gelo", "device_class": "temperature"}},
    {"entity_id": "sensor.consumo", "state": "340",
     "attributes": {"friendly_name": "Consumo", "device_class": "power",
                    "unit_of_measurement": "W"}},
    {"entity_id": "climate.termo_salone", "state": "heat",
     "attributes": {"friendly_name": "Termostato",
                    "current_temperature": 20.4, "temperature": 21}},
    {"entity_id": "stt.whisper", "state": "idle", "attributes": {"friendly_name": "Whisper"}},
    # Runnable entities: one per supported domain, plus a disabled automation,
    # since a disabled automation can still be triggered by hand and must be
    # marked as such. The scene is the negative case: scenes are not runnable,
    # so it must never reach /esegui.
    {"entity_id": "scene.cinema", "state": "unknown",
     "attributes": {"friendly_name": "Cinema"}},
    {"entity_id": "script.buonanotte", "state": "off",
     "attributes": {"friendly_name": "Buonanotte"}},
    {"entity_id": "script.aperitivo", "state": "off",
     "attributes": {"friendly_name": "Aperitivo"}},
    {"entity_id": "automation.risveglio", "state": "on",
     "attributes": {"friendly_name": "Risveglio"}},
    {"entity_id": "automation.vacanza", "state": "off",
     "attributes": {"friendly_name": "Vacanza"}},
]

AREAS: dict[str, str] = {
    "light.salone_principale": "Salone",
    "light.salone_faretti": "Salone",
    "light.cucina": "Cucina",
    "light.cucina_led": "Cucina",
    "light.studio": "Studio",
    "light.terrazza": "Terrazza",
    "switch.presa_tv": "Salone",
    "sensor.temp_salone": "Salone",
    "sensor.umid_salone": "Salone",
    "sensor.temp_cucina": "Cucina",
    "binary_sensor.gelo": "Bagno",
    "sensor.consumo": "Salone",
    "climate.termo_salone": "Salone",
}


def house() -> list[dict[str, Any]]:
    """Return a deep-enough copy of :data:`HOUSE`.

    The bot never mutates a state dictionary, but a test that does must not leak
    into the next one.
    """
    return [{**s, "attributes": dict(s.get("attributes", {}))} for s in HOUSE]


# ------------------------------------------------------------ Home Assistant
class FakeHA:
    """Stand-in for :class:`ha_client.HomeAssistantClient`.

    Records what the bot asked it to do so tests can assert on the service calls
    rather than on the confirmation text, and counts state reads so the "is this
    path hitting the API twice" questions have an answer.

    Attributes:
        calls: ``(domain, service, entity_ids)`` per :meth:`call_service`.
        state_reads: How many times :meth:`states` was called.
        area_reads: How many times the areas were actually rendered, as opposed to
            served from the cache.
        invalidations: How many times the cache was dropped.
        fail_with: When set, every method raises it instead of answering.
    """

    def __init__(self, states=None, areas=None, fail_with=None):
        self._states = states if states is not None else house()
        self._areas = AREAS if areas is None else areas
        self._areas_cache: dict[str, str] | None = None
        self.calls: list[tuple[str, str, Any]] = []
        self.state_reads = 0
        self.area_reads = 0
        self.invalidations = 0
        self.fail_with = fail_with
        self.stt_calls: list[dict[str, Any]] = []
        self.stt_result = "accendi il salone"

    def _boom(self):
        if self.fail_with is not None:
            raise self.fail_with

    async def states(self):
        self._boom()
        self.state_reads += 1
        return self._states

    async def areas(self):
        """Cached after the first call, as the real client caches it forever.

        Modelled rather than simplified because the bot leans on it: /esegui reads
        the areas on every invocation and must not go near the network to do so.
        """
        if self._areas_cache is None:
            self._boom()
            self.area_reads += 1
            self._areas_cache = self._areas
        return self._areas_cache

    async def call_service(self, domain, service, data):
        self._boom()
        self.calls.append((domain, service, data["entity_id"]))
        self.invalidations += 1
        return []

    def invalidate_states(self):
        self.invalidations += 1

    async def stt_entities(self):
        self._boom()
        return sorted(s["entity_id"] for s in self._states if s["entity_id"].startswith("stt."))

    async def speech_to_text(self, audio, entity_id, **kwargs):
        self._boom()
        self.stt_calls.append({"audio": audio, "entity_id": entity_id, **kwargs})
        return self.stt_result

    async def ping(self):
        self._boom()
        return "API running."

    async def aclose(self):
        pass


# ------------------------------------------------------------------ Telegram
class FakeChat:
    """The two attributes the bot reads off a chat, plus the action it sends."""

    def __init__(self, chat_id: int = 1):
        self.id = chat_id
        self.actions: list[str] = []

    async def send_action(self, action):
        self.actions.append(action)


class FakeMessage:
    """Collects what the bot replied instead of sending it anywhere."""

    def __init__(self, text: str | None = None, chat: FakeChat | None = None):
        self.text = text
        self.chat = chat or FakeChat()
        self.sent: list[tuple[str, dict]] = []
        self.voice = self.audio = self.video_note = None

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))

    # -- assertions helpers -------------------------------------------------
    @property
    def last(self) -> str:
        """The body of the most recent reply."""
        return self.sent[-1][0]

    @property
    def first(self) -> str:
        """The body of the first reply."""
        return self.sent[0][0]

    @property
    def markup(self):
        """The ``reply_markup`` of the most recent reply, or ``None``."""
        return self.sent[-1][1].get("reply_markup")


class FakeUpdate:
    """The shape of an ``Update`` the handlers actually use."""

    def __init__(self, text: str | None = None, chat_id: int = 1, with_message: bool = True):
        self.effective_chat = FakeChat(chat_id)
        self.effective_message = FakeMessage(text, self.effective_chat) if with_message else None


class FakeFile:
    """A Telegram ``File`` that yields fixed bytes."""

    def __init__(self, payload: bytes):
        self.payload = payload

    async def download_as_bytearray(self):
        return bytearray(self.payload)


class FakeVoice:
    """A voice note, audio file or video note."""

    def __init__(self, payload: bytes = b"OggS-fake", size: int | None = None, mime_type="audio/ogg"):
        self.payload = payload
        self.file_size = len(payload) if size is None else size
        self.mime_type = mime_type

    async def get_file(self):
        return FakeFile(self.payload)


class FakeQuery:
    """A ``CallbackQuery``: records answers and message edits."""

    def __init__(self, data: str = ""):
        self.data = data
        self.answers: list[tuple[str | None, dict]] = []
        self.edits: list[tuple[str, dict]] = []
        self.edit_error: Exception | None = None

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((text, kwargs))

    @property
    def last_edit(self) -> str:
        return self.edits[-1][0]


class FakeBot:
    """The one Telegram API method the lifecycle hooks call, plus its failure mode.

    Attributes:
        command_menus: ``(language_code, [(name, description), ...])`` per
            :meth:`set_my_commands`; ``None`` as the code is the unscoped default.
        fail_with: When set, every call raises it instead of recording.
    """

    def __init__(self, fail_with=None):
        self.command_menus: list[tuple[str | None, list[tuple[str, str]]]] = []
        self.fail_with = fail_with

    async def set_my_commands(self, commands, language_code=None, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.command_menus.append((language_code, [(c.command, c.description) for c in commands]))


def app(hass, bot=None):
    """The shape of an ``Application`` the lifecycle hooks actually use."""
    return types.SimpleNamespace(bot_data={"hass": hass}, bot=bot if bot is not None else FakeBot())


def context(args=None, error=None, bot_data=None):
    """Build the ``ContextTypes.DEFAULT_TYPE`` stand-in a handler receives."""
    return types.SimpleNamespace(
        args=args,
        error=error,
        application=types.SimpleNamespace(bot_data=bot_data if bot_data is not None else {}),
    )


def buttons(markup) -> list[str]:
    """Flatten an ``InlineKeyboardMarkup`` to its button labels."""
    return [b.text for row in markup.inline_keyboard for b in row]


def payloads(markup) -> list[str]:
    """Flatten an ``InlineKeyboardMarkup`` to its ``callback_data`` values."""
    return [b.callback_data for row in markup.inline_keyboard for b in row]
