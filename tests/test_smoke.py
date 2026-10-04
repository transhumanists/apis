"""Smoke tests for transhumanists/apis — no live network calls.

Coverage:
  - Configuration / schema validation
  - Pure functions: normalize_value, rank_milestone, get_geocode
  - HTML / RSS / malformed input handling in fetch_feed
  - score_article end-to-end with mocked LLM
  - Auto-discovery of new categories / subcategories
  - build_message for Facebook (all/empty categories)
  - should_post once-per-day guard (UTC consistency)
  - JSON schema validity of generated milestones.json
  - Milestones.md generation contains all categories
"""
import importlib
import json
import os
import pathlib
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Ensure no real LLM calls during import
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

rss_fetcher = importlib.import_module("scrapers.rss_fetcher")
llm_scorer = importlib.import_module("llm.score_milestone")
source_checker = importlib.import_module("self_healer.source_checker")
facebook_poster = importlib.import_module("social.facebook_poster")
dashboard_updater = importlib.import_module("github.dashboard_updater")


class TestRssFetcher(unittest.TestCase):

    def setUp(self):
        self.patches = []
        # Patch rate_limit calls so rss_fetcher tests don't touch the real budget
        self.patches.append(patch.object(rss_fetcher, "check_and_consume", lambda p: MagicMock(allowed=True, retry_after=0)))  # noqa: E501
        self.patches.append(patch.object(rss_fetcher, "record_response", lambda p, s, e="": None))
        self.patches.append(patch.object(rss_fetcher, "cache_get", lambda k, **kw: None))
        self.patches.append(patch.object(rss_fetcher, "cache_set", lambda k, v=None, **kw: None))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_feeds_is_list(self):
        self.assertIsInstance(rss_fetcher.FEEDS, list)
        self.assertGreater(len(rss_fetcher.FEEDS), 50)

    def test_feeds_have_required_keys(self):
        for f in rss_fetcher.FEEDS:
            self.assertIn("url", f)
            self.assertIn("category", f)
            self.assertIn("weight", f)
            self.assertTrue(f["url"].startswith("http"), f"Bad URL: {f['url']}")

    def test_all_feeds_have_known_category(self):
        # The 7 defaults plus dynamically-added ones
        for f in rss_fetcher.FEEDS:
            self.assertIsInstance(f["category"], str)
            self.assertGreater(len(f["category"]), 0)

    def test_unique_urls(self):
        urls = [f["url"] for f in rss_fetcher.FEEDS]
        self.assertEqual(len(urls), len(set(urls)), "Duplicate feed URLs found")

    def test_fetch_feed_handles_timeout(self):
        with patch.object(rss_fetcher.requests, "get", side_effect=rss_fetcher.requests.Timeout()):
            articles, errors = rss_fetcher.fetch_feed({"url": "https://x.com", "category": "X", "weight": 1})
        self.assertEqual(articles, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("url", errors[0])

    def test_fetch_feed_handles_http_404(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = rss_fetcher.requests.HTTPError(
            response=MagicMock(status_code=404)
        )
        with patch.object(rss_fetcher.requests, "get", return_value=mock_resp):
            articles, errors = rss_fetcher.fetch_feed({"url": "https://x.com", "category": "X", "weight": 1})
        self.assertEqual(articles, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["status"], "dead")

    def test_fetch_feed_handles_html_response(self):
        ok_resp = MagicMock()
        ok_resp.headers = {"Content-Type": "application/rss+xml"}
        ok_resp.content = b"<rss><channel><title>T</title><item><title>A</title><link>https://x.com/a</link></item></channel></rss>"
        ok_resp.raise_for_status = lambda: None

        html_resp = MagicMock()
        html_resp.headers = {"Content-Type": "text/html"}
        html_resp.raise_for_status = lambda: None

        with patch.object(rss_fetcher.requests, "get", side_effect=[html_resp, html_resp, ok_resp]), \
             patch("scrapers.rss_fetcher.time.sleep", lambda *_: None), \
             patch("scrapers.rss_fetcher.feedparser.parse") as mock_parse:
            mock_parsed = MagicMock()
            mock_parsed.bozo = False
            mock_parsed.feed = {"title": "T"}
            mock_parsed.entries = [{"title": "A", "link": "https://x.com/a", "summary": "s"}]
            mock_parse.return_value = mock_parsed
            articles, _ = rss_fetcher.fetch_feed({"url": "https://x.com", "category": "X", "weight": 1})
        self.assertIsInstance(articles, list)

    def test_fetch_feed_size_cap(self):
        """Response larger than MAX_ARTICLE_SIZE is truncated."""
        huge = b"x" * (rss_fetcher.MAX_ARTICLE_SIZE + 10000)
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/rss+xml"}
        resp.content = huge
        resp.raise_for_status = lambda: None

        with patch.object(rss_fetcher.requests, "get", return_value=resp):
            with patch.object(rss_fetcher.time, "sleep", lambda *_: None):
                articles, _ = rss_fetcher.fetch_feed({"url": "https://x.com", "category": "X", "weight": 1})
        # Truncated response will fail to parse as RSS — that's fine, no crash
        self.assertIsInstance(articles, list)

    def test_arxiv_backfill_default_is_disabled(self):
        """Deep backfill is a no-op unless explicitly enabled (zero routine cost)."""
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", ""), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0):
            articles, errors = rss_fetcher.fetch_arxiv_history()
        self.assertEqual(articles, [])
        self.assertEqual(errors, [])

    def test_arxiv_backfill_iterates_month_windows(self):
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2026-08-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now):
            windows = rss_fetcher._iter_backfill_windows()
        # 2026-08-01..2026-09-23 → August + September (partial) windows, both
        # date-filtered in arXiv's submittedDate format.
        self.assertEqual(windows[0], ("20260801000000", "20260901000000"))
        self.assertEqual(windows[1], ("20260901000000", "20260924000000"))

    def test_fetch_arxiv_history_builds_dated_articles(self):
        """A mocked export-API page yields ground-truth dated arxiv articles."""
        fake_entry = MagicMock()
        fake_entry.id = "http://arxiv.org/abs/2609.12345v1"
        fake_entry.title = "  Decoder-heavy   transformers \n 2026  "
        fake_entry.summary = "<p>Abstract text &amp; more</p>"
        fake_entry.published_parsed = time.strptime("2026-09-20", "%Y-%m-%d")

        parsed = MagicMock()
        parsed.entries = [fake_entry]

        resp = MagicMock()
        resp.content = b"<feed/>"
        resp.raise_for_status = lambda: None

        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2026-09-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now), \
             patch.object(rss_fetcher.time, "sleep", lambda *_: None), \
             patch.object(rss_fetcher.requests, "get", return_value=resp), \
             patch.object(rss_fetcher.feedparser, "parse", return_value=parsed):
            articles, errors = rss_fetcher.fetch_arxiv_history()
        self.assertEqual(errors, [])
        self.assertGreater(len(articles), 0)
        a = articles[0]
        self.assertEqual(a["title"], "Decoder-heavy transformers 2026")
        self.assertEqual(a["published"], "2026-09-20")
        self.assertEqual(a["source"], "arXiv")
        self.assertTrue(a["arxiv_history"])

    def test_arxiv_backfill_malformed_start_is_ignored_not_crash(self):
        """A malformed ARXIV_HISTORY_START must disable backfill, never crash."""
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2026/06/01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now):
            windows = rss_fetcher._iter_backfill_windows()
        self.assertEqual(windows, [])

    def test_arxiv_backfill_future_start_is_disabled_not_crash(self):
        """A start date after "today" must disable backfill without crashing."""
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2030-01-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now):
            windows = rss_fetcher._iter_backfill_windows()
        self.assertEqual(windows, [])

    def test_arxiv_backfill_window_count_is_capped(self):
        """A far-past start must not generate unbounded month windows (job timeout)."""
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2010-01-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now):
            windows = rss_fetcher._iter_backfill_windows()
        self.assertEqual(len(windows), rss_fetcher.ARXIV_HISTORY_MAX_WINDOWS)
        self.assertEqual(windows[0][0], "20100101000000")

    def test_arxiv_backfill_retries_on_429_then_succeeds(self):
        """A 429 (arXiv rate limit) retries with backoff instead of dropping the query."""
        entry = MagicMock()
        entry.id = "http://arxiv.org/abs/2609.10001v1"
        entry.title = "X"
        entry.summary = "s"
        entry.published_parsed = time.strptime("2026-09-20", "%Y-%m-%d")
        parsed = MagicMock()
        parsed.entries = [entry]

        rate = MagicMock()
        rate.status_code = 429
        rate.headers = {"Retry-After": "3"}
        rate.raise_for_status = lambda: (_ for _ in ()).throw(rss_fetcher.requests.HTTPError())
        ok = MagicMock()
        ok.status_code = 200
        ok.content = b"<feed/>"
        ok.raise_for_status = lambda: None

        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            calls["n"] += 1
            return rate if calls["n"] == 1 else ok

        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2026-09-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now), \
             patch.object(rss_fetcher.time, "sleep", lambda *_: None), \
             patch.object(rss_fetcher.requests, "get", side_effect=fake_get), \
             patch.object(rss_fetcher.feedparser, "parse", return_value=parsed):
            articles, errors = rss_fetcher.fetch_arxiv_history()
        self.assertEqual(errors, [])
        # one rate-limited attempt (no article) + one success per query category
        self.assertEqual(len(articles), len(rss_fetcher.ARXIV_HISTORY_QUERIES))
        self.assertEqual(calls["n"], len(rss_fetcher.ARXIV_HISTORY_QUERIES) + 1)

    def test_arxiv_backfill_retries_on_timeout_then_records_error(self):
        """Timeouts across all attempts record an error — backfill must not crash."""
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        total_attempts = (rss_fetcher.FETCH_RETRIES + 1) * len(rss_fetcher.ARXIV_HISTORY_QUERIES)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2026-09-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now), \
             patch.object(rss_fetcher.time, "sleep", lambda *_: None), \
             patch.object(rss_fetcher.requests, "get",
                          side_effect=[rss_fetcher.requests.Timeout()] * total_attempts):
            articles, errors = rss_fetcher.fetch_arxiv_history()
        self.assertEqual(articles, [])
        self.assertEqual(len(errors), len(rss_fetcher.ARXIV_HISTORY_QUERIES))

    def test_arxiv_backfill_persistent_429_records_error(self):
        """429s across every attempt surface as a counted error, not a silent empty result."""
        rate = MagicMock()
        rate.status_code = 429
        rate.headers = {"Retry-After": "3"}
        rate.raise_for_status = lambda: (_ for _ in ()).throw(rss_fetcher.requests.HTTPError())
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        total_attempts = (rss_fetcher.FETCH_RETRIES + 1) * len(rss_fetcher.ARXIV_HISTORY_QUERIES)
        with patch.object(rss_fetcher, "ARXIV_HISTORY_START", "2026-09-01"), \
             patch.object(rss_fetcher, "ARXIV_HISTORY_DAYS", 0), \
             patch.object(rss_fetcher, "_utc_now", return_value=now), \
             patch.object(rss_fetcher.time, "sleep", lambda *_: None), \
             patch.object(rss_fetcher.requests, "get", side_effect=[rate] * total_attempts):
            articles, errors = rss_fetcher.fetch_arxiv_history()
        self.assertEqual(articles, [])
        self.assertEqual(len(errors), len(rss_fetcher.ARXIV_HISTORY_QUERIES))


