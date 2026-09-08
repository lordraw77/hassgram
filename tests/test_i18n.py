"""Tests for the message catalogue, the language detector and the two grammars.

Besides the behavioural cases, this module contains three *structural* tests
that would otherwise only fail in production: every catalogue entry exists in
both languages, every entry can actually be formatted, and every key the code
asks for exists in the catalogue. ``i18n.t`` raises ``KeyError`` on a missing
key on purpose, and without these tests the first person to find out would be a
user staring at a silent handler.
"""

from __future__ import annotations

import ast
import doctest
import pathlib
import re
import string
import unittest

import tests.fakes  # noqa: F401  -- puts the project root on sys.path

import entities as ent
import i18n

ROOT = pathlib.Path(__file__).resolve().parent.parent


class DoctestTest(unittest.TestCase):
    """Runs the doctests in :mod:`i18n` and :mod:`entities` as part of the suite.

    ``i18n.parse`` documents both grammars by example and ``entities.normalize``
    documents its folding rules the same way; those examples are the cheapest
    regression guard the project has, and they should not depend on somebody
    remembering to type ``python3 -m doctest``.

    Driven through :func:`doctest.testmod` rather than the ``load_tests``
    protocol, because pytest does not implement ``load_tests`` and would
    silently skip them.
    """

    def test_doctests_pass(self):
        for module in (i18n, ent):
            with self.subTest(module=module.__name__):
                result = doctest.testmod(module, verbose=False, report=False)
                self.assertEqual(result.failed, 0, f"{module.__name__}: {result.failed} failed")
                self.assertGreater(result.attempted, 0, f"{module.__name__}: no doctests ran")


