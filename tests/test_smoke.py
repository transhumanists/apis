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
        md = llm_scorer.generate_milestones_md(cats, {})
        self.assertIn("1. Biotechnology", md)
        self.assertIn("2. Robotics", md)
        self.assertIn("remote_surgery", md)
        self.assertIn("CRISPR record", md)
        self.assertIn("First robotic surgery", md)
        self.assertIn("Auto-generated", md)


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


class TestJsonSchemas(unittest.TestCase):
    def test_existing_milestones_json_valid(self):
        path = ROOT / "data" / "milestones.json"
        if path.exists():
            data = json.loads(path.read_text())
            self.assertIn("categories", data)
            for _cat_name, cat in data["categories"].items():
                self.assertIn("icon", cat)
                self.assertIn("color", cat)
                self.assertIn("subcategories", cat)
                self.assertIsInstance(cat["milestones"], list)

    def test_existing_events_json_valid(self):
        path = ROOT / "data" / "events.json"
        if path.exists():
            data = json.loads(path.read_text())
            self.assertIn("events", data)
            for ev in data["events"]:
                self.assertIn("title", ev)
                self.assertIn("geolocation", ev)
                self.assertIn("lat", ev["geolocation"])
                self.assertIn("lon", ev["geolocation"])

    def test_existing_activity_json_valid(self):
        path = ROOT / "data" / "activity.json"
        if path.exists():
            data = json.loads(path.read_text())
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
