"""Tests for the asynchronous handlers: commands, callbacks, voice, wiring.

These drive :class:`bot.HassBot` through the fakes in :mod:`tests.fakes`, which
implement only the handful of Telegram attributes the handlers actually reach
for. Nothing here needs a network, a token or an event loop of its own.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from tests.fakes import (
    AREAS, FakeHA, FakeQuery, FakeUpdate, FakeVoice, buttons, context, house, payloads,
)

import bot
import i18n
from ha_client import HomeAssistantError


def make_bot(allowed=(), **kwargs) -> bot.HassBot:
    """A bot wired to a :class:`tests.fakes.FakeHA`, reachable as ``b.ha``."""
    return bot.HassBot(kwargs.pop("ha", None) or FakeHA(), set(allowed), **kwargs)


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """Resets the process-global token store, which tests would otherwise share."""

    def setUp(self):
        bot._tokens.clear()
        self.b = make_bot()


# ------------------------------------------------------------ authorisation
class AuthorisationTest(BotTestCase):
    async def test_empty_allow_list_lets_anyone_in(self):
        self.assertTrue(self.b.authorized(FakeUpdate()))

    async def test_allow_list_admits_a_listed_chat(self):
        b = make_bot(allowed={42})
        self.assertTrue(b.authorized(FakeUpdate(chat_id=42)))

    async def test_allow_list_refuses_everyone_else(self):
        b = make_bot(allowed={42})
        self.assertFalse(b.authorized(FakeUpdate(chat_id=7)))

    async def test_an_update_without_a_chat_is_refused_when_the_list_is_set(self):
        b = make_bot(allowed={42})
        update = FakeUpdate()
        update.effective_chat = None
        self.assertFalse(b.authorized(update))

    async def test_guard_answers_the_refusal(self):
        b = make_bot(allowed={42})
        update = FakeUpdate(chat_id=7)
        with self.assertLogs("hassgram", level="WARNING") as logs:
            self.assertFalse(await b.guard(update))
        self.assertIn("7", logs.output[0])
        self.assertIn(i18n.t("it", "unauthorized"), update.effective_message.last)

    async def test_guard_passes_an_authorised_update(self):
        update = FakeUpdate()
        self.assertTrue(await self.b.guard(update))
        self.assertEqual(update.effective_message.sent, [])

    async def test_every_command_refuses_a_stranger(self):
        """No handler may reach Home Assistant before the allow-list is consulted."""
        handlers = [
            ("cmd_start", None), ("cmd_language", []), ("cmd_lights", []),
            ("cmd_on_now", None), ("cmd_on", ["salone"]), ("cmd_off", ["salone"]),
            ("cmd_temperature", []), ("cmd_state", ["salone"]),
        ]
        for name, args in handlers:
            with self.subTest(handler=name):
                b = make_bot(allowed={42})
                update = FakeUpdate(chat_id=7)
                await getattr(b, name)(update, context(args=args))
                self.assertEqual(b.ha.calls, [], f"{name} called a service for a stranger")
                self.assertEqual(b.ha.state_reads, 0, f"{name} read states for a stranger")
                self.assertEqual(dict(b.chat_lang), {}, f"{name} remembered a stranger's language")
                self.assertIn(i18n.t("it", "unauthorized"), update.effective_message.last)

    async def test_callbacks_refuse_a_stranger_with_an_alert(self):
        b = make_bot(allowed={42})
        update = FakeUpdate(chat_id=7)
        query = FakeQuery(data=f"do:on:{bot.tok('light.cucina')}")
        update.callback_query = query
        await b.on_callback(update, context())
        self.assertEqual(b.ha.calls, [])
        self.assertTrue(query.answers[0][1]["show_alert"])


# ---------------------------------------------------------------- languages
class LanguageTest(BotTestCase):
    async def test_command_name_identifies_the_language(self):
        self.assertEqual(self.b.resolve_lang(FakeUpdate("/luci")), "it")
        self.assertEqual(self.b.resolve_lang(FakeUpdate("/lights")), "en")

    async def test_a_shared_command_name_carries_no_signal(self):
        """``/on`` exists in both languages: it must not flip the conversation."""
        b = make_bot(default_lang="en")
        self.assertEqual(b.resolve_lang(FakeUpdate("/on studio")), "en")
        b.chat_lang[1] = "it"
        self.assertEqual(b.resolve_lang(FakeUpdate("/on studio")), "it")

    async def test_command_with_a_bot_suffix_is_still_recognised(self):
        self.assertEqual(self.b.resolve_lang(FakeUpdate("/lights@hassbot")), "en")

    async def test_words_identify_the_language(self):
        self.assertEqual(self.b.resolve_lang(FakeUpdate("turn on the light in the study")), "en")
        self.assertEqual(self.b.resolve_lang(FakeUpdate("accendi la luce dello studio")), "it")

    async def test_ambiguous_text_keeps_the_conversation_where_it_was(self):
        self.b.chat_lang[1] = "en"
        self.assertEqual(self.b.resolve_lang(FakeUpdate("salone")), "en")

    async def test_the_answer_is_remembered_for_input_that_carries_no_language(self):
        update = FakeUpdate("which lights are on")
        self.b.resolve_lang(update)
        self.assertEqual(self.b.lang_of(FakeUpdate(chat_id=1)), "en")

    async def test_explicit_text_argument_wins_over_the_message(self):
        # the voice path passes the transcription in
        self.assertEqual(self.b.resolve_lang(FakeUpdate("/luci"), "turn everything off"), "en")

    async def test_lang_of_falls_back_to_the_process_default(self):
        b = make_bot(default_lang="en")
        self.assertEqual(b.lang_of(FakeUpdate(chat_id=999)), "en")

    async def test_default_lang_is_normalised(self):
        self.assertEqual(make_bot(default_lang="en-GB").default_lang, "en")
        self.assertEqual(make_bot(default_lang="nonsense").default_lang, i18n.DEFAULT_LANG)

    async def test_remembered_languages_are_bounded(self):
        with mock.patch.object(bot, "MAX_CHAT_LANGS", 3):
            for i in range(10):
                self.b._remember_lang(FakeUpdate(chat_id=i), "en")
            self.assertEqual(len(self.b.chat_lang), 3)
            self.assertNotIn(0, self.b.chat_lang)
            self.assertIn(9, self.b.chat_lang)

    async def test_remember_ignores_an_update_without_a_chat(self):
        update = FakeUpdate()
        update.effective_chat = None
        self.b._remember_lang(update, "en")
        self.assertEqual(dict(self.b.chat_lang), {})

    async def test_cmd_language_reports_the_current_language(self):
        update = FakeUpdate("/lingua")
        await self.b.cmd_language(update, context(args=[]))
        self.assertIn("italiano", update.effective_message.last)

    async def test_cmd_language_pins_the_chat(self):
        update = FakeUpdate("/language en")
        await self.b.cmd_language(update, context(args=["en"]))
        self.assertEqual(self.b.chat_lang[1], "en")
        self.assertIn("English", update.effective_message.last)

    async def test_cmd_language_accepts_loose_tags(self):
        update = FakeUpdate("/lingua")
        await self.b.cmd_language(update, context(args=["English"]))
        self.assertEqual(self.b.chat_lang[1], "en")

    async def test_cmd_language_rejects_an_unknown_tag_without_changing_anything(self):
        update = FakeUpdate("/lingua")
        await self.b.cmd_language(update, context(args=["klingon"]))
        self.assertNotIn(1, self.b.chat_lang)
        self.assertIn("it", update.effective_message.last)


# ----------------------------------------------------------------- browsing
class LightsCommandTest(BotTestCase):
    async def test_no_argument_shows_the_overview_with_a_room_keyboard(self):
        update = FakeUpdate("/luci")
        await self.b.cmd_lights(update, context(args=[]))
        self.assertIn("Scegli una stanza", update.effective_message.last)
        self.assertEqual(buttons(update.effective_message.markup),
                         ["Cucina", "Salone", "Studio", "Terrazza"])

    async def test_a_whole_house_word_also_shows_the_overview(self):
        update = FakeUpdate("/luci casa")
        await self.b.cmd_lights(update, context(args=["casa"]))
        self.assertIn("Scegli una stanza", update.effective_message.last)

    async def test_a_room_shows_its_lights_with_toggles(self):
        update = FakeUpdate("/luci salone")
        await self.b.cmd_lights(update, context(args=["salone"]))
        self.assertIn("Luce salone", update.effective_message.last)
        self.assertIn("Faretti", update.effective_message.last)
        self.assertTrue(payloads(update.effective_message.markup)[0].startswith("do:"))

    async def test_a_miss_says_so_rather_than_showing_an_empty_keyboard(self):
        update = FakeUpdate("/luci cantina")
        await self.b.cmd_lights(update, context(args=["cantina"]))
        self.assertIn("Nessuna luce trovata", update.effective_message.last)
        self.assertIsNone(update.effective_message.markup)

    async def test_the_browser_never_lists_switches(self):
        update = FakeUpdate("/luci presa")
        await self.b.cmd_lights(update, context(args=["presa"]))
        self.assertIn("Nessuna luce trovata", update.effective_message.last)

    async def test_answers_in_english_for_the_english_command_name(self):
        update = FakeUpdate("/lights")
        await self.b.cmd_lights(update, context(args=[]))
        self.assertIn("Pick a room", update.effective_message.last)

    async def test_a_sentence_falls_back_to_the_overview_instead_of_blaming_the_user(self):
        update = FakeUpdate("fammi vedere le luci")
        await self.b.on_text(update, context())
        self.assertIn("Scegli una stanza", update.effective_message.last)


class WhatsOnTest(BotTestCase):
    async def test_lists_what_is_on_grouped_by_room(self):
        update = FakeUpdate("/accese")
        await self.b.cmd_on_now(update, context())
        text = update.effective_message.last
        self.assertIn("Luci accese", text)
        self.assertIn("<b>Salone</b>", text)
        self.assertIn("Luce salone", text)
        self.assertNotIn("Luce cucina", text)  # off

    async def test_offers_a_keyboard_of_what_is_on(self):
        update = FakeUpdate("/accese")
        await self.b.cmd_on_now(update, context())
        self.assertTrue(all(d.startswith("do:off:") for d in payloads(update.effective_message.markup)[:2]))

    async def test_says_so_when_everything_is_off(self):
        dark = [dict(s, state="off") for s in house()]
        b = make_bot(ha=FakeHA(states=dark))
        update = FakeUpdate("/accese")
        await b.cmd_on_now(update, context())
        self.assertIn("Tutte le luci sono spente", update.effective_message.last)


class StateCommandTest(BotTestCase):
    async def test_usage_without_an_argument(self):
        update = FakeUpdate("/stato")
        await self.b.cmd_state(update, context(args=[]))
        self.assertIn("/stato", update.effective_message.last)

    async def test_reaches_any_domain(self):
        update = FakeUpdate("/stato consumo")
        await self.b.cmd_state(update, context(args=["consumo"]))
        text = update.effective_message.last
        self.assertIn("340 W", text)
        self.assertIn("<code>sensor.consumo</code>", text)

    async def test_attaches_no_buttons(self):
        # "toggle whatever this is" is not a safe offer for an arbitrary domain
        update = FakeUpdate("/stato consumo")
        await self.b.cmd_state(update, context(args=["consumo"]))
        self.assertIsNone(update.effective_message.markup)

    async def test_reports_a_miss(self):
        update = FakeUpdate("/stato cantina")
        await self.b.cmd_state(update, context(args=["cantina"]))
        self.assertIn("Nessuna entità trovata", update.effective_message.last)

    async def test_escapes_the_query_it_echoes(self):
        update = FakeUpdate("/stato")
        await self.b.cmd_state(update, context(args=["<script>"]))
        self.assertNotIn("<script>", update.effective_message.last)


# ------------------------------------------------------------------ actions
class SwitchTest(BotTestCase):
    async def test_usage_when_the_argument_is_missing(self):
        update = FakeUpdate("/accendi")
        await self.b.cmd_on(update, context(args=[]))
        self.assertIn("/accendi", update.effective_message.last)
        self.assertEqual(self.b.ha.calls, [])

    async def test_usage_names_the_english_command_in_english(self):
        update = FakeUpdate("/on")
        self.b.chat_lang[1] = "en"
        await self.b.cmd_on(update, context(args=[]))
        self.assertIn("/on", update.effective_message.last)

    async def test_a_single_light(self):
        update = FakeUpdate("/accendi luce cucina")
        await self.b.cmd_on(update, context(args=["luce", "cucina"]))
        self.assertEqual(self.b.ha.calls, [("light", "turn_on", ["light.cucina"])])
        self.assertIn("accesa", update.effective_message.last)

    async def test_a_room(self):
        update = FakeUpdate("/spegni salone")
        await self.b.cmd_off(update, context(args=["salone"]))
        domain, service, ids = self.b.ha.calls[0]
        self.assertEqual((domain, service), ("light", "turn_off"))
        self.assertEqual(set(ids), {"light.salone_principale", "light.salone_faretti"})
        self.assertIn("Salone (2 luci)", update.effective_message.last)

    async def test_the_whole_house(self):
        update = FakeUpdate("/spegni casa")
        await self.b.cmd_off(update, context(args=["casa"]))
        self.assertIn("Tutta la casa", update.effective_message.last)
        self.assertNotIn("light.cucina_led", self.b.ha.calls[0][2])  # unavailable

    async def test_a_bulk_action_never_switches_a_plug(self):
        update = FakeUpdate("/spegni casa")
        await self.b.cmd_off(update, context(args=["casa"]))
        self.assertEqual([c[0] for c in self.b.ha.calls], ["light"])

    async def test_a_plug_can_still_be_named_explicitly(self):
        update = FakeUpdate("/spegni presa tv")
        await self.b.cmd_off(update, context(args=["presa", "tv"]))
        self.assertEqual(self.b.ha.calls, [("switch", "turn_off", ["switch.presa_tv"])])

    async def test_an_ambiguous_name_offers_a_choice_instead_of_acting(self):
        update = FakeUpdate("/accendi luce")
        await self.b.cmd_on(update, context(args=["luce"]))
        self.assertEqual(self.b.ha.calls, [])
        self.assertIn("Quale vuoi", update.effective_message.last)
        data = payloads(update.effective_message.markup)
        self.assertTrue(data[0].startswith("do:on:"))
        self.assertTrue(data[-1].startswith("all:on:"))

    async def test_a_room_name_beats_an_entity_name(self):
        """Targets resolve by decreasing specificity: house, then room, then entity.

        ``light.studio`` is *named* "Studio" and *lives in* the area "Studio", so
        ``/accendi studio`` is a room operation and takes the other light in that
        room with it. The entity rules only run once the area match has failed.
        """
        states = house() + [{"entity_id": "light.studio_2", "state": "off",
                             "attributes": {"friendly_name": "Studio grande"}}]
        b = make_bot(ha=FakeHA(states=states, areas={**AREAS, "light.studio_2": "Studio"}))
        update = FakeUpdate("/accendi Studio")
        await b.cmd_on(update, context(args=["Studio"]))
        self.assertEqual(b.ha.calls, [("light", "turn_on", ["light.studio", "light.studio_2"])])
        self.assertIn("Studio (2 luci)", update.effective_message.last)

    async def test_an_exact_name_wins_over_a_longer_one(self):
        """Rule 3: one lamp's name being a prefix of another's must not be ambiguous."""
        states = house() + [
            {"entity_id": "light.lampada", "state": "off",
             "attributes": {"friendly_name": "Lampada"}},
            {"entity_id": "light.lampada_grande", "state": "off",
             "attributes": {"friendly_name": "Lampada grande"}},
        ]
        b = make_bot(ha=FakeHA(states=states))
        update = FakeUpdate("/accendi lampada")
        await b.cmd_on(update, context(args=["lampada"]))
        self.assertEqual(b.ha.calls, [("light", "turn_on", ["light.lampada"])])

    async def test_a_name_that_is_only_a_prefix_offers_a_choice(self):
        states = house() + [
            {"entity_id": "light.lampada_a", "state": "off",
             "attributes": {"friendly_name": "Lampada rossa"}},
            {"entity_id": "light.lampada_b", "state": "off",
             "attributes": {"friendly_name": "Lampada blu"}},
        ]
        b = make_bot(ha=FakeHA(states=states))
        update = FakeUpdate("/accendi lampada")
        await b.cmd_on(update, context(args=["lampada"]))
        self.assertEqual(b.ha.calls, [])
        self.assertIn("Quale vuoi", update.effective_message.last)

    async def test_nothing_to_switch(self):
        update = FakeUpdate("/accendi cantina")
        await self.b.cmd_on(update, context(args=["cantina"]))
        self.assertEqual(self.b.ha.calls, [])
        self.assertIn("Non ho trovato niente", update.effective_message.last)

    async def test_a_room_whose_lights_are_all_unreachable_falls_through(self):
        """It must not report a bulk success on nothing.

        ``_bulk_targets`` drops unreachable entities, so the room yields none and
        the resolver falls through to the name search -- which may well find the
        unreachable light by name, since only *bulk* operations filter it out.
        What must not happen is a confirmation claiming a room was switched.
        """
        states = [s for s in house() if s["entity_id"] != "light.cucina"]
        b = make_bot(ha=FakeHA(states=states))
        update = FakeUpdate("/accendi cucina")
        await b.cmd_on(update, context(args=["cucina"]))
        self.assertNotIn("Cucina (", update.effective_message.last)
        self.assertEqual(b.ha.calls, [("light", "turn_on", ["light.cucina_led"])])

    async def test_a_room_with_no_lights_at_all_reports_a_miss(self):
        b = make_bot(ha=FakeHA(states=[s for s in house() if not s["entity_id"].startswith("light.cucina")]))
        update = FakeUpdate("/accendi cucina")
        await b.cmd_on(update, context(args=["cucina"]))
        self.assertEqual(b.ha.calls, [])
        self.assertIn("Non ho trovato niente", update.effective_message.last)

    async def test_call_on_ids_groups_by_domain(self):
        await self.b._call_on_ids(["light.a", "switch.b", "light.c"], turn_on=True)
        self.assertEqual(self.b.ha.calls, [
            ("light", "turn_on", ["light.a", "light.c"]),
            ("switch", "turn_on", ["switch.b"]),
        ])

    async def test_confirmation_is_inflected_for_number(self):
        update = FakeUpdate()
        targets = [{"entity_id": "light.a", "attributes": {"friendly_name": "A"}}]
        await self.b._apply(update, targets, turn_on=True, lang="it")
        self.assertIn("<b>A</b> accesa", update.effective_message.last)

        update = FakeUpdate()
        targets = targets + [{"entity_id": "light.b", "attributes": {"friendly_name": "B"}}]
        await self.b._apply(update, targets, turn_on=False, lang="it")
        self.assertIn("2 entità</b> spente", update.effective_message.last)

    async def test_confirmation_escapes_the_name(self):
        update = FakeUpdate()
        targets = [{"entity_id": "light.a", "attributes": {"friendly_name": "<b>x</b>"}}]
        await self.b._apply(update, targets, turn_on=True, lang="it")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", update.effective_message.last)


# -------------------------------------------------------------- temperature
class TemperatureTest(BotTestCase):
    async def test_whole_house_groups_by_room_and_offers_a_keyboard(self):
        update = FakeUpdate("/temperatura")
        await self.b.cmd_temperature(update, context(args=[]))
        text = update.effective_message.last
        self.assertIn("Temperature per stanza", text)
        self.assertIn("<b>Salone</b>", text)
        self.assertIn("21.5", text)
        self.assertTrue(payloads(update.effective_message.markup)[0].startswith("temp:"))

    async def test_a_room_shows_its_sensors_and_its_thermostat(self):
        update = FakeUpdate("/temperatura salone")
        await self.b.cmd_temperature(update, context(args=["salone"]))
        text = update.effective_message.last
        self.assertIn("21.5", text)
        self.assertIn("Termostato", text)
        self.assertIsNone(update.effective_message.markup)

    async def test_the_house_summary_leaves_thermostats_out(self):
        update = FakeUpdate("/temperatura")
        await self.b.cmd_temperature(update, context(args=[]))
        self.assertNotIn("Termostato", update.effective_message.last)

    async def test_a_decoy_device_class_is_never_reported(self):
        update = FakeUpdate("/temperatura")
        await self.b.cmd_temperature(update, context(args=[]))
        self.assertNotIn("Allarme gelo", update.effective_message.last)

    async def test_an_unrecognised_room_falls_back_to_a_sensor_search(self):
        update = FakeUpdate("/temperatura consumo")
        await self.b.cmd_temperature(update, context(args=["consumo"]))
        self.assertIn("Non ho trovato sensori", update.effective_message.last)

    async def test_a_room_without_sensors(self):
        update = FakeUpdate("/temperatura bagno")
        await self.b.cmd_temperature(update, context(args=["bagno"]))
        self.assertIn("Nessun sensore di temperatura", update.effective_message.last)

    async def test_an_installation_without_any_sensor(self):
        bare = [s for s in house() if s["entity_id"].startswith("light.")]
        b = make_bot(ha=FakeHA(states=bare))
        update = FakeUpdate("/temperatura")
        await b.cmd_temperature(update, context(args=[]))
        self.assertIn("Nessun sensore di temperatura trovato", update.effective_message.last)


# ---------------------------------------------------------------- callbacks
class CallbackTest(BotTestCase):
    async def handle(self, data, lang="it"):
        query = FakeQuery(data)
        await self.b._handle_callback(query, data, lang)
        return query

    async def test_area_shows_the_rooms_lights(self):
        query = await self.handle(f"area:{bot.tok('Cucina')}")
        self.assertIn("Luce cucina", query.last_edit)
        self.assertIn("Led cucina", query.last_edit)

    async def test_temp_shows_the_rooms_sensors(self):
        query = await self.handle(f"temp:{bot.tok('Salone')}")
        self.assertIn("21.5", query.last_edit)

    async def test_temp_excludes_a_decoy_device_class(self):
        query = await self.handle(f"temp:{bot.tok('Salone')}")
        self.assertNotIn("Allarme gelo", query.last_edit)

    async def test_temp_for_a_room_without_sensors_says_so(self):
        """Regression: the fallback used to be unreachable, leaving a bare heading."""
        query = await self.handle(f"temp:{bot.tok('Bagno')}")
        self.assertEqual(query.last_edit, i18n.t("it", "no_sensors"))

    async def test_temp_for_a_room_without_sensors_is_localised(self):
        query = await self.handle(f"temp:{bot.tok('Bagno')}", lang="en")
        self.assertEqual(query.last_edit, i18n.t("en", "no_sensors"))

    async def test_do_switches_one_entity_and_re_renders(self):
        query = await self.handle(f"do:on:{bot.tok('light.cucina')}")
        self.assertEqual(self.b.ha.calls, [("light", "turn_on", ["light.cucina"])])
        self.assertEqual(query.answers[0][0], i18n.t("it", "toast_on"))
        self.assertTrue(query.edits)

    async def test_all_switches_every_entity_in_the_token(self):
        token = bot.tok("light.cucina|light.studio")
        query = await self.handle(f"all:off:{token}")
        self.assertEqual(self.b.ha.calls, [("light", "turn_off", ["light.cucina", "light.studio"])])
        self.assertIn("(2)", query.answers[0][0])

    async def test_refresh_re_reads_the_states(self):
        query = await self.handle(f"refresh:{bot.tok('light.cucina')}")
        self.assertEqual(query.answers[0][0], i18n.t("it", "toast_refreshed"))
        self.assertEqual(self.b.ha.invalidations, 1)

    async def test_an_expired_token_is_a_stale_session_not_a_bug(self):
        for data in ("area:gone", "temp:gone", "do:on:gone", "all:off:gone", "refresh:gone"):
            with self.subTest(data=data):
                query = await self.handle(data)
                self.assertEqual(query.answers[0][0], i18n.t("it", "session_expired"))
                self.assertTrue(query.answers[0][1]["show_alert"])
                self.assertEqual(query.edits, [])

    async def test_an_unknown_payload_still_stops_the_spinner(self):
        query = await self.handle("fromtheotherbuild:x")
        self.assertEqual(query.answers, [(None, {})])

    async def test_a_home_assistant_failure_becomes_an_alert(self):
        b = make_bot(ha=FakeHA(fail_with=HomeAssistantError("down", kind="network")))
        update = FakeUpdate()
        update.callback_query = FakeQuery(f"do:on:{bot.tok('light.cucina')}")
        await b.on_callback(update, context())
        text = update.callback_query.answers[-1][0]
        self.assertIn("Rete non raggiungibile", text)
        self.assertLessEqual(len(text), 190)


class RefreshMessageTest(BotTestCase):
    async def test_an_identical_rendering_is_not_an_error(self):
        query = FakeQuery()
        query.edit_error = BadRequest("Message is not modified")
        await self.b._refresh_message(query, ["light.cucina"], "it")  # must not raise

    async def test_any_other_edit_failure_propagates(self):
        query = FakeQuery()
        query.edit_error = BadRequest("Chat not found")
        with self.assertRaises(BadRequest):
            await self.b._refresh_message(query, ["light.cucina"], "it")

    async def test_entities_that_vanished_are_dropped(self):
        query = FakeQuery()
        await self.b._refresh_message(query, ["light.cucina", "light.gone"], "it")
        self.assertIn("Luce cucina", query.last_edit)

    async def test_a_message_with_nothing_left_to_show_is_left_alone(self):
        query = FakeQuery()
        await self.b._refresh_message(query, ["light.gone"], "it")
        self.assertEqual(query.edits, [])

    async def test_several_entities_are_titled_with_their_room(self):
        query = FakeQuery()
        await self.b._refresh_message(query, ["light.cucina", "light.cucina_led"], "it")
        self.assertIn("<b>Cucina</b>", query.last_edit)

    async def test_one_entity_is_titled_with_its_name(self):
        query = FakeQuery()
        await self.b._refresh_message(query, ["light.cucina"], "it")
        self.assertIn("<b>Luce cucina</b>", query.last_edit)


# --------------------------------------------------------- natural language
class DispatchTest(BotTestCase):
    async def dispatch(self, text, chat_id=1):
        update = FakeUpdate(text, chat_id=chat_id)
        await self.b.on_text(update, context())
        return update.effective_message

    async def test_italian_sentences(self):
        msg = await self.dispatch("accendi la luce dello studio")
        self.assertEqual(self.b.ha.calls, [("light", "turn_on", ["light.studio"])])
        self.assertIn("accesa", msg.last)

    async def test_english_sentences(self):
        msg = await self.dispatch("turn everything off")
        self.assertEqual(self.b.ha.calls[0][1], "turn_off")
        self.assertIn("whole house", msg.last)

    async def test_a_question_about_temperature(self):
        msg = await self.dispatch("quanti gradi in salone")
        self.assertIn("21.5", msg.last)

    async def test_asking_what_is_on(self):
        msg = await self.dispatch("which lights are on")
        self.assertIn("Lights on", msg.last)

    async def test_an_unparsable_sentence_is_quoted_back(self):
        msg = await self.dispatch("il gatto sul tetto")
        self.assertIn("il gatto sul tetto", msg.last)
        self.assertEqual(self.b.ha.calls, [])

    async def test_the_quoted_text_is_escaped(self):
        msg = await self.dispatch("<script>alert(1)</script>")
        self.assertNotIn("<script>", msg.last)

    async def test_an_empty_message_gets_the_short_hint(self):
        update = FakeUpdate("   ")
        await self.b.on_text(update, context())
        self.assertEqual(update.effective_message.last, i18n.t("it", "not_understood_short"))

    async def test_the_conversation_language_follows_the_words(self):
        await self.dispatch("turn everything off", chat_id=5)
        self.assertEqual(self.b.chat_lang[5], "en")


# ------------------------------------------------------------------- voice
class VoiceTest(BotTestCase):
    def voice_update(self, **kwargs):
        update = FakeUpdate()
        update.effective_message.voice = FakeVoice(**kwargs)
        return update

    async def test_transcribes_echoes_and_executes(self):
        b = make_bot(stt_entity="stt.whisper")
        b.ha.stt_result = "accendi la luce dello studio"
        update = self.voice_update()
        await b.on_voice(update, context())
        texts = [t for t, _ in update.effective_message.sent]
        self.assertIn("accendi la luce dello studio", texts[0])   # echoed first
        self.assertEqual(b.ha.calls, [("light", "turn_on", ["light.studio"])])

    async def test_shows_a_typing_action_while_it_works(self):
        b = make_bot(stt_entity="stt.whisper")
        update = self.voice_update()
        await b.on_voice(update, context())
        self.assertEqual(len(update.effective_message.chat.actions), 1)

    async def test_uses_the_chats_language_for_the_engine(self):
        b = make_bot(stt_entity="stt.whisper")
        b.chat_lang[1] = "en"
        b.ha.stt_result = "turn on the light in the study"
        await b.on_voice(self.voice_update(), context())
        self.assertEqual(b.ha.stt_calls[0]["language"], "en-US")

    async def test_italian_chats_get_the_italian_tag(self):
        b = make_bot(stt_entity="stt.whisper")
        await b.on_voice(self.voice_update(), context())
        self.assertEqual(b.ha.stt_calls[0]["language"], "it-IT")

    async def test_declares_the_container_telegram_actually_sent(self):
        b = make_bot(stt_entity="stt.whisper")
        await b.on_voice(self.voice_update(mime_type="audio/ogg"), context())
        self.assertEqual(b.ha.stt_calls[0]["audio_format"], "ogg")
        self.assertEqual(b.ha.stt_calls[0]["codec"], "opus")

    async def test_without_an_engine_it_explains_instead_of_failing(self):
        b = make_bot(stt_entity=None)
        update = self.voice_update()
        await b.on_voice(update, context())
        self.assertIn("speech-to-text", update.effective_message.last)
        self.assertEqual(b.ha.stt_calls, [])

    async def test_an_oversized_clip_is_refused_before_it_is_downloaded(self):
        b = make_bot(stt_entity="stt.whisper")
        update = self.voice_update(size=bot.MAX_VOICE_BYTES + 1)
        await b.on_voice(update, context())
        self.assertIn("troppo lungo", update.effective_message.last)
        self.assertEqual(b.ha.stt_calls, [])

    async def test_a_clip_at_the_limit_is_accepted(self):
        b = make_bot(stt_entity="stt.whisper")
        await b.on_voice(self.voice_update(size=bot.MAX_VOICE_BYTES), context())
        self.assertEqual(len(b.ha.stt_calls), 1)

    async def test_silence_is_reported_not_executed(self):
        b = make_bot(stt_entity="stt.whisper")
        b.ha.stt_result = ""
        update = self.voice_update()
        await b.on_voice(update, context())
        self.assertIn("Non ho sentito nulla", update.effective_message.last)
        self.assertEqual(b.ha.calls, [])

    async def test_a_failing_engine_is_reported_in_the_chats_language(self):
        b = make_bot(ha=FakeHA(fail_with=HomeAssistantError("nope", kind="stt")), stt_entity="stt.whisper")
        b.chat_lang[1] = "en"
        update = self.voice_update()
        with self.assertLogs("hassgram", level="WARNING"):
            await b.on_voice(update, context())
        self.assertIn("could not transcribe", update.effective_message.last)
        self.assertIn("rejected the audio", update.effective_message.last)

    async def test_an_update_with_no_media_is_ignored(self):
        b = make_bot(stt_entity="stt.whisper")
        update = FakeUpdate()
        await b.on_voice(update, context())
        self.assertEqual(update.effective_message.sent, [])


# ------------------------------------------------------- startup and errors
class DiscoverSttTest(BotTestCase):
    async def test_an_explicit_entity_wins(self):
        b = make_bot(stt_entity="stt.configured")
        await b.discover_stt()
        self.assertEqual(b.stt_entity, "stt.configured")

    async def test_otherwise_the_first_engine_is_picked(self):
        b = make_bot()
        with self.assertLogs("hassgram", level="INFO"):
            await b.discover_stt()
        self.assertEqual(b.stt_entity, "stt.whisper")

    async def test_finding_nothing_is_a_warning_not_an_error(self):
        bare = [s for s in house() if not s["entity_id"].startswith("stt.")]
        b = make_bot(ha=FakeHA(states=bare))
        with self.assertLogs("hassgram", level="WARNING"):
            await b.discover_stt()
        self.assertIsNone(b.stt_entity)


class ErrorHandlerTest(BotTestCase):
    async def test_a_home_assistant_failure_is_explained_in_the_chats_language(self):
        b = make_bot()
        b.chat_lang[1] = "en"
        update = FakeUpdate()
        ctx = context(error=HomeAssistantError("connection refused", kind="network"), bot_data={"hass": b})
        with self.assertLogs("hassgram", level="ERROR"):
            await bot.on_error(update, ctx)
        text = update.effective_message.last
        self.assertIn("not responding", text)
        self.assertIn("Network unreachable", text)

    async def test_anything_else_gets_a_generic_apology(self):
        update = FakeUpdate()
        ctx = context(error=ValueError("internal detail"), bot_data={"hass": self.b})
        with self.assertLogs("hassgram", level="ERROR"):
            await bot.on_error(update, ctx)
        self.assertEqual(update.effective_message.last, i18n.t("it", "generic_error"))
        self.assertNotIn("internal detail", update.effective_message.last)

    async def test_an_update_without_a_message_only_logs(self):
        with self.assertLogs("hassgram", level="ERROR"):
            await bot.on_error(object(), context(error=ValueError("x")))

    async def test_it_does_not_raise_when_telegram_itself_is_failing(self):
        update = FakeUpdate()

        async def boom(*args, **kwargs):
            raise RuntimeError("telegram is down")

        update.effective_message.reply_text = boom
        with self.assertLogs("hassgram", level="ERROR"):
            await bot.on_error(update, context(error=ValueError("x"), bot_data={"hass": self.b}))

    async def test_it_survives_a_missing_bot_instance(self):
        update = FakeUpdate()
        with self.assertLogs("hassgram", level="ERROR"):
            await bot.on_error(update, context(error=ValueError("x"), bot_data={}))
        self.assertEqual(update.effective_message.last, i18n.t(i18n.DEFAULT_LANG, "generic_error"))


class LifecycleTest(BotTestCase):
    async def test_post_init_pings_and_discovers(self):
        b = make_bot()
        app = types.SimpleNamespace(bot_data={"hass": b})
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(app)
        self.assertEqual(b.stt_entity, "stt.whisper")

    async def test_post_init_fails_loudly_on_an_unreachable_instance(self):
        b = make_bot(ha=FakeHA(fail_with=HomeAssistantError("refused", kind="network")))
        app = types.SimpleNamespace(bot_data={"hass": b})
        with self.assertRaises(HomeAssistantError):
            await bot.post_init(app)

    async def test_post_shutdown_closes_the_session(self):
        closed = []
        b = make_bot()
        b.ha.aclose = lambda: closed.append(True) or _done()
        await bot.post_shutdown(types.SimpleNamespace(bot_data={"hass": b}))


async def _done():
    return None


# ------------------------------------------------------------------- wiring
class FakeApp:
    def __init__(self):
        self.bot_data = {}
        self.handlers = []
        self.error_handlers = []
        self.polling_kwargs = None

    def add_handler(self, handler):
        self.handlers.append(handler)

    def add_error_handler(self, handler):
        self.error_handlers.append(handler)

    def run_polling(self, **kwargs):
        self.polling_kwargs = kwargs


class FakeBuilder:
    def __init__(self, app):
        self.app = app
        self.token_value = None

    def token(self, value):
        self.token_value = value
        return self

    def post_init(self, fn):
        self.app.post_init = fn
        return self

    def post_shutdown(self, fn):
        self.app.post_shutdown = fn
        return self

    def build(self):
        return self.app


class MainTest(unittest.TestCase):
    """Covers configuration parsing and handler registration without polling."""

    def run_main(self, env):
        app = FakeApp()
        builder = FakeBuilder(app)
        with mock.patch.object(bot, "load_dotenv", lambda *a, **k: None), \
             mock.patch.dict(bot.os.environ, env, clear=True), \
             mock.patch.object(bot, "Application", types.SimpleNamespace(builder=lambda: builder)), \
             mock.patch.object(bot, "HomeAssistantClient", lambda *a, **k: FakeHA()):
            bot.main()
        return app

    BASE = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "HOME_ASSISTANT_API_URL": "http://ha.test/api/",
        "HOME_ASSISTANT_API_ACCESS_TOKEN": "tok",
    }

    def test_a_missing_required_variable_exits_with_a_message_naming_it(self):
        for missing in self.BASE:
            with self.subTest(missing=missing):
                env = {k: v for k, v in self.BASE.items() if k != missing}
                with self.assertRaises(SystemExit) as ctx:
                    self.run_main(env)
                self.assertIn(missing, str(ctx.exception))

    def test_every_command_is_registered_under_both_languages(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42"})
        commands = {c for h in app.handlers if isinstance(h, CommandHandler) for c in h.commands}
        for name in ("luci", "lights", "accese", "whatson", "accendi", "on", "spegni", "off",
                     "temperatura", "temperature", "stato", "state", "lingua", "language",
                     "start", "help", "aiuto"):
            self.assertIn(name, commands, name)

    def test_every_command_name_that_identifies_a_language_is_registered(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42"})
        commands = {c for h in app.handlers if isinstance(h, CommandHandler) for c in h.commands}
        self.assertEqual(set(bot.COMMAND_LANG) - commands, set())

    def test_the_catch_all_handlers_and_the_error_handler_are_wired(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42"})
        self.assertTrue(any(isinstance(h, CallbackQueryHandler) for h in app.handlers))
        self.assertEqual(sum(isinstance(h, MessageHandler) for h in app.handlers), 2)
        self.assertEqual(app.error_handlers, [bot.on_error])

    def test_chat_ids_tolerate_spaces_quotes_and_trailing_separators(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": ' "42", -100123 , '})
        self.assertEqual(app.bot_data["hass"].allowed_chats, {42, -100123})

    def test_an_empty_allow_list_is_permitted_but_warned_about(self):
        with self.assertLogs("hassgram", level="WARNING") as logs:
            app = self.run_main(self.BASE)
        self.assertEqual(app.bot_data["hass"].allowed_chats, set())
        self.assertIn("anyone", " ".join(logs.output))

    def test_stt_language_defaults(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42"})
        self.assertEqual(app.bot_data["hass"].stt_languages, {"it": "it-IT", "en": "en-US"})

    def test_the_legacy_stt_language_variable_still_works(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42", "STT_LANGUAGE": "it-CH"})
        self.assertEqual(app.bot_data["hass"].stt_languages["it"], "it-CH")

    def test_the_explicit_variable_wins_over_the_legacy_one(self):
        env = {**self.BASE, "TELEGRAM_CHAT_ID": "42", "STT_LANGUAGE": "it-CH", "STT_LANGUAGE_IT": "it-IT"}
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main(env)
        self.assertEqual(app.bot_data["hass"].stt_languages["it"], "it-IT")

    def test_bot_language_sets_the_default(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42", "BOT_LANGUAGE": "en"})
        self.assertEqual(app.bot_data["hass"].default_lang, "en")

    def test_polling_asks_for_every_update_type(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42"})
        self.assertIn("allowed_updates", app.polling_kwargs)


if __name__ == "__main__":
    unittest.main()
