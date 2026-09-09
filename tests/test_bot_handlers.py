"""Tests for the asynchronous handlers: commands, callbacks, voice, wiring.

These drive :class:`bot.HassBot` through the fakes in :mod:`tests.fakes`, which
implement only the handful of Telegram attributes the handlers actually reach
for. Nothing here needs a network, a token or an event loop of its own.
"""

from __future__ import annotations

import asyncio
import re
import types
import unittest
from unittest import mock

from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from tests.fakes import (
    AREAS, FakeBot, FakeHA, FakeQuery, FakeUpdate, FakeVoice, app as fake_app, buttons,
    context, house, payloads,
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
        b = make_bot(runnables_refresh=0)
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(fake_app(b))
        self.assertEqual(b.stt_entity, "stt.whisper")

    async def test_post_init_reads_the_runnable_catalogue_once(self):
        b = make_bot(runnables_refresh=0)
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(fake_app(b))
        self.assertEqual(
            [s["entity_id"] for s in await b.runnables()],
            ["script.aperitivo", "script.buonanotte", "automation.risveglio", "automation.vacanza"],
        )

    async def test_post_init_starts_and_post_shutdown_stops_the_refresh_cycle(self):
        b = make_bot(runnables_refresh=30)
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(fake_app(b))
        self.assertIsNotNone(b._refresh_task)
        await bot.post_shutdown(fake_app(b))
        self.assertIsNone(b._refresh_task)

    async def test_post_init_publishes_a_command_menu_per_language(self):
        b = make_bot(runnables_refresh=0)
        app = fake_app(b)
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(app)
        codes = [code for code, _ in app.bot.command_menus]
        self.assertEqual(codes, [None, "it", "en"])

    async def test_the_command_menu_offers_esegui_and_run(self):
        b = make_bot(runnables_refresh=0)
        app = fake_app(b)
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(app)
        by_code = dict(app.bot.command_menus)
        self.assertIn("esegui", [name for name, _ in by_code["it"]])
        self.assertIn("run", [name for name, _ in by_code["en"]])

    async def test_the_default_menu_follows_the_bot_language(self):
        b = make_bot(runnables_refresh=0, default_lang="en")
        app = fake_app(b)
        with self.assertLogs("hassgram", level="INFO"):
            await bot.post_init(app)
        self.assertEqual(dict(app.bot.command_menus)[None], dict(app.bot.command_menus)["en"])

    async def test_a_rejected_command_menu_does_not_stop_the_bot(self):
        b = make_bot(runnables_refresh=0)
        app = fake_app(b, bot=FakeBot(fail_with=TelegramError("flood")))
        with self.assertLogs("hassgram", level="WARNING") as logs:
            await bot.post_init(app)
        self.assertIn("command menu", " ".join(logs.output))

    async def test_post_init_fails_loudly_on_an_unreachable_instance(self):
        b = make_bot(ha=FakeHA(fail_with=HomeAssistantError("refused", kind="network")))
        with self.assertRaises(HomeAssistantError):
            await bot.post_init(fake_app(b))

    async def test_post_shutdown_closes_the_session(self):
        closed = []
        b = make_bot()
        b.ha.aclose = lambda: closed.append(True) or _done()
        await bot.post_shutdown(fake_app(b))
        self.assertEqual(closed, [True])


async def _done():
    return None


# ------------------------------------------------------- running things
class RunCommandTest(BotTestCase):
    """``/esegui``: listing, resolution, and the service each domain is run with."""

    async def test_no_argument_lists_every_runnable_grouped_by_domain(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context([]))
        text = update.effective_message.last
        for name in ("Aperitivo", "Buonanotte", "Risveglio", "Vacanza"):
            self.assertIn(name, text)
        self.assertIn(i18n.t("it", "domain_script"), text)
        self.assertIn(i18n.t("it", "domain_automation"), text)
        self.assertEqual(self.b.ha.calls, [])

    async def test_a_scene_is_never_listed(self):
        """Scenes are not runnable: a scene in the house must not reach the menu."""
        update = FakeUpdate()
        await self.b.cmd_run(update, context([]))
        self.assertNotIn("Cinema", update.effective_message.last)
        self.assertNotIn("scene.", update.effective_message.last)

    async def test_the_listing_marks_a_disabled_automation(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context([]))
        line = next(l for l in update.effective_message.last.splitlines() if "Vacanza" in l)
        self.assertIn(i18n.t("it", "automation_disabled"), line)
        other = next(l for l in update.effective_message.last.splitlines() if "Risveglio" in l)
        self.assertNotIn(i18n.t("it", "automation_disabled"), other)

    async def test_the_listing_offers_one_run_button_per_entity(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context([]))
        data = payloads(update.effective_message.markup)
        self.assertEqual(len(data), 4)  # two scripts, two automations
        self.assertTrue(all(d.startswith("run:") for d in data))

    async def test_a_scene_is_not_runnable(self):
        """Naming a scene finds nothing rather than turning it on."""
        update = FakeUpdate()
        await self.b.cmd_run(update, context(["cinema"]))
        self.assertEqual(self.b.ha.calls, [])
        self.assertIn("cinema", update.effective_message.last)

    async def test_a_script_is_started_with_turn_on(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context(["buonanotte"]))
        self.assertEqual(self.b.ha.calls, [("script", "turn_on", ["script.buonanotte"])])

    async def test_an_automation_is_triggered_not_turned_on(self):
        """The whole point of the feature: turn_on would only enable it."""
        update = FakeUpdate()
        await self.b.cmd_run(update, context(["risveglio"]))
        self.assertEqual(self.b.ha.calls, [("automation", "trigger", ["automation.risveglio"])])

    async def test_a_disabled_automation_can_still_be_triggered_by_hand(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context(["vacanza"]))
        self.assertEqual(self.b.ha.calls, [("automation", "trigger", ["automation.vacanza"])])

    async def test_the_confirmation_names_what_was_started(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context(["buonanotte"]))
        self.assertIn("Buonanotte", update.effective_message.last)

    async def test_an_unknown_name_runs_nothing_and_says_so(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context(["cioccolato"]))
        self.assertEqual(self.b.ha.calls, [])
        self.assertIn("cioccolato", update.effective_message.last)

    async def test_a_house_with_nothing_runnable_says_so(self):
        b = make_bot(ha=FakeHA(states=[s for s in house() if not s["entity_id"].startswith(
            ("script.", "automation."))]))
        update = FakeUpdate()
        await b.cmd_run(update, context([]))
        self.assertEqual(update.effective_message.last, i18n.t("it", "no_runnables"))

    async def test_an_ambiguous_name_offers_a_choice_and_runs_nothing(self):
        states = house() + [
            {"entity_id": "script.buonanotte_estate", "state": "off",
             "attributes": {"friendly_name": "Buonanotte estate"}},
        ]
        b = make_bot(ha=FakeHA(states=states))
        update = FakeUpdate()
        # Not "buonanotte": that is the exact name of one script, and an exact name
        # wins over the ambiguity, exactly as it does for /accendi.
        await b.cmd_run(update, context(["buonanott"]))
        self.assertEqual(b.ha.calls, [])
        self.assertEqual(len(payloads(update.effective_message.markup)), 2)

    async def test_an_exact_name_wins_over_the_ambiguity(self):
        states = house() + [
            {"entity_id": "script.buonanotte_estate", "state": "off",
             "attributes": {"friendly_name": "Buonanotte estate"}},
        ]
        b = make_bot(ha=FakeHA(states=states))
        await b.cmd_run(FakeUpdate(), context(["buonanotte"]))
        self.assertEqual(b.ha.calls, [("script", "turn_on", ["script.buonanotte"])])

    def test_only_scripts_and_automations_are_in_the_runnable_pool(self):
        """The pool is domain-filtered, so no fuzzy match reaches a lamp or a scene."""
        ids = [s["entity_id"] for s in bot.HassBot._runnables(house())]
        self.assertEqual(
            ids,
            ["script.aperitivo", "script.buonanotte", "automation.risveglio", "automation.vacanza"],
        )

    def test_every_runnable_domain_has_a_service_and_an_icon(self):
        for domain in bot.RUN_DOMAINS:
            self.assertIn(domain, bot.RUN_SERVICES)
            self.assertIn(domain, bot.RUN_ICONS)
            self.assertIn(f"domain_{domain}", i18n.MESSAGES)

    async def test_the_run_keyboard_executes_the_entity_it_carries(self):
        update = FakeUpdate()
        await self.b.cmd_run(update, context([]))
        data = next(d for d in payloads(update.effective_message.markup) if d.startswith("run:"))
        query = FakeQuery(data)
        await self.b._handle_callback(query, data, "it")
        self.assertEqual(len(self.b.ha.calls), 1)
        self.assertEqual(query.edits, [])  # a menu, not a status display: left untouched

    async def test_an_expired_run_token_runs_nothing(self):
        query = FakeQuery("run:deadbeef")
        await self.b._handle_callback(query, query.data, "it")
        self.assertEqual(self.b.ha.calls, [])
        self.assertEqual(query.answers[0][0], i18n.t("it", "session_expired"))

    async def test_run_ids_skips_a_domain_it_has_no_service_for(self):
        """A scene reaching _run_ids is a bug upstream: skip it, do not guess."""
        with self.assertLogs("hassgram", level="WARNING"):
            await self.b._run_ids(["light.cucina", "scene.cinema", "script.buonanotte"])
        self.assertEqual(self.b.ha.calls, [("script", "turn_on", ["script.buonanotte"])])

    async def test_a_sentence_runs_an_automation(self):
        update = FakeUpdate("esegui l'automazione risveglio")
        await self.b.on_text(update, context())
        self.assertEqual(self.b.ha.calls, [("automation", "trigger", ["automation.risveglio"])])

    async def test_an_english_sentence_runs_a_script(self):
        update = FakeUpdate("run the buonanotte script please")
        await self.b.on_text(update, context())
        self.assertEqual(self.b.ha.calls, [("script", "turn_on", ["script.buonanotte"])])


# ------------------------------------------------------- listing in pages
def catalogue(automations=0, scripts=0, name="Automazione numero {i:03d} descrittiva"):
    """A catalogue of arbitrary size, for the paging tests."""
    return (
        [{"entity_id": f"script.p{i:03d}", "state": "off",
          "attributes": {"friendly_name": f"Script {i:03d}"}} for i in range(scripts)]
        + [{"entity_id": f"automation.a{i:03d}", "state": "on" if i % 3 else "off",
            "attributes": {"friendly_name": name.format(i=i)}} for i in range(automations)]
    )


class RunnablesPagingTest(BotTestCase):
    """A catalogue too big for one message is split, not truncated."""

    async def send(self, states):
        b = make_bot(ha=FakeHA(states=states), runnables_refresh=0)
        update = FakeUpdate()
        await b.cmd_run(update, context([]))
        return update.effective_message.sent

    async def test_a_small_catalogue_still_fits_one_message(self):
        self.assertEqual(len(await self.send(house())), 1)

    async def test_a_large_catalogue_is_split_across_messages(self):
        sent = await self.send(catalogue(automations=70, scripts=5))
        self.assertGreater(len(sent), 1)

    async def test_no_page_is_ever_truncated(self):
        for text, _ in await self.send(catalogue(automations=200)):
            self.assertNotIn(i18n.t("it", "truncated"), text)

    async def test_every_page_fits_telegram_s_limit(self):
        for text, _ in await self.send(catalogue(automations=200)):
            self.assertLessEqual(len(text), 4096)

    async def test_every_entity_named_in_a_page_has_a_button_in_that_page(self):
        """Text and keyboard are built from the same list, page by page."""
        for text, kwargs in await self.send(catalogue(automations=70, scripts=5)):
            named = re.findall(r"<code>((?:script|automation)\.[^<]+)</code>", text)
            self.assertEqual(len(named), len(payloads(kwargs["reply_markup"])))

    async def test_no_entity_is_dropped_and_none_is_listed_twice(self):
        states = catalogue(automations=70, scripts=8)
        listed = []
        for text, _ in await self.send(states):
            listed += re.findall(r"<code>((?:script|automation)\.[^<]+)</code>", text)
        self.assertEqual(sorted(listed), sorted(s["entity_id"] for s in states))

    async def test_a_continued_domain_repeats_its_heading(self):
        sent = await self.send(catalogue(automations=70))
        for text, _ in sent[1:]:
            self.assertIn(i18n.t("it", "domain_automation"), text.splitlines()[0])

    async def test_only_the_first_page_carries_the_title(self):
        sent = await self.send(catalogue(automations=70))
        self.assertIn(i18n.t("it", "runnables_title"), sent[0][0])
        for text, _ in sent[1:]:
            self.assertNotIn(i18n.t("it", "runnables_title"), text)

    async def test_only_the_last_page_carries_the_hint(self):
        sent = await self.send(catalogue(automations=70))
        for text, _ in sent[:-1]:
            self.assertNotIn(i18n.t("it", "run_tap_hint"), text)
        self.assertIn(i18n.t("it", "run_tap_hint"), sent[-1][0])

    async def test_a_pathological_catalogue_is_capped_and_says_so(self):
        """The cap is a burst guard; unlike clip() it reports what it left out.

        The catalogue is sized off the constants rather than off a round number, so
        the test keeps testing the cap when either of them is retuned.
        """
        total = bot.MAX_RUN_PAGES * bot.MAX_BUTTONS + 7
        sent = await self.send(catalogue(automations=total))
        self.assertEqual(len(sent), bot.MAX_RUN_PAGES)
        listed = sum(len(payloads(kw["reply_markup"])) for _, kw in sent)
        self.assertLess(listed, total)
        self.assertIn(str(total - listed), sent[-1][0])

    async def test_a_catalogue_that_fits_the_cap_is_never_capped(self):
        """One page short of the cap must still be listed in full."""
        total = (bot.MAX_RUN_PAGES - 1) * bot.MAX_BUTTONS
        sent = await self.send(catalogue(automations=total))
        self.assertLessEqual(len(sent), bot.MAX_RUN_PAGES)
        listed = sum(len(payloads(kw["reply_markup"])) for _, kw in sent)
        self.assertEqual(listed, total)
        capped = i18n.MESSAGES["run_list_capped"]["it"].split("{")[0]
        for text, _ in sent:
            self.assertNotIn(capped, text)

    async def test_the_cap_holds_at_whatever_value_it_is_set_to(self):
        with mock.patch.object(bot, "MAX_RUN_PAGES", 3):
            sent = await self.send(catalogue(automations=3 * bot.MAX_BUTTONS + 5))
        self.assertEqual(len(sent), 3)

    def test_a_page_break_falls_on_the_character_budget(self):
        """Not only on the entity count: long names must close a page early."""
        states = catalogue(automations=40, name="{i:03d} " + "n" * 200)
        pages = bot.HassBot._runnables_pages(states, "it")
        self.assertTrue(any(len(page) < bot.MAX_BUTTONS for _, page in pages[:-1]))
        for text, _ in pages:
            self.assertLessEqual(len(text), bot.MAX_MESSAGE_CHARS)

    def test_one_entity_per_page_still_produces_a_coherent_listing(self):
        pages = bot.HassBot._runnables_pages(house(), "it", per_page=1)
        self.assertEqual([len(page) for _, page in pages], [1, 1, 1, 1])
        self.assertIn(i18n.t("it", "run_tap_hint"), pages[-1][0])


# ------------------------------------------------ the runnable catalogue cache
class RunnablesCacheTest(BotTestCase):
    """The catalogue is read at startup and refreshed on a cycle, not per command."""

    async def test_esegui_answers_from_the_cache_without_reading_states(self):
        b = make_bot(runnables_refresh=0)
        await b.refresh_runnables()
        reads = b.ha.state_reads
        await b.cmd_run(FakeUpdate(), context([]))
        await b.cmd_run(FakeUpdate(), context(["cinema"]))
        self.assertEqual(b.ha.state_reads, reads)
        self.assertEqual(b.ha.area_reads, 1)  # rendered once, then cached by the client

    async def test_a_cold_cache_is_filled_on_demand(self):
        """The startup read can have failed; the command must still work."""
        b = make_bot(runnables_refresh=0)
        update = FakeUpdate()
        await b.cmd_run(update, context([]))
        self.assertEqual(b.ha.state_reads, 1)
        self.assertIn("Buonanotte", update.effective_message.last)

    async def test_the_menu_still_lists_while_home_assistant_is_down(self):
        """The point of caching: the catalogue outlives the instance being reachable."""
        b = make_bot(runnables_refresh=0)
        await b.refresh_runnables()
        b.ha.fail_with = HomeAssistantError("refused", kind="network")
        update = FakeUpdate()
        with self.assertLogs("hassgram", level="WARNING"):  # areas unavailable
            await b.cmd_run(update, context([]))
        self.assertIn("Buonanotte", update.effective_message.last)

    async def test_running_while_home_assistant_is_down_still_fails_loudly(self):
        """Listing degrades gracefully; executing must not pretend to have worked."""
        b = make_bot(runnables_refresh=0)
        await b.refresh_runnables()
        b.ha.fail_with = HomeAssistantError("refused", kind="network")
        with self.assertLogs("hassgram", level="WARNING"), \
             self.assertRaises(HomeAssistantError):
            await b.cmd_run(FakeUpdate(), context(["buonanotte"]))
        self.assertEqual(b.ha.calls, [])

    async def test_a_refresh_picks_up_a_newly_created_script(self):
        b = make_bot(runnables_refresh=0)
        await b.refresh_runnables()
        b.ha._states = house() + [
            {"entity_id": "script.festa", "state": "off", "attributes": {"friendly_name": "Festa"}},
        ]
        self.assertNotIn("script.festa", [s["entity_id"] for s in await b.runnables()])
        await b.refresh_runnables()
        self.assertIn("script.festa", [s["entity_id"] for s in await b.runnables()])

    async def test_the_cycle_refreshes_and_survives_a_failed_read(self):
        b = make_bot(runnables_refresh=0.01)
        await b.refresh_runnables()
        b.ha.fail_with = HomeAssistantError("refused", kind="network")
        b.start_refreshing()
        with self.assertLogs("hassgram", level="WARNING") as logs:
            await asyncio.sleep(0.05)
        await b.stop_refreshing()
        self.assertIn("refresh", " ".join(logs.output))
        # The previous catalogue is still there: a failed read never empties it.
        self.assertEqual(len(await b.runnables()), 4)

    async def test_a_zero_interval_starts_no_cycle(self):
        b = make_bot(runnables_refresh=0)
        b.start_refreshing()
        self.assertIsNone(b._refresh_task)

    async def test_starting_twice_leaves_one_task(self):
        b = make_bot(runnables_refresh=30)
        b.start_refreshing()
        first = b._refresh_task
        b.start_refreshing()
        self.assertIs(b._refresh_task, first)
        await b.stop_refreshing()

    async def test_stopping_a_cycle_that_never_started_is_harmless(self):
        await make_bot(runnables_refresh=0).stop_refreshing()


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
                     "start", "help", "aiuto", "esegui", "run"):
            self.assertIn(name, commands, name)

    def test_every_command_in_the_menu_is_actually_registered(self):
        """A menu entry with no handler is a button that does nothing."""
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42"})
        commands = {c for h in app.handlers if isinstance(h, CommandHandler) for c in h.commands}
        for lang, menu in i18n.COMMAND_MENU.items():
            for name, _ in menu:
                with self.subTest(lang=lang, command=name):
                    self.assertIn(name, commands)

    def test_the_refresh_interval_can_be_configured_and_tolerates_a_typo(self):
        with self.assertLogs("hassgram", level="INFO"):
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42", "RUNNABLES_REFRESH_SECONDS": "60"})
        self.assertEqual(app.bot_data["hass"].runnables_refresh, 60.0)
        with self.assertLogs("hassgram", level="WARNING") as logs:
            app = self.run_main({**self.BASE, "TELEGRAM_CHAT_ID": "42", "RUNNABLES_REFRESH_SECONDS": "presto"})
        self.assertEqual(app.bot_data["hass"].runnables_refresh, bot.RUNNABLES_REFRESH_SECONDS)
        self.assertIn("RUNNABLES_REFRESH_SECONDS", " ".join(logs.output))

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
