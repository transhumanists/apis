"""Smoke tests for transhumanists/apis — no live network calls."""
import json, pathlib, sys, os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import importlib
rss_fetcher = importlib.import_module("scrapers.rss_fetcher")
llm_scorer = importlib.import_module("llm.score_milestone")
source_checker = importlib.import_module("self_healer.source_checker")
facebook_poster = importlib.import_module("social.facebook_poster")


class TestRssFetcher(unittest.TestCase):
    def test_feeds_is_list(self):
        self.assertIsInstance(rss_fetcher.FEEDS, list)
        self.assertGreater(len(rss_fetcher.FEEDS), 50)

    def test_feeds_have_required_keys(self):
        for f in rss_fetcher.FEEDS[:5]:
            self.assertIn("url", f)
            self.assertIn("category", f)
            self.assertIn("weight", f)
            self.assertGreater(len(f["url"]), 5)
            self.assertIn("http", f["url"])


class TestLlmScorer(unittest.TestCase):
    def test_categories_defined(self):
        self.assertEqual(len(llm_scorer.CATEGORIES), 7)

    def test_subcategories_are_snake_case(self):
        for cat, data in llm_scorer.CATEGORIES.items():
            for sub in data["subcategories"]:
                self.assertTrue(
                    sub.replace("_", "").isalnum(),
                    f"{sub} is not snake_case",
                )

    def test_geocode_known(self):
        g = llm_scorer.get_geocode("Stanford")
        self.assertEqual(g["lat"], 37.4321)

    def test_geocode_unknown(self):
        g = llm_scorer.get_geocode("Nonsense")
        self.assertEqual(g["lat"], 0.0)

    def test_rank_milestone_record(self):
        a = {"is_record": True, "value": 100, "unit": "km"}
        b = {"is_record": False, "value": 100, "unit": "km"}
        self.assertGreater(llm_scorer.rank_milestone(a), llm_scorer.rank_milestone(b))

    def test_rank_milestone_recency(self):
        today = "2026-08-29"
        old = "2020-01-01"
        a = {"is_record": False, "value": None, "date": today}
        b = {"is_record": False, "value": None, "date": old}
        self.assertGreater(llm_scorer.rank_milestone(a), llm_scorer.rank_milestone(b))


class TestSourceChecker(unittest.TestCase):
    def test_replacements_defined_for_all_categories(self):
        for cat in llm_scorer.CATEGORIES:
            self.assertIn(cat, source_checker.REPLACEMENTS)
            self.assertGreater(len(source_checker.REPLACEMENTS[cat]), 0)


class TestFacebookPoster(unittest.TestCase):
    def test_build_message(self):
        ms = {
            "categories": {
                "Biotechnology": {
                    "milestones": [{"value": 94.2, "unit": "%", "source": "Broad", "date": "2026-08-25"}],
                },
                "Energy": {
                    "milestones": [{"value": 17.6, "unit": "Q", "source": "NIF", "date": "2026-08-20"}],
                },
            }
        }
        msg = facebook_poster.build_message(ms)
        self.assertIn("transhumanists", msg)
        self.assertIn("Biotechnology", msg)
        self.assertIn("94.2", msg)
        self.assertIn("Energy", msg)

    def test_build_message_no_milestones(self):
        msg = facebook_poster.build_message({"categories": {}})
        self.assertIn("transhumanists", msg)


class TestLlmJsonSchemas(unittest.TestCase):
    def test_existing_milestones_json_valid(self):
        """The seed data file should parse as valid JSON."""
        path = pathlib.Path(__file__).resolve().parent.parent / "data" / "milestones.json"
        if path.exists():
            data = json.loads(path.read_text())
            self.assertIn("categories", data)
            for cat_name, cat in data["categories"].items():
                self.assertIn("icon", cat)
                self.assertIn("color", cat)
                self.assertIsInstance(cat["milestones"], list)


if __name__ == "__main__":
    unittest.main()