class TestLlmScorer(unittest.TestCase):
    def test_categories_defined(self):
        self.assertEqual(len(llm_scorer.DEFAULT_SUBCATEGORIES), 7)

    def test_subcategories_are_snake_case(self):
        for cat, subcats in llm_scorer.DEFAULT_SUBCATEGORIES.items():
            for sub in subcats:
                self.assertTrue(
                    sub.replace("_", "").isalnum(),
                    f"{sub} in {cat} is not snake_case",
                )

    def test_geocode_known(self):
        g = llm_scorer.get_geocode("Stanford University")
        self.assertEqual(g["lat"], 37.4321)

    def test_geocode_unknown(self):
        g = llm_scorer.get_geocode("Nonsense Source")
        self.assertEqual(g["lat"], 0.0)

    def test_geocode_uses_location_when_source_unmatched(self):
        g = llm_scorer.get_geocode("Unlisted University of the Far East", location="Nanjing, China")
        self.assertEqual(g["lat"], 32.0603)

    def test_geocode_known_source_beats_location(self):
        g = llm_scorer.get_geocode("IBM", location="San Francisco, USA")
        self.assertEqual(g["lat"], 41.0323)

    def test_geocode_longest_key_wins_over_prefix(self):
        # "nif" is a prefix of "nifs" but they are different labs (Livermore NIF
        # vs Japan's NIFS). NIFS must never resolve to Livermore.
        self.assertEqual(llm_scorer.get_geocode("NIFS Japan")["lat"], 35.6762)
        self.assertEqual(llm_scorer.get_geocode("NIF Livermore")["lat"], 37.6881)
        self.assertEqual(llm_scorer.get_geocode("NIFS")["lat"], 35.6762)

    def test_normalize_value_none(self):
        self.assertEqual(llm_scorer.normalize_value(None, "km"), 0.0)

    def test_normalize_value_invalid(self):
        self.assertEqual(llm_scorer.normalize_value("not a number", "km"), 0.0)

    def test_normalize_value_percent(self):
        self.assertEqual(llm_scorer.normalize_value(94.2, "%"), 94.2)

    def test_normalize_value_mach(self):
        self.assertEqual(llm_scorer.normalize_value(13, "Mach"), 65.0)

    def test_normalize_value_km_capped(self):
        self.assertEqual(llm_scorer.normalize_value(30000, "km"), 100.0)
        self.assertEqual(llm_scorer.normalize_value(5000, "km"), 25.0)

    def test_normalize_value_rejects_nonfinite(self):
        """NaN/Infinity must never poison rank keys or is_new comparisons."""
        self.assertEqual(llm_scorer.normalize_value(float("nan"), "km"), 0.0)
        self.assertEqual(llm_scorer.normalize_value(float("inf"), "km"), 0.0)
        self.assertEqual(llm_scorer.normalize_value("-inf", ""), 0.0)
        self.assertEqual(llm_scorer.normalize_value("1e400", ""), 0.0)
        self.assertEqual(llm_scorer.normalize_value(0e400, "Wh/kg"), 0.0)

    def test_normalize_value_strips_thousands_separators(self):
        """LLM "10,000" style literals must rank normally, not deflate to 0.0."""
        self.assertEqual(llm_scorer.normalize_value("10,000", "km"), 50.0)
        self.assertEqual(llm_scorer.normalize_value("1,000,000", "qubit"), 20000.0)
        self.assertEqual(llm_scorer.normalize_value("10,000", ""), 10000.0)
        self.assertEqual(llm_scorer.normalize_value("10000", "km"), 50.0)
        # European decimals and stray commas still fail closed (no mis-parse).
        self.assertEqual(llm_scorer.normalize_value("0,5", "km"), 0.0)
        self.assertEqual(llm_scorer.normalize_value("1,2,3", ""), 0.0)

    def test_rank_milestone_record(self):
        a = {"is_record": True, "value": 100, "unit": "km"}
        b = {"is_record": False, "value": 100, "unit": "km"}
        self.assertGreater(llm_scorer.rank_milestone(a), llm_scorer.rank_milestone(b))

    def test_rank_milestone_recency(self):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        old_str = "2020-01-01"
        a = {"is_record": False, "value": None, "date": today_str}
        b = {"is_record": False, "value": None, "date": old_str}
        self.assertGreater(llm_scorer.rank_milestone(a), llm_scorer.rank_milestone(b))

    def test_rank_milestone_invalid_date(self):
        m = {"is_record": False, "value": 100, "date": "not-a-date"}
        self.assertGreater(llm_scorer.rank_milestone(m), 0)

    def test_milestone_sort_key_is_chronological_first(self):
        """Backfilled older milestones sort below newer ones regardless of value."""
        old = {"date": "2025-06-01", "value": 1000, "is_record": True}
        newer = {"date": "2026-09-22", "value": 1, "is_record": False}
        self.assertLess(llm_scorer.milestone_sort_key(old), llm_scorer.milestone_sort_key(newer))
        # Undated records sort to the bottom (1970 sentinel) — never crash.
        self.assertLess(llm_scorer.milestone_sort_key({"date": ""}), llm_scorer.milestone_sort_key(newer))
        # Equal dates tie-break on rank so ordering stays deterministic.
        a = {"date": "2026-09-22", "value": 50}
        b = {"date": "2026-09-22", "value": 40}
        self.assertGreater(llm_scorer.milestone_sort_key(a), llm_scorer.milestone_sort_key(b))

    def test_ensure_category_new(self):
        llm_scorer.ensure_category("Crystallography", "#ff00ff")
        self.assertIn("Crystallography", llm_scorer.CATEGORIES)
        self.assertEqual(llm_scorer.CATEGORIES["Crystallography"]["color"], "#ff00ff")
        # idempotent
        llm_scorer.ensure_category("Crystallography", "#000000")
        self.assertEqual(llm_scorer.CATEGORIES["Crystallography"]["color"], "#ff00ff")

    def test_ensure_subcategory_new(self):
        llm_scorer.ensure_subcategory("Biotechnology", "protein_design")
        self.assertIn("protein_design", llm_scorer.DYNAMIC_SUBCATEGORIES["Biotechnology"])
        # idempotent
        llm_scorer.ensure_subcategory("Biotechnology", "protein_design")
        self.assertEqual(llm_scorer.DYNAMIC_SUBCATEGORIES["Biotechnology"].count("protein_design"), 1)

    def test_ensure_subcategory_new_category(self):
        llm_scorer.ensure_subcategory("Robotics", "humanoid_walk")
        self.assertIn("Robotics", llm_scorer.DYNAMIC_SUBCATEGORIES)
        self.assertIn("humanoid_walk", llm_scorer.DYNAMIC_SUBCATEGORIES["Robotics"])

    def test_score_article_with_milestone(self):
        article = {"title": "New 4,158-qubit chip", "summary": "IBM breaks record with 4,158 qubits"}
        mock_result = {
            "is_milestone": True,
            "category": "Quantum Physics",
            "subcategory": "qubit_count",
            "title": "IBM 4,158-qubit Condor 2",
            "value": 4158,
            "unit": "qubits",
            "source": "IBM",
            "date": "2026-08-22",
            "is_record": True,
            "is_breakthrough": True,
            "summary": "IBM unveils Condor 2 with 4,158 qubits.",
        }
        with patch.object(llm_scorer, "call_llm", return_value=mock_result):
            m = llm_scorer.score_article(article)
        self.assertIsNotNone(m)
        self.assertEqual(m["category"], "Quantum Physics")
        self.assertEqual(m["value"], 4158)
        self.assertEqual(m["subcategory"], "qubit_count")
        self.assertEqual(m["geolocation"]["lat"], 41.0323)  # IBM

    def test_score_article_missing_summary_does_not_crash(self):
        # RSS entries can carry a summary key set to null; scoring must not
        # slice a None summary into a TypeError.
        mock_result = {
            "is_milestone": True,
            "category": "Biotechnology",
            "subcategory": "gene_therapy",
            "title": "Groundbreaking ex-vivo therapy",
            "value": None,
            "unit": None,
        }
        with patch.object(llm_scorer, "call_llm", return_value=mock_result):
            m = llm_scorer.score_article({"title": "X", "summary": None})
        self.assertIsNotNone(m)
        self.assertEqual(m["summary"], "")

    def test_score_article_unknown_category_auto_added(self):
        article = {"title": "Robotic surgery breakthrough", "summary": "First remote robotic microsurgery"}
        mock_result = {
            "is_milestone": True,
            "category": "Robotics",
            "is_new_category": True,
            "subcategory": "remote_surgery",
            "title": "First remote robotic microsurgery",
            "value": None,
            "unit": None,
            "source": "MIT",
            "date": "2026-08-15",
            "is_record": True,
            "is_breakthrough": True,
            "summary": "MIT performs first remote robotic microsurgery.",
        }
        with patch.object(llm_scorer, "call_llm", return_value=mock_result):
            m = llm_scorer.score_article(article)
        self.assertIsNotNone(m)
        self.assertIn("Robotics", llm_scorer.CATEGORIES)
        self.assertIn("remote_surgery", llm_scorer.DYNAMIC_SUBCATEGORIES["Robotics"])
        # Cleanup
        llm_scorer.CATEGORIES.pop("Robotics", None)
        llm_scorer.DYNAMIC_SUBCATEGORIES.pop("Robotics", None)

    def test_score_article_not_milestone(self):
        with patch.object(llm_scorer, "call_llm", return_value={"is_milestone": False}):
            m = llm_scorer.score_article({"title": "Op-ed", "summary": "blah"})
        self.assertIsNone(m)

    def test_score_article_unknown_subcategory_falls_back(self):
        mock_result = {
            "is_milestone": True,
            "category": "Biotechnology",
            "subcategory": "made_up_subcat_xyz",
            "title": "X",
            "value": 1, "unit": "x",
            "source": "X", "date": "2026-01-01",
            "is_record": False, "is_breakthrough": False, "summary": "x",
        }
        with patch.object(llm_scorer, "call_llm", return_value=mock_result):
            _ = llm_scorer.score_article({"title": "X", "summary": "x"})
        # Should auto-add the new subcategory
        self.assertIn("made_up_subcat_xyz", llm_scorer.DYNAMIC_SUBCATEGORIES["Biotechnology"])

    def test_call_llm_openai_missing_key(self):
        with patch.object(llm_scorer, "OPENAI_API_KEY", ""):
            with patch.object(llm_scorer, "ANTHROPIC_API_KEY", ""):
                self.assertIsNone(llm_scorer.call_llm_openai("t", "s"))
                self.assertIsNone(llm_scorer.call_llm_anthropic("t", "s"))

    def test_call_llm_openai_with_fences(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = '```json\n{"is_milestone": false}\n```'
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch.object(llm_scorer, "OpenAI", return_value=mock_client):
            result = llm_scorer.call_llm_openai("t", "s")
        self.assertEqual(result, {"is_milestone": False})

    def test_generate_milestones_md_includes_all_categories(self):
        cats = {
            "Biotechnology": {"name": "Biotechnology", "icon": "🧬", "color": "#00e676",
                               "subcategories": ["gene_editing"],
                               "milestones": [{"id": "1", "title": "CRISPR record", "value": 1, "unit": "u",
                                              "source": "S", "date": "2026-01-01",
                                              "subcategory": "gene_editing", "is_new": True}]},
            "Robotics": {"name": "Robotics", "icon": "🤖", "color": "#ff00ff",
                          "subcategories": ["remote_surgery"],
                          "milestones": [{"id": "2", "title": "First robotic surgery", "value": 1, "unit": "op",
                                         "source": "MIT", "date": "2026-01-02",
                                         "subcategory": "remote_surgery", "is_new": False}]},
        }
        md = llm_scorer.generate_milestones_md(cats)
        self.assertIn("1. Biotechnology", md)
        self.assertIn("2. Robotics", md)
        self.assertIn("remote_surgery", md)
        self.assertIn("CRISPR record", md)
        self.assertIn("First robotic surgery", md)
        self.assertIn("Auto-generated", md)

    def test_merge_with_existing_retains_records_when_no_new_scored(self):
        """A run that scores nothing must NOT collapse the published dataset."""
        existing = {
            "Biotechnology/biosensors": [
                {"id": "ms-aaa", "category": "Biotechnology", "subcategory": "biosensors",
                 "title": "Nanopore sensor", "value": 98.7, "date": "2026-09-15", "is_new": True},
            ],
            "Quantum Physics/qubit_count": [
                {"id": "ms-bbb", "category": "Quantum Physics", "subcategory": "qubit_count",
                 "title": "IBM 4,158 qubits", "value": 4158, "date": "2026-08-22"},
            ],
        }
        merged = llm_scorer.merge_with_existing(existing, {})
        self.assertEqual(len(merged), 2)
        total = sum(len(v) for v in merged.values())
        self.assertEqual(total, 2)
        # Retained records lose their "is_new" marker.
        for records in merged.values():
            for m in records:
                self.assertFalse(m.get("is_new"))

    def test_merge_with_existing_keeps_previous_on_partial_scoring(self):
        """Old collapse bug: only freshly scored subs survived (37 -> 4 -> 3)."""
        existing = {
            cat + "/" + sub: [
                {"id": f"ms-{i}", "category": cat, "subcategory": sub,
                 "title": f"{cat} {sub} milestone", "value": 100, "date": "2026-09-01"}
            ]
            for i, (cat, sub) in enumerate(
                [("Biotechnology", "biosensors"), ("Energy", "fusion"), ("Spaceflight", "launch")]
            )
        }
        # Only one new milestone arrives today, in a sub that already exists.
        scored = {
            "Biotechnology/biosensors": {
                "id": "ms-new1", "category": "Biotechnology", "subcategory": "biosensors",
                "title": "Newer nanopore", "value": 99.0, "date": "2026-09-22", "is_new": True,
            }
        }
        merged = llm_scorer.merge_with_existing(existing, scored)
        self.assertEqual(len(merged), 3)          # all three subs survive
        self.assertEqual(sum(len(v) for v in merged.values()), 4)  # 3 old + 1 new

    def test_merge_with_existing_replaces_same_id(self):
        """Same-id update supersedes in place (no duplicate ids)."""
        existing = {
            "Biotechnology/biosensors": [
                {"id": "ms-dup", "category": "Biotechnology", "subcategory": "biosensors",
                 "title": "Old title", "value": 90.0, "date": "2026-09-01"},
            ]
        }
        scored = {
            "Biotechnology/biosensors": {
                "id": "ms-dup", "category": "Biotechnology", "subcategory": "biosensors",
                "title": "New title", "value": 99.0, "date": "2026-09-22", "is_record": True,
            }
        }
        merged = llm_scorer.merge_with_existing(existing, scored)
        records = merged["Biotechnology/biosensors"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "New title")
        self.assertTrue(records[0]["is_new"])

    def test_build_existing_by_subcat_normalizes_display_names(self):
        """Canonical display names map to LLM-side keys so nothing is dropped."""
        raw = {
            "categories": {
                "renewable_energy": {
                    "name": "Renewable Energy",
                    "milestones": [
                        {"id": "ms-aa", "category": "Renewable Energy",
                         "subcategory": "fusion", "title": "NIF Q>1", "value": 1.5,
                         "date": "2026-07-12"},
                    ],
                }
            }
        }
        existing = llm_scorer.build_existing_by_subcat(raw)
        self.assertEqual(list(existing), ["Energy/fusion"])
        self.assertEqual(existing["Energy/fusion"][0]["category"], "Energy")

    def test_merge_retains_display_named_canonical_categories(self):
        """Real canonical data (long display names) must survive a dry run.

        Regression: the restored 40-record DB keys 14 records under long
        display names ("Renewable Energy", "Spaceflight & Aeronautics",
        "Military & Defense") while the pipeline keys by short LLM names
        ("Energy", "Spaceflight", "Defense"). Without normalisation the
        distribution silently dropped those buckets on a zero-article run
        (26 of 40 kept, observed 2026-09-22 during an audit).
        """
        raw = {
            "categories": {
                cat_key: {
                    "name": display,
                    "milestones": [
                        {"id": f"ms-{i}", "category": display, "subcategory": sub,
                         "title": f"{display} {sub}", "value": 100, "date": "2026-09-01"}
                    ],
                }
                for i, (cat_key, display, sub) in enumerate([
                    ("Energy", "Renewable Energy", "fusion"),
                    ("Spaceflight", "Spaceflight & Aeronautics", "launch"),
                    ("Defense", "Military & Defense", "air_defense"),
                    ("Quantum Physics", "Quantum Physics", "qubit_count"),
                ])
            }
        }
        existing = llm_scorer.build_existing_by_subcat(raw)
        merged = llm_scorer.merge_with_existing(existing, {})
        self.assertEqual(len(merged), 4)
        self.assertIn("Energy/fusion", merged)
        self.assertIn("Spaceflight/launch", merged)
        self.assertIn("Defense/air_defense", merged)
        self.assertIn("Quantum Physics/qubit_count", merged)

    def test_merge_is_new_compared_against_display_named_existing(self):
        """is_new must be judged against existing records even when the
        existing bucket is keyed under a long canonical display name.

        Regression: before normalisation the preview lookup missed display-name
        buckets, so every freshly scored record was flagged is_new=True and
        superseded unconditionally — collapsing stronger stored records.
        """
        raw = {
            "categories": {
                "Energy": {
                    "name": "Renewable Energy",
                    "milestones": [
                        {"id": "ms-ref", "category": "Renewable Energy",
                         "subcategory": "fusion", "title": "NIF record",
                         "value": 1.5, "date": "2026-09-22"},
                    ],
                }
            }
        }
        existing = llm_scorer.build_existing_by_subcat(raw)
        weak = {
            "Energy/fusion": {
                "id": "ms-weak", "category": "Energy", "subcategory": "fusion",
                "title": "Smaller result", "value": 0.5, "date": "2026-09-22",
            }
        }
        merged = llm_scorer.merge_with_existing(existing, weak)
        recs = merged["Energy/fusion"]
        self.assertEqual(len(recs), 2)
        self.assertFalse(next(r for r in recs if r["id"] == "ms-weak")["is_new"])

    def test_build_categories_output_uses_display_names(self):
        """Category containers publish the canonical long display names."""
        out = llm_scorer.build_categories_output()
        self.assertEqual(out["Energy"]["name"], "Renewable Energy")
        self.assertEqual(out["Spaceflight"]["name"], "Spaceflight & Aeronautics")
        self.assertEqual(out["Defense"]["name"], "Military & Defense")

    def test_event_value_uses_title_when_no_metric(self):
        """Metric-less milestones publish their title, never a summary string."""
        self.assertEqual(llm_scorer.event_value(
            {"value": None, "summary": "A summary.", "title": "A title"}
        ), "A title")
        self.assertEqual(
            llm_scorer.event_value({"value": None, "title": "Alkermes orexin ADHD"}),
            "Alkermes orexin ADHD",
        )
        self.assertEqual(llm_scorer.event_value({"title": "Only title"}), "Only title")
        self.assertEqual(llm_scorer.event_value({}), "")
        self.assertEqual(llm_scorer.event_value({"value": 100, "unit": "MW", "title": "NIF"}), "100 MW")

    def test_main_reads_and_writes_utf8_content(self):
        """Regression: Windows cp1252 default encoding crashed pipeline I/O.

        Pipeline JSON carries non-ASCII (emoji) content; main() must read and
        write its files as UTF-8 regardless of the platform default encoding.
        """
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp())
        articles = {"last_update": "2026-09-22T00:00:00Z", "articles": [
            {"id": "a1", "title": "Fusion reactor milestone 🔥", "summary": "Breakthrough ⚡",
             "source": "Test Feed", "published": "2026-09-22",
             "url": "https://example.com/a1", "weight": 10},
        ]}
        in_file = tmp / "articles.json"
        existing = tmp / "milestones_existing.json"
        out_ms = tmp / "milestones.json"
        out_ev = tmp / "events.json"
        out_md = tmp / "Milestones.md"
        in_file.write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
        existing.write_text(json.dumps({"categories": {}}), encoding="utf-8")
        orig = (llm_scorer.IN_FILE, llm_scorer.EXISTING, llm_scorer.OUT_MILESTONES,
                llm_scorer.OUT_EVENTS, llm_scorer.OUT_MD)
        try:
            llm_scorer.IN_FILE = in_file
            llm_scorer.EXISTING = existing
            llm_scorer.OUT_MILESTONES = out_ms
            llm_scorer.OUT_EVENTS = out_ev
            llm_scorer.OUT_MD = out_md
            with patch.object(llm_scorer, "call_llm", return_value=None), \
                    patch.object(llm_scorer, "_get_router", return_value=None):
                llm_scorer.main()
            data = json.loads(out_ms.read_text(encoding="utf-8"))
            self.assertIn("categories", data)
            self.assertEqual(json.loads(out_ev.read_text(encoding="utf-8"))["events"], [])
            self.assertIn("Human Progress Milestones", out_md.read_text(encoding="utf-8"))
        finally:
            (llm_scorer.IN_FILE, llm_scorer.EXISTING, llm_scorer.OUT_MILESTONES,
             llm_scorer.OUT_EVENTS, llm_scorer.OUT_MD) = orig


