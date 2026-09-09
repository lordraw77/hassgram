"""Tests for the synchronous parts of :mod:`bot`.

Everything here is callable without an event loop: the token store, the message
clipper, the error renderer, the entity selectors and the keyboard builders.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.fakes import AREAS, buttons, house, payloads

import bot
import entities as ent
import i18n
import views
import voice
from ha_client import HomeAssistantError


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        views._tokens.clear()

    def test_token_fits_in_callback_data(self):
        token = views.tok("light.salone_principale")
        self.assertEqual(len(token), 12)
        # a whole payload must stay under Telegram's 64-byte cap
        self.assertLess(len(f"do:off:{token}".encode()), 64)

    def test_round_trip(self):
        value = "light.a|light.b|light.c"
        self.assertEqual(views.untok(views.tok(value)), value)

    def test_same_value_reuses_the_same_token(self):
        self.assertEqual(views.tok("Salone"), views.tok("Salone"))
        self.assertEqual(len(views._tokens), 1)

    def test_unknown_token_is_none_not_an_error(self):
        self.assertIsNone(views.untok("deadbeef1234"))

    def test_store_is_bounded(self):
        with mock.patch.object(views, "MAX_TOKENS", 3):
            for i in range(10):
                views.tok(f"value-{i}")
            self.assertEqual(len(views._tokens), 3)

    def test_eviction_drops_the_oldest(self):
        with mock.patch.object(views, "MAX_TOKENS", 3):
            a, b, c = (views.tok(v) for v in ("a", "b", "c"))
            views.tok("d")
            self.assertIsNone(views.untok(a))
            self.assertEqual(views.untok(b), "b")

    def test_use_refreshes_the_position(self):
        """A keyboard still in use must not expire under a busy one."""
        with mock.patch.object(views, "MAX_TOKENS", 3):
            a, b, c = (views.tok(v) for v in ("a", "b", "c"))
            views.untok(a)          # a is now the most recently used
            views.tok("d")          # evicts b, not a
            self.assertEqual(views.untok(a), "a")
            self.assertIsNone(views.untok(b))


class EscapeTest(unittest.TestCase):
    def test_escapes_html_metacharacters(self):
        self.assertEqual(bot.esc("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;")

    def test_stringifies_anything(self):
        self.assertEqual(bot.esc(None), "None")
        self.assertEqual(bot.esc(21.5), "21.5")


class ClipTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(bot.clip("hello", "it", limit=100), "hello")

    def test_text_exactly_at_the_limit_is_untouched(self):
        self.assertEqual(bot.clip("x" * 10, "it", limit=10), "x" * 10)

    def test_cut_falls_on_a_line_boundary(self):
        """Cutting inside a ``<b>`` tag would make Telegram reject the message.

        The limit deliberately falls in the *middle* of the second line, so a
        naive ``text[:limit]`` would produce a partial line and pass a weaker
        assertion.
        """
        text = "<b>aaa</b>\n<b>bbbbbbbbbb</b>\n<b>ccc</b>"
        out = bot.clip(text, "it", limit=20)
        body = out[: -len("\n" + i18n.t("it", "truncated"))]
        self.assertEqual(body, "<b>aaa</b>")
        # every emitted line must be one of the originals, never half of one
        for line in body.split("\n"):
            self.assertIn(line, text.split("\n"))

    def test_a_line_that_ends_exactly_on_the_limit_is_kept_whole(self):
        out = bot.clip("aaa\nbbb\nccc", "it", limit=8)
        self.assertTrue(out.startswith("aaa\nbbb"))
        self.assertNotIn("ccc", out)

    def test_notice_is_appended_and_localised(self):
        self.assertIn(i18n.t("it", "truncated"), bot.clip("a\n" * 50, "it", limit=10))
        self.assertIn(i18n.t("en", "truncated"), bot.clip("a\n" * 50, "en", limit=10))

    def test_a_single_over_long_line_is_cut_at_the_limit(self):
        out = bot.clip("x" * 100, "it", limit=10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn(i18n.t("it", "truncated"), out)

    def test_leading_newline_does_not_produce_an_empty_body(self):
        out = bot.clip("\n" + "x" * 100, "it", limit=10)
        self.assertIn("x", out)

    def test_default_limit_leaves_room_under_telegrams_cap(self):
        out = bot.clip("word\n" * 5000)
        self.assertLess(len(out), 4096)


class HaErrorTextTest(unittest.TestCase):
    def test_each_kind_gets_its_own_wording(self):
        cases = [
            (HomeAssistantError("connection refused", kind="network"), "Rete non raggiungibile", "Network unreachable"),
            (HomeAssistantError("bad token", kind="http", status=401), "ha risposto 401", "answered 401"),
            (HomeAssistantError("nope", kind="stt"), "trascrizione", "transcription"),
        ]
        for exc, it_fragment, en_fragment in cases:
            with self.subTest(kind=exc.kind):
                self.assertIn(it_fragment, bot.ha_error_text("it", exc))
                self.assertIn(en_fragment.lower(), bot.ha_error_text("en", exc).lower())

    def test_detail_is_carried_through(self):
        exc = HomeAssistantError("connection refused", kind="network")
        self.assertIn("connection refused", bot.ha_error_text("en", exc))

    def test_generic_kind_is_just_the_detail(self):
        self.assertEqual(bot.ha_error_text("en", HomeAssistantError("boom")), "boom")

    def test_unknown_kind_does_not_raise(self):
        exc = HomeAssistantError("odd", kind="from_the_future")
        self.assertEqual(bot.ha_error_text("en", exc), "odd")

    def test_a_plain_exception_falls_back_to_its_str(self):
        """:func:`bot.on_error` sees whatever was raised, not only client errors."""
        self.assertEqual(bot.ha_error_text("en", ValueError("kaboom")), "kaboom")

    def test_no_italian_leaks_into_an_english_answer(self):
        exc = HomeAssistantError("bad token", kind="http", status=401)
        self.assertNotIn("ha risposto", bot.ha_error_text("en", exc))


class SelectorTest(unittest.TestCase):
    def setUp(self):
        self.states = house()

    def ids(self, result):
        return [s["entity_id"] for s in result]

    def test_lights_defaults_to_the_light_domain(self):
        result = self.ids(bot.HassBot._lights(self.states))
        self.assertTrue(all(i.startswith("light.") for i in result))
        self.assertNotIn("switch.presa_tv", result)

    def test_lights_can_include_switches(self):
        result = self.ids(bot.HassBot._lights(self.states, domains=bot.LIGHT_DOMAINS))
        self.assertIn("switch.presa_tv", result)

    def test_lights_by_area(self):
        result = self.ids(bot.HassBot._lights(self.states, AREAS, area="Cucina"))
        self.assertEqual(result, ["light.cucina", "light.cucina_led"])

    def test_lights_by_unknown_area_is_empty(self):
        self.assertEqual(bot.HassBot._lights(self.states, AREAS, area="Cantina"), [])

    def test_temp_sensors_ignore_a_matching_device_class_on_another_domain(self):
        """``binary_sensor.gelo`` carries ``device_class: temperature`` and is not a reading."""
        result = self.ids(bot.HassBot._temp_sensors(self.states))
        self.assertNotIn("binary_sensor.gelo", result)
        self.assertEqual(result, ["sensor.temp_salone", "sensor.umid_salone", "sensor.temp_cucina"])

    def test_temp_sensors_ignore_other_device_classes(self):
        self.assertNotIn("sensor.consumo", self.ids(bot.HassBot._temp_sensors(self.states)))

    def test_temp_sensors_exclude_climate(self):
        # climate carries its reading in an attribute and is handled separately
        self.assertNotIn("climate.termo_salone", self.ids(bot.HassBot._temp_sensors(self.states)))

    def test_temp_sensors_by_area(self):
        result = self.ids(bot.HassBot._temp_sensors(self.states, AREAS, area="Salone"))
        self.assertEqual(result, ["sensor.temp_salone", "sensor.umid_salone"])

    def test_a_room_with_only_a_decoy_has_no_sensors(self):
        self.assertEqual(bot.HassBot._temp_sensors(self.states, AREAS, area="Bagno"), [])


class BulkTargetsTest(unittest.TestCase):
    def setUp(self):
        self.lights = bot.HassBot._lights(house(), domains=bot.LIGHT_DOMAINS)

    def ids(self, **kwargs):
        return [s["entity_id"] for s in bot.HassBot._bulk_targets(self.lights, AREAS, **kwargs)]

    def test_never_touches_switches(self):
        """"Turn the house off" must not cut power to the fridge."""
        self.assertNotIn("switch.presa_tv", self.ids())
        self.assertNotIn("switch.caldaia", self.ids())

    def test_skips_unreachable_entities(self):
        # so the count in the confirmation is what actually received the command
        self.assertNotIn("light.cucina_led", self.ids())

    def test_restricted_to_one_room(self):
        self.assertEqual(self.ids(area="Cucina"), ["light.cucina"])

    def test_whole_house(self):
        self.assertEqual(
            self.ids(),
            ["light.salone_principale", "light.salone_faretti", "light.cucina",
             "light.studio", "light.corridoio", "light.terrazza"],
        )


class AreaMatchingTest(unittest.TestCase):
    def setUp(self):
        self.b = bot.HassBot(None, set())

    def test_is_home_accepts_both_languages(self):
        self.assertTrue(self.b._is_home("casa"))
        self.assertTrue(self.b._is_home("Tutta la casa"))
        self.assertTrue(self.b._is_home("EVERYTHING"))

    def test_is_home_is_not_a_substring_match(self):
        self.assertFalse(self.b._is_home("casetta"))
        self.assertFalse(self.b._is_home("luce di casa"))

    def test_exact_area_match_preserves_home_assistants_spelling(self):
        self.assertEqual(self.b._match_area("salone", AREAS), "Salone")
        self.assertEqual(self.b._match_area("SALONE", AREAS), "Salone")

    def test_partial_match_in_either_direction(self):
        areas = {"light.a": "Camera da letto"}
        self.assertEqual(self.b._match_area("camera", areas), "Camera da letto")
        self.assertEqual(self.b._match_area("camera da letto grande", areas), "Camera da letto")

    def test_ambiguity_returns_none_rather_than_guessing(self):
        areas = {"light.a": "Camera", "light.b": "Cameretta"}
        self.assertIsNone(self.b._match_area("camer", areas))

    def test_empty_query(self):
        self.assertIsNone(self.b._match_area("", AREAS))
        self.assertIsNone(self.b._match_area("   ", AREAS))

    def test_no_match(self):
        self.assertIsNone(self.b._match_area("cantina", AREAS))

    def test_area_name_translates_only_the_sentinel(self):
        self.assertEqual(views.area_name("Salone", "en"), "Salone")
        self.assertEqual(views.area_name(ent.NO_AREA, "it"), "Senza stanza")
        self.assertEqual(views.area_name(ent.NO_AREA, "en"), "No room")


class RenderingTest(unittest.TestCase):
    def setUp(self):
        views._tokens.clear()
        self.b = bot.HassBot(None, set())
        self.states = house()
        self.lights = bot.HassBot._lights(self.states)

    def test_areas_summary_counts_unreachable_as_off(self):
        text = views.areas_summary(self.lights, AREAS, "it")
        # Cucina has two lights, one off and one unavailable
        self.assertIn("<b>Cucina</b>: 0/2", text)
        self.assertIn("<b>Salone</b>: 1/2", text)

    def test_areas_summary_localises_the_no_area_bucket(self):
        self.assertIn("Senza stanza", views.areas_summary(self.lights, AREAS, "it"))
        self.assertIn("No room", views.areas_summary(self.lights, AREAS, "en"))

    def test_lights_text_escapes_names(self):
        text = views.lights_text("Terrazza", self.lights, "it")
        self.assertIn("Luce &lt;terrazza&gt; &amp; co", text)
        self.assertNotIn("<terrazza>", text)

    def test_lights_text_escapes_the_title(self):
        self.assertIn("&lt;script&gt;", views.lights_text("<script>", [], "it"))

    def test_lights_keyboard_offers_the_opposite_action(self):
        lights = [s for s in self.lights if s["entity_id"] in ("light.salone_principale", "light.cucina")]
        data = payloads(views.lights_keyboard(lights, "it"))
        self.assertTrue(data[0].startswith("do:off:"))   # salone is on
        self.assertTrue(data[1].startswith("do:on:"))    # cucina is off

    def test_lights_keyboard_ends_with_bulk_and_refresh(self):
        kb = views.lights_keyboard(self.lights, "en")
        self.assertEqual(buttons(kb)[-3:], ["🟡 Turn all on", "⚫ Turn all off", "🔄 Refresh"])

    def test_bulk_buttons_share_one_token_with_everything_displayed(self):
        kb = views.lights_keyboard(self.lights, "it")
        data = payloads(kb)
        on_token = data[-3].split(":")[-1]
        self.assertEqual(views.untok(on_token).split("|"), [s["entity_id"] for s in self.lights])

    def test_lights_keyboard_is_capped(self):
        with mock.patch.object(views, "MAX_BUTTONS", 2):
            kb = views.lights_keyboard(self.lights, "it")
            self.assertEqual(len(kb.inline_keyboard), 2 + 2)  # 2 lights + bulk row + refresh row

    def test_areas_keyboard_drops_the_no_area_bucket(self):
        labels = buttons(views.areas_keyboard(self.lights, AREAS))
        self.assertEqual(labels, ["Cucina", "Salone", "Studio", "Terrazza"])

    def test_areas_keyboard_carries_the_prefix(self):
        data = payloads(views.areas_keyboard(self.lights, AREAS, prefix="temp"))
        self.assertTrue(all(d.startswith("temp:") for d in data))

    def test_areas_keyboard_tokens_resolve_to_room_names(self):
        data = payloads(views.areas_keyboard(self.lights, AREAS))
        self.assertEqual(views.untok(data[0].split(":")[1]), "Cucina")

    def test_sensor_line_for_a_sensor(self):
        sensor = next(s for s in self.states if s["entity_id"] == "sensor.temp_salone")
        line = views.sensor_line(sensor, AREAS, "it")
        self.assertIn("🌡", line)
        self.assertIn("21.5°C", line)

    def test_sensor_line_picks_the_humidity_icon(self):
        sensor = next(s for s in self.states if s["entity_id"] == "sensor.umid_salone")
        self.assertIn("💧", views.sensor_line(sensor, AREAS, "it"))

    def test_sensor_line_short_form_drops_the_room(self):
        sensor = next(s for s in self.states if s["entity_id"] == "sensor.temp_cucina")
        self.assertNotIn("(Cucina)", views.sensor_line(sensor, AREAS, "it", short=True))

    def test_sensor_line_for_a_thermostat(self):
        climate = next(s for s in self.states if s["entity_id"] == "climate.termo_salone")
        line = views.sensor_line(climate, AREAS, "it")
        self.assertIn("🎛", line)
        self.assertIn("20.4°C", line)
        self.assertIn("target 21°C", line)
        self.assertIn("heat", line)

    def test_thermostat_without_a_current_reading(self):
        climate = {"entity_id": "climate.x", "state": "off", "attributes": {"friendly_name": "T"}}
        line = views.sensor_line(climate, AREAS, "it")
        self.assertIn("—", line)
        self.assertNotIn("target", line)


class AudioFormatTest(unittest.TestCase):
    def test_wav_is_declared_as_pcm(self):
        self.assertEqual(voice.audio_format("audio/wav"), ("wav", "pcm"))
        self.assertEqual(voice.audio_format("audio/x-wav"), ("wav", "pcm"))

    def test_everything_else_is_ogg_opus(self):
        # what Telegram voice notes are, and what HA's providers accept natively
        self.assertEqual(voice.audio_format("audio/ogg"), ("ogg", "opus"))
        self.assertEqual(voice.audio_format(None), ("ogg", "opus"))
        self.assertEqual(voice.audio_format("video/mp4"), ("ogg", "opus"))


if __name__ == "__main__":
    unittest.main()