class TestCatalogueStructure(unittest.TestCase):
    def test_every_entry_covers_every_language(self):
        for key, entry in i18n.MESSAGES.items():
            for lang in i18n.LANGS:
                self.assertIn(lang, entry, f"{key} is missing {lang}")

    def test_every_entry_is_formattable_and_agrees_on_placeholders(self):
        for key, entry in i18n.MESSAGES.items():
            fields = {
                lang: {f for _, f, _, _ in string.Formatter().parse(text) if f}
                for lang, text in entry.items()
            }
            reference = fields[i18n.DEFAULT_LANG]
            for lang, names in fields.items():
                self.assertEqual(
                    names, reference,
                    f"{key}: {lang} uses {names}, {i18n.DEFAULT_LANG} uses {reference}",
                )
            # and the template must survive being rendered
            for lang in i18n.LANGS:
                i18n.t(lang, key, **{name: "x" for name in reference})

    def test_every_key_the_code_asks_for_exists(self):
        """Scan the source for ``t(lang, "key")`` and ``plural("base", n)``."""
        wanted: set[str] = set()
        for name in ("bot.py", "entities.py", "i18n.py"):
            tree = ast.parse((ROOT / name).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if fname == "t" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    wanted.add(node.args[1].value)
                elif fname == "plural" and node.args and isinstance(node.args[0], ast.Constant):
                    wanted.add(f"{node.args[0].value}_one")
                    wanted.add(f"{node.args[0].value}_many")
        self.assertTrue(wanted, "the scan found no keys at all -- it has stopped working")
        missing = sorted(k for k in wanted if k not in i18n.MESSAGES)
        self.assertEqual(missing, [], f"keys used in code but absent from MESSAGES: {missing}")

    def test_plural_pairs_are_complete(self):
        """A ``_many`` variant without its ``_one`` is a KeyError waiting to happen.

        Only this direction is checked: ``which_one`` is a whole key, not the
        singular half of a pair, and :func:`i18n.plural` is never asked for it.
        The pairs that *are* used come from the source scan above, which adds
        both halves of every ``plural()`` base.
        """
        for key in i18n.MESSAGES:
            if key.endswith("_many"):
                self.assertIn(key[:-5] + "_one", i18n.MESSAGES, key)

    def test_markers_do_not_overlap(self):
        """The two counts in :func:`i18n.detect` must be independent evidence.

        A word present in both lists bumps both counters, producing a tie that
        :func:`i18n.detect` resolves by falling back -- so it contributes
        nothing while looking as though it does.
        """
        words = {
            lang: set(re.findall(r"[a-z]+", re.sub(r"\\[a-zA-Z]", " ", pattern)))
            for lang, pattern in i18n.MARKERS.items()
        }
        self.assertEqual(words["it"] & words["en"], set())

    def test_markers_are_not_empty(self):
        for lang, pattern in i18n.MARKERS.items():
            self.assertGreater(len(re.findall(r"\|", pattern)), 10, lang)


class TestT(unittest.TestCase):
    def test_renders_the_requested_language(self):
        self.assertEqual(i18n.t("it", "no_sensors"), "Nessun sensore.")
        self.assertEqual(i18n.t("en", "no_sensors"), "No sensors.")

    def test_unknown_language_falls_back_instead_of_raising(self):
        self.assertEqual(i18n.t("de", "no_sensors"), i18n.t(i18n.DEFAULT_LANG, "no_sensors"))

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            i18n.t("it", "no_such_key")

    def test_substitutes_arguments(self):
        self.assertIn("Salone", i18n.t("it", "temp_title", title="Salone"))

    def test_plural_picks_the_variant(self):
        self.assertEqual(i18n.plural("room_counts", 1), "room_counts_one")
        self.assertEqual(i18n.plural("room_counts", 0), "room_counts_many")
        self.assertEqual(i18n.plural("room_counts", 7), "room_counts_many")


class TestDetect(unittest.TestCase):
    def detect(self, text, fallback=i18n.DEFAULT_LANG):
        return i18n.detect(ent.normalize(text), fallback=fallback)

    def test_italian_sentences(self):
        for text in ("accendi la luce dello studio", "quanti gradi in salone",
                     "spegni tutte le luci", "che temperatura c'e in camera"):
            self.assertEqual(self.detect(text), "it", text)

    def test_english_sentences(self):
        for text in ("turn on the light in the study", "how warm is it in the bedroom",
                     "which lights are on", "turn everything off"):
            self.assertEqual(self.detect(text), "en", text)

    def test_ambiguous_input_keeps_the_conversation_where_it_was(self):
        # a bare room name is evidence of nothing
        self.assertEqual(self.detect("salone", fallback="en"), "en")
        self.assertEqual(self.detect("salone", fallback="it"), "it")

    def test_empty_input_falls_back(self):
        self.assertEqual(self.detect("", fallback="en"), "en")

    def test_a_tie_falls_back(self):
        # one marker each: "luci" (it) and "the" (en)
        self.assertEqual(self.detect("the luci", fallback="it"), "it")
        self.assertEqual(self.detect("the luci", fallback="en"), "en")


class TestNormalizeLang(unittest.TestCase):
    def test_accepts_the_shapes_a_user_or_env_can_produce(self):
        for value in ("it", "IT", "it-IT", "it_IT", "italiano", "ital"):
            self.assertEqual(i18n.normalize_lang(value), "it", value)
        for value in ("en", "EN", "en-GB", "en_US", "english", "inglese"):
            self.assertEqual(i18n.normalize_lang(value), "en", value)

    def test_unknown_and_empty_fall_back(self):
        self.assertEqual(i18n.normalize_lang("klingon"), i18n.DEFAULT_LANG)
        self.assertEqual(i18n.normalize_lang(None), i18n.DEFAULT_LANG)
        self.assertEqual(i18n.normalize_lang(""), i18n.DEFAULT_LANG)
        self.assertEqual(i18n.normalize_lang("klingon", fallback=""), "")


class TestIsHome(unittest.TestCase):
    def test_accepts_both_vocabularies(self):
        for word in ("casa", "tutta la casa", "ovunque", "appartamento", "tutto"):
            self.assertTrue(i18n.is_home(word), word)
        for word in ("house", "the whole house", "everything", "everywhere", "all"):
            self.assertTrue(i18n.is_home(word), word)

    def test_matching_is_exact_not_substring(self):
        # the whole-house operation is the most destructive one the bot has
        self.assertFalse(i18n.is_home("casetta"))
        self.assertFalse(i18n.is_home("homework"))
        self.assertFalse(i18n.is_home("luce di casa"))


class TestStripFiller(unittest.TestCase):
    def test_reduces_a_sentence_to_its_target(self):
        self.assertEqual(i18n.strip_filler("accendi la luce dello studio", "it"), "studio")
        self.assertEqual(i18n.strip_filler("turn on the light in the study", "en"), "study")

    def test_a_quantifier_only_sentence_becomes_the_home_token(self):
        self.assertEqual(i18n.strip_filler("accendi tutto", "it"), i18n.HOME_TOKEN)
        self.assertEqual(i18n.strip_filler("turn everything on", "en"), i18n.HOME_TOKEN)

    def test_nothing_left_and_no_home_hint_gives_an_empty_target(self):
        self.assertEqual(i18n.strip_filler("accendi", "it"), "")

    def test_unknown_language_falls_back_to_the_default_grammar(self):
        self.assertEqual(i18n.strip_filler("accendi la luce dello studio", "de"), "studio")


class TestParse(unittest.TestCase):
    CASES = [
        # Italian
        ("accendi la luce dello studio", "it", ("on", "studio")),
        ("spegni le luci del salone", "it", ("off", "salone")),
        ("accendi tutto", "it", ("on", "casa")),
        ("spegni tutte le luci", "it", ("off", "casa")),
        ("quanti gradi in salone", "it", ("temperature", "salone")),
        ("che temperatura c e in camera da letto", "it", ("temperature", "camera da letto")),
        ("quali luci sono accese", "it", ("lights_on", "")),
        ("fammi vedere le luci", "it", ("lights", "fammi vedere")),
        ("buongiorno", "it", (None, "")),
        # English
        ("turn on the light in the study", "en", ("on", "study")),
        ("turn off the lights in the living room", "en", ("off", "living")),
        ("turn everything off", "en", ("off", "casa")),
        ("how warm is it in the bedroom", "en", ("temperature", "bedroom")),
        ("which lights are on", "en", ("lights_on", "")),
        ("hello there", "en", (None, "")),
    ]

    def test_table(self):
        for text, lang, expected in self.CASES:
            with self.subTest(text=text, lang=lang):
                self.assertEqual(i18n.parse(ent.normalize(text), lang), expected)

    def test_english_checks_the_listing_before_the_verb(self):
        """``on`` is both the imperative particle and the state in English.

        "which lights are on" must not read as a command to turn them on, which
        is why the ``lights_on`` rule precedes ``on`` in the English table and
        is guarded by an interrogative.
        """
        self.assertEqual(i18n.parse("which lights are on", "en")[0], "lights_on")
        self.assertEqual(i18n.parse("turn the lamp on", "en")[0], "on")

    def test_italian_checks_the_verb_first(self):
        self.assertEqual(i18n.parse("accendi le luci", "it")[0], "on")
        self.assertEqual(i18n.parse("quali luci sono accese", "it")[0], "lights_on")

    def test_unknown_language_falls_back_to_the_default_grammar(self):
        self.assertEqual(i18n.parse("accendi lo studio", "de"), ("on", "studio"))

    def test_italian_run_verbs_are_not_read_as_a_switch(self):
        for sentence in ("esegui la scena cinema", "lancia lo script buonanotte",
                         "avvia l'automazione risveglio"):
            with self.subTest(sentence=sentence):
                self.assertEqual(i18n.parse(sentence, "it")[0], "run")

    def test_english_run_verbs_are_not_read_as_a_switch(self):
        for sentence in ("run the cinema scene", "trigger the wake up automation",
                         "execute the good night script"):
            with self.subTest(sentence=sentence):
                self.assertEqual(i18n.parse(sentence, "en")[0], "run")

    def test_run_leaves_the_name_as_the_target(self):
        self.assertEqual(i18n.parse("esegui cinema", "it"), ("run", "cinema"))
        self.assertEqual(i18n.parse("run cinema", "en"), ("run", "cinema"))

    def test_attiva_still_switches_rather_than_runs(self):
        """It is said of a light far more often than of a scene; /esegui covers the rest."""
        self.assertEqual(i18n.parse("attiva la luce dello studio", "it"), ("on", "studio"))

    def test_the_run_rule_does_not_swallow_the_switching_sentences(self):
        """The run rule is checked first, so the old intents must still win their own."""
        cases = (
            ("accendi le luci", "it", "on"),
            ("spegni tutto", "it", "off"),
            ("quali luci sono accese", "it", "lights_on"),
            ("turn on the lamp", "en", "on"),
            ("turn everything off", "en", "off"),
            ("which lights are on", "en", "lights_on"),
            ("quanti gradi in salone", "it", "temperature"),
        )
        for sentence, lang, expected in cases:
            with self.subTest(sentence=sentence):
                self.assertEqual(i18n.parse(sentence, lang)[0], expected)


if __name__ == "__main__":
    unittest.main()