class TestSourceChecker(unittest.TestCase):
    def test_replacements_defined_for_all_categories(self):
        for cat in llm_scorer.DEFAULT_SUBCATEGORIES:
            self.assertIn(cat, source_checker.REPLACEMENTS)
            self.assertGreater(len(source_checker.REPLACEMENTS[cat]), 0)

    def test_find_replacement_uses_cache(self):
        source_checker._REPLACEMENT_CACHE.clear()
        # Pre-fill cache
        source_checker._REPLACEMENT_CACHE["TestCat"] = "https://cached.example.com"
        with patch.object(source_checker, "check_url") as mock_check:
            url = source_checker.find_replacement("TestCat", "https://dead.example.com")
        self.assertEqual(url, "https://cached.example.com")
        mock_check.assert_not_called()

    def test_check_url_timeout(self):
        with patch.object(source_checker.requests, "head", side_effect=source_checker.requests.Timeout()):
            with patch.object(source_checker.requests, "get", side_effect=source_checker.requests.Timeout()):
                result = source_checker.check_url("https://x.com")
        self.assertEqual(result["status"], "timeout")


class TestFacebookPoster(unittest.TestCase):
    def test_build_message(self):
        ms = {
            "categories": {
                "Biotechnology": {
                    "milestones": [{"value": 94.2, "unit": "%", "source": "Broad", "date": "2026-08-25"}]
                },
                "Energy": {
                    "milestones": [{"value": 17.6, "unit": "Q", "source": "NIF", "date": "2026-08-20"}]
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

    def test_build_message_includes_dynamic_categories(self):
        ms = {
            "categories": {
                "Robotics": {"milestones": [{"value": 1, "unit": "op", "source": "MIT", "date": "2026-08-15"}]},
            }
        }
        msg = facebook_poster.build_message(ms)
        self.assertIn("Robotics", msg)
        self.assertIn("MIT", msg)

    def test_should_post_when_no_history(self):
        with patch.object(pathlib.Path, "exists", return_value=False):
            self.assertTrue(facebook_poster.should_post())

    def test_should_post_skips_same_day(self):
        today = facebook_poster._utc_date()
        fake_data = {"last_post_date": today}
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="{}"), \
             patch.object(facebook_poster.json, "loads", return_value=fake_data):
            self.assertFalse(facebook_poster.should_post())

    def test_should_post_allows_next_day(self):
        with patch.object(json, "loads", return_value={"last_post_date": "2020-01-01"}):
            with patch.object(pathlib.Path, "exists", return_value=True):
                self.assertTrue(facebook_poster.should_post())

    def test_post_to_facebook_no_requests(self):
        with patch.object(facebook_poster, "requests", None):
            result = facebook_poster.post_to_facebook("p", "t", "m")
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_post_to_facebook_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "post_123"}
        with patch.object(facebook_poster.requests, "post", return_value=mock_resp):
            result = facebook_poster.post_to_facebook("page_abc", "token_xyz", "hello")
        self.assertTrue(result["success"])
        self.assertEqual(result["post_id"], "post_123")
        self.assertIn("facebook.com", result["url"])

    def test_post_to_facebook_api_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": {"code": 190, "message": "Invalid token"}}
        with patch.object(facebook_poster.requests, "post", return_value=mock_resp):
            result = facebook_poster.post_to_facebook("p", "bad", "m")
        self.assertFalse(result["success"])
        self.assertIn("Invalid token", result["error"])


class TestDashboardUpdater(unittest.TestCase):
    def test_retry_request_retries_on_5xx(self):
        from requests import Response
        fail_resp = Response()
        fail_resp.status_code = 503
        ok_resp = Response()
        ok_resp.status_code = 200
        with patch.object(dashboard_updater.requests, "request", side_effect=[fail_resp, ok_resp]):
            with patch.object(dashboard_updater.time, "sleep", lambda *_: None):
                resp = dashboard_updater._retry_request("GET", "https://api.github.com/test")
        self.assertEqual(resp.status_code, 200)

    def test_generate_activity_with_real_milestones(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp) / "milestones.json"
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            tmp_path.write_text(json.dumps({
                "categories": {
                    "Biotechnology": {"milestones": [
                        {"date": today, "value": 1, "unit": "x", "title": "T", "source": "S"}
                    ]}
                }
            }))
            with patch.object(dashboard_updater.pathlib.Path, "exists", return_value=True):
                with patch.object(dashboard_updater.pathlib.Path, "read_text", return_value=tmp_path.read_text()):
                    activity = dashboard_updater.generate_activity()
        self.assertEqual(len(activity["days"]), 30)
        self.assertIn("last_update", activity)
        self.assertIn("spikes", activity)

    def test_get_file_sha_404_returns_none(self):
        from requests import Response
        resp_404 = Response()
        resp_404.status_code = 404
        with patch.object(dashboard_updater.requests, "request", return_value=resp_404):
            sha = dashboard_updater.get_file_sha("owner", "repo", "missing.json")
        self.assertIsNone(sha)

    def test_get_file_sha_500_raises_file_fetch_error(self):
        from requests import Response
        resp_500 = Response()
        resp_500.status_code = 500
        resp_500._content = b"server error"
        with patch.object(dashboard_updater.requests, "request", return_value=resp_500):
            with patch.object(dashboard_updater.time, "sleep", lambda *_: None):
                with self.assertRaises(dashboard_updater.FileFetchError) as ctx:
                    dashboard_updater.get_file_sha("owner", "repo", "path.json")
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ctx.exception.owner, "owner")
        self.assertEqual(ctx.exception.repo, "repo")

    def test_get_file_sha_network_error_raises_file_fetch_error(self):
        from requests.exceptions import ConnectionError
        with patch.object(dashboard_updater.requests, "request", side_effect=ConnectionError("dns fail")):
            with patch.object(dashboard_updater.time, "sleep", lambda *_: None):
                with self.assertRaises(dashboard_updater.FileFetchError) as ctx:
                    dashboard_updater.get_file_sha("owner", "repo", "path.json")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("dns fail", ctx.exception.message)

    def test_upsert_file_skips_on_file_fetch_error(self):
        from requests import Response
        resp_500 = Response()
        resp_500.status_code = 500
        with patch.object(dashboard_updater.requests, "request", return_value=resp_500):
            with patch.object(dashboard_updater.time, "sleep", lambda *_: None):
                ok = dashboard_updater.upsert_file("owner", "repo", "data/x.json", b"{}", "msg")
        self.assertFalse(ok)

    def test_upsert_file_skips_unchanged_content(self):
        import base64

        from requests import Response
        resp = Response()
        resp.status_code = 200
        resp.json = lambda: {"sha": "abc", "content": base64.b64encode(b"{}").decode()}
        with patch.object(dashboard_updater.requests, "request", return_value=resp) as mock_req:
            with patch.object(dashboard_updater.time, "sleep", lambda *_: None):
                ok = dashboard_updater.upsert_file("owner", "repo", "data/x.json", b"{}", "msg")
        self.assertFalse(ok)
        # Only the GET happened — no wasteful PUT/commit for identical content.
        for call in mock_req.call_args_list:
            self.assertEqual(call.args[0], "GET")

    def test_upsert_file_puts_when_content_differs(self):
        from requests import Response
        get_resp = Response()
        get_resp.status_code = 200
        get_resp.json = lambda: {"sha": "old", "content": "e30="}  # b"{}"
        put_resp = Response()
        put_resp.status_code = 200
        put_resp.json = lambda: {"content": {}}
        with patch.object(dashboard_updater.requests, "request", side_effect=[get_resp, put_resp]):
            with patch.object(dashboard_updater.time, "sleep", lambda *_: None):
                ok = dashboard_updater.upsert_file("owner", "repo", "data/x.json", b"{1}", "msg")
        self.assertTrue(ok)


