"""Tests for the domain layer: pure functions over plain dictionaries."""

from __future__ import annotations

import unittest

from tests.fakes import AREAS, house

import entities as ent


class TestNormalize(unittest.TestCase):
    def test_strips_accents(self):
        self.assertEqual(ent.normalize("Perché"), "perche")
        self.assertEqual(ent.normalize("Umidità"), "umidita")

    def test_flattens_the_three_shapes_of_a_name(self):
        # object id, entity id and elided article must fold to the same words
        self.assertEqual(ent.normalize("camera_da_letto"), "camera da letto")
        self.assertEqual(ent.normalize("light.studio"), "light studio")
        self.assertEqual(ent.normalize("l'accendi"), "l accendi")

    def test_collapses_whitespace(self):
        self.assertEqual(ent.normalize("  Luce   salone \n"), "luce salone")

    def test_tolerates_none_and_empty(self):
        self.assertEqual(ent.normalize(None), "")
        self.assertEqual(ent.normalize(""), "")

    def test_keeps_other_punctuation(self):
        # documented: callers strip ? ! and commas themselves
        self.assertEqual(ent.normalize("quanti gradi?"), "quanti gradi?")


class TestNames(unittest.TestCase):
    def setUp(self):
        self.states = {s["entity_id"]: s for s in house()}

    def test_friendly_name(self):
        self.assertEqual(ent.friendly_name(self.states["light.cucina"]), "Luce cucina")

    def test_friendly_name_falls_back_to_entity_id(self):
        self.assertEqual(ent.friendly_name(self.states["light.corridoio"]), "light.corridoio")

    def test_label_appends_the_area_when_it_adds_information(self):
        self.assertEqual(ent.label(self.states["light.salone_faretti"], AREAS), "Faretti (Salone)")

    def test_label_does_not_repeat_the_area(self):
        # "Luce salone" already contains "salone"
        self.assertEqual(ent.label(self.states["light.salone_principale"], AREAS), "Luce salone")
        # and a light whose whole name is its area must not become "Studio (Studio)"
        self.assertEqual(ent.label(self.states["light.studio"], AREAS), "Studio")

    def test_label_without_an_area(self):
        self.assertEqual(ent.label(self.states["light.corridoio"], AREAS), "light.corridoio")


class TestStates(unittest.TestCase):
    def test_is_on_is_negative(self):
        for raw in ("on", "heat", "playing", "42"):
            self.assertTrue(ent.is_on({"state": raw}), raw)
        for raw in ("off", "unavailable", "unknown", "none", ""):
            self.assertFalse(ent.is_on({"state": raw}), raw)

    def test_is_on_folds_unreachable_into_off(self):
        # documented trade-off: right for counters, wrong for choosing targets
        self.assertFalse(ent.is_on({"state": "unavailable"}))

    def test_state_icon_keeps_unreachable_visible(self):
        self.assertEqual(ent.state_icon({"state": "unavailable"}), "⚠️")
        self.assertEqual(ent.state_icon({"state": "unknown"}), "⚠️")
        self.assertEqual(ent.state_icon({"state": "on"}), "🟡")
        self.assertEqual(ent.state_icon({"state": "off"}), "⚫")

    def test_state_text_is_localised(self):
        self.assertEqual(ent.state_text({"state": "on"}, "it"), "accesa")
        self.assertEqual(ent.state_text({"state": "on"}, "en"), "on")
        self.assertEqual(ent.state_text({"state": "unavailable"}, "en"), "unavailable")

    def test_state_text_passes_other_values_through(self):
        self.assertEqual(ent.state_text({"state": "21.5"}, "it"), "21.5")
        self.assertEqual(ent.state_text({"state": "heat"}, "en"), "heat")


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.states = house()

    def ids(self, query, **kwargs):
        return [s["entity_id"] for s in ent.search(query, self.states, AREAS, **kwargs)]

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.ids(""), [])
        self.assertEqual(self.ids("   "), [])

    def test_exact_name_wins(self):
        self.assertEqual(self.ids("luce cucina")[0], "light.cucina")

    def test_exact_entity_id_matches(self):
        self.assertEqual(self.ids("light.salone_faretti")[0], "light.salone_faretti")

    def test_area_name_matches_every_light_in_it(self):
        found = self.ids("salone", domains=("light",))
        self.assertEqual(set(found), {"light.salone_principale", "light.salone_faretti"})

    def test_area_haystack_finds_a_light_not_named_after_its_room(self):
        # "Faretti" contains neither "luce" nor "salone"; the "<area> <name>"
        # haystack is what makes this work
        self.assertIn("light.salone_faretti", self.ids("salone faretti"))

    def test_prefix_match(self):
        self.assertIn("light.cucina", self.ids("luce cuc"))

    def test_fuzzy_absorbs_a_typo(self):
        self.assertIn("light.cucina", self.ids("luce cucnia"))

    def test_unrelated_query_returns_nothing(self):
        self.assertEqual(self.ids("xyzzy quux"), [])

    def test_domains_is_a_hard_filter(self):
        self.assertEqual(self.ids("presa tv", domains=("light",)), [])
        self.assertIn("switch.presa_tv", self.ids("presa tv", domains=("light", "switch")))

    def test_limit_is_honoured(self):
        self.assertEqual(len(self.ids("luce", limit=2)), 2)

    def test_equal_scores_break_ties_alphabetically(self):
        # both are exact area matches (0.95), so friendly_name decides
        self.assertEqual(self.ids("salone", domains=("light",)),
                         ["light.salone_faretti", "light.salone_principale"])

    def test_result_is_stable_across_calls(self):
        self.assertEqual(self.ids("luce"), self.ids("luce"))


class TestGroupByArea(unittest.TestCase):
    def setUp(self):
        self.lights = [s for s in house() if s["entity_id"].startswith("light.")]

    def test_groups_and_sorts_areas_alphabetically(self):
        grouped = ent.group_by_area(self.lights, AREAS)
        self.assertEqual([k for k in grouped if k != ent.NO_AREA],
                         ["Cucina", "Salone", "Studio", "Terrazza"])

    def test_no_area_is_forced_last(self):
        grouped = ent.group_by_area(self.lights, AREAS)
        self.assertEqual(list(grouped)[-1], ent.NO_AREA)
        self.assertEqual([s["entity_id"] for s in grouped[ent.NO_AREA]], ["light.corridoio"])

    def test_entities_inside_a_group_are_sorted_by_name(self):
        grouped = ent.group_by_area(self.lights, AREAS)
        self.assertEqual([ent.friendly_name(s) for s in grouped["Salone"]], ["Faretti", "Luce salone"])

    def test_area_order_is_case_insensitive(self):
        areas = {"light.a": "zeta", "light.b": "Alfa"}
        grouped = ent.group_by_area([{"entity_id": "light.a"}, {"entity_id": "light.b"}], areas)
        self.assertEqual(list(grouped), ["Alfa", "zeta"])

    def test_empty_input(self):
        self.assertEqual(ent.group_by_area([], AREAS), {})


if __name__ == "__main__":
    unittest.main()