class TestJsonSchemas(unittest.TestCase):
    def test_existing_milestones_json_valid(self):
        path = ROOT / "data" / "milestones.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("categories", data)
            for _cat_name, cat in data["categories"].items():
                self.assertIn("icon", cat)
                self.assertIn("color", cat)
                self.assertIn("subcategories", cat)
                self.assertIsInstance(cat["milestones"], list)

    def test_existing_events_json_valid(self):
        path = ROOT / "data" / "events.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("events", data)
            for ev in data["events"]:
                self.assertIn("title", ev)
                self.assertIn("geolocation", ev)
                self.assertIn("lat", ev["geolocation"])
                self.assertIn("lon", ev["geolocation"])

    def test_existing_activity_json_valid(self):
        path = ROOT / "data" / "activity.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("days", data)
            self.assertEqual(len(data["days"]), 30)


class TestLlmOutput(unittest.TestCase):
    def test_facebook_poster_root_points_to_apis_dir(self):
        """ROOT must resolve to the apis/ directory (not workspace root).

        Bug: facebook_poster had 3x .parent which resolved outside the repo.
        """
        import social.facebook_poster as fb
        # ROOT / "data" must exist relative to this repo
        expected = ROOT / "data" / "milestones.json"
        self.assertEqual(fb.MILESTONES_JSON, expected)
        expected_history = ROOT / "data" / "fb_post_history.json"
        self.assertEqual(fb.POST_HISTORY, expected_history)

    def test_openai_client_singleton_created_once(self):
        """Client singleton must be reused across calls, not recreated each time."""
        import llm.score_milestone as sm
        sm._reset_router()
        client1 = sm._get_openai_client()
        client2 = sm._get_openai_client()
        self.assertIs(client1, client2)

    def test_openai_client_returns_none_on_init_failure(self):
        """If OpenAI client init raises, _get_openai_client returns None and caches the error.

        Only runs when openai lib is actually installed (CI installs it).
        """
        import llm.score_milestone as sm
        if sm.OpenAI is None:
            self.skipTest("openai lib not installed")
        sm._reset_router()
        with patch.object(sm.OpenAI, "__init__", side_effect=OSError("bad key")):
            result = sm._get_openai_client()
        self.assertIsNone(result)
        # Second call should also return None (cached error)
        result2 = sm._get_openai_client()
        self.assertIsNone(result2)


class TestYamlSchemas(unittest.TestCase):
    def test_replacements_yaml_well_formed(self):
        """The replacements dict is the single source of truth for fallback feeds."""
        for _cat, urls in source_checker.REPLACEMENTS.items():
            self.assertIsInstance(urls, list)
            for u in urls:
                self.assertTrue(u.startswith("http"), f"Bad replacement URL: {u}")


if __name__ == "__main__":
    unittest.main()
