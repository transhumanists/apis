"""Tests for the neohiro/LLM FreeModelsRouter integration.

Covers:
  - Vendor data files present and parseable
  - call_llm_router() returns parsed JSON on success
  - call_llm_router() returns None on router error
  - call_llm_router() returns None on malformed JSON
  - call_llm() ordering: router first, then OpenAI, then Anthropic
  - call_llm() respects LLM_ROUTER_ENABLED=false
  - score_article() allows router-only path (no OPENAI/ANTHROPIC key)
  - FreeModelsRouter.pick() returns a valid ModelChoice
  - Cascade: when first provider fails, second is tried
"""
import importlib
import json
import os
import pathlib
import sys
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@dataclass
class _FakeChoice:
    model: str
    provider: str
    base_url: str
    country: str
    tier: str
    task_match: float
    health_score: float
    overall_score: float
    reason: str


@dataclass
class _FakeChatResult:
    content: str
    model: str
    provider: str
    country: str
    attempts: int
    latency_ms: float
    error: str | None = None


class TestVendorDataPresent(unittest.TestCase):
    """The vendored router data files must be present and parseable."""

    def test_router_script_vendored(self):
        p = ROOT / "llm" / "router.py"
        self.assertTrue(p.exists(), f"Missing vendored file: {p}")
        self.assertGreater(p.stat().st_size, 1000)

    def test_providers_json_vendored(self):
        p = ROOT / "data" / "providers.json"
        self.assertTrue(p.exists(), f"Missing vendored file: {p}")
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        self.assertIn("groq", data)
        self.assertIn("base_url", data["groq"])
        self.assertIn("env_key", data["groq"])

    def test_models_json_vendored(self):
        p = ROOT / "data" / "models.json"
        self.assertTrue(p.exists(), f"Missing vendored file: {p}")
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        self.assertGreater(len(data), 0)
        model_keys = [k for k in data if k != "_meta"]
        self.assertGreater(len(model_keys), 0)
        for key in model_keys[:3]:
            entry = data[key]
            self.assertIn("provider", entry, f"Model {key} missing 'provider'")

    def test_free_models_json_vendored(self):
        p = ROOT / "data" / "free_models.json"
        self.assertTrue(p.exists(), f"Missing vendored file: {p}")
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(data, dict)

    def test_unlimited_json_vendored(self):
        p = ROOT / "data" / "unlimited.json"
        self.assertTrue(p.exists(), f"Missing vendored file: {p}")
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(data, dict)

    def test_no_bom_in_vendored_files(self):
        for f in [
            ROOT / "llm" / "router.py",
            ROOT / "data" / "providers.json",
            ROOT / "data" / "models.json",
            ROOT / "data" / "free_models.json",
            ROOT / "data" / "unlimited.json",
        ]:
            raw = f.read_bytes()
            self.assertFalse(
                raw.startswith(b"\xef\xbb\xbf"),
                f"{f.name} has UTF-8 BOM — will break json.load()",
            )


class TestFreeModelsRouterImport(unittest.TestCase):
    """The router must be importable from the apis project."""

    def test_router_importable_from_llm(self):
        sys.path.insert(0, str(ROOT / "llm"))
        from router import FreeModelsRouter  # noqa: F401

    def test_router_init_with_vendored_data(self):
        sys.path.insert(0, str(ROOT / "llm"))
        from router import FreeModelsRouter

        r = FreeModelsRouter(
            providers_json=str(ROOT / "data" / "providers.json"),
            models_json=str(ROOT / "data" / "models.json"),
            free_json=str(ROOT / "data" / "free_models.json"),
            unlimited_json=str(ROOT / "data" / "unlimited.json"),
            state_path=str(ROOT / "data" / "router_state.json"),
        )
        self.assertGreater(len(r.providers), 0)
        self.assertGreater(len(r.models), 0)

    def test_router_pick_returns_model_choice(self):
        sys.path.insert(0, str(ROOT / "llm"))
        from router import FreeModelsRouter

        r = FreeModelsRouter(
            providers_json=str(ROOT / "data" / "providers.json"),
            models_json=str(ROOT / "data" / "models.json"),
            free_json=str(ROOT / "data" / "free_models.json"),
            unlimited_json=str(ROOT / "data" / "unlimited.json"),
            state_path=str(ROOT / "data" / "router_state.json"),
        )
        choice = r.pick(task="classify")
        self.assertIsNotNone(choice.model)
        self.assertIsNotNone(choice.provider)
        self.assertIn(choice.tier, ("unlimited", "free", "paid"))


class TestCallLlmRouter(unittest.TestCase):
    """call_llm_router() must wrap the FreeModelsRouter correctly."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("OPENAI_API_KEY", "test-key")
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
        cls.scorer = importlib.import_module("llm.score_milestone")

    def setUp(self):
        self.scorer._reset_router()

    def _mock_router(self, chat_result: _FakeChatResult):
        mock = MagicMock()
        mock.chat.return_value = chat_result
        return mock

    def test_returns_parsed_json_on_success(self):
        result = _FakeChatResult(
            content='{"is_milestone": true, "category": "Computing & AGI"}',
            model="groq:llama-3.3-70b-versatile", provider="groq", country="US",
            attempts=1, latency_ms=200.0,
        )
        with patch.object(self.scorer, "_get_router", return_value=self._mock_router(result)):
            actual = self.scorer.call_llm_router("Title", "Summary")
        self.assertEqual(actual, {"is_milestone": True, "category": "Computing & AGI"})

    def test_returns_none_on_router_error(self):
        result = _FakeChatResult(
            content="", model="", provider="", country="",
            attempts=3, latency_ms=0.0, error="All providers exhausted",
        )
        with patch.object(self.scorer, "_get_router", return_value=self._mock_router(result)):
            actual = self.scorer.call_llm_router("t", "s")
        self.assertIsNone(actual)

    def test_returns_none_on_malformed_json(self):
        result = _FakeChatResult(
            content="not valid json",
            model="groq:llama-3.3-70b", provider="groq", country="US",
            attempts=1, latency_ms=100.0,
        )
        with patch.object(self.scorer, "_get_router", return_value=self._mock_router(result)):
            actual = self.scorer.call_llm_router("t", "s")
        self.assertIsNone(actual)

    def test_strips_json_fences(self):
        result = _FakeChatResult(
            content='```json\n{"is_milestone": false}\n```',
            model="groq:llama", provider="groq", country="US",
            attempts=1, latency_ms=100.0,
        )
        with patch.object(self.scorer, "_get_router", return_value=self._mock_router(result)):
            actual = self.scorer.call_llm_router("t", "s")
        self.assertEqual(actual, {"is_milestone": False})

    def test_returns_none_on_router_init_failure(self):
        with patch.object(self.scorer, "_get_router", return_value=None):
            actual = self.scorer.call_llm_router("t", "s")
        self.assertIsNone(actual)


class TestCallLlmOrdering(unittest.TestCase):
    """call_llm() tries router → OpenAI → Anthropic in that order."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("OPENAI_API_KEY", "test-key")
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
        cls.scorer = importlib.import_module("llm.score_milestone")

    def test_router_first_when_enabled(self):
        router_result = {"is_milestone": True, "category": "X"}
        with patch.object(self.scorer, "call_llm_router", return_value=router_result) as r1:
            with patch.object(self.scorer, "call_llm_openai") as r2:
                with patch.object(self.scorer, "call_llm_anthropic") as r3:
                    result = self.scorer.call_llm("t", "s")
        self.assertEqual(result, router_result)
        r1.assert_called_once()
        r2.assert_not_called()
        r3.assert_not_called()

    def test_falls_back_to_openai_when_router_returns_none(self):
        with patch.object(self.scorer, "LLM_ROUTER_ENABLED", True):
            with patch.object(self.scorer, "call_llm_router", return_value=None):
                with patch.object(self.scorer, "call_llm_openai", return_value={"is_milestone": True}) as r2:
                    with patch.object(self.scorer, "call_llm_anthropic") as r3:
                        result = self.scorer.call_llm("t", "s")
        self.assertEqual(result, {"is_milestone": True})
        r2.assert_called_once()
        r3.assert_not_called()

    def test_falls_back_to_anthropic_when_router_and_openai_return_none(self):
        with patch.object(self.scorer, "LLM_ROUTER_ENABLED", True):
            with patch.object(self.scorer, "call_llm_router", return_value=None):
                with patch.object(self.scorer, "call_llm_openai", return_value=None):
                    with patch.object(self.scorer, "call_llm_anthropic", return_value={"is_milestone": False}) as r3:
                        result = self.scorer.call_llm("t", "s")
        self.assertEqual(result, {"is_milestone": False})
        r3.assert_called_once()

    def test_skips_router_when_disabled(self):
        with patch.object(self.scorer, "LLM_ROUTER_ENABLED", False):
            with patch.object(self.scorer, "call_llm_router") as r1:
                with patch.object(self.scorer, "call_llm_openai", return_value={"is_milestone": True}) as r2:
                    result = self.scorer.call_llm("t", "s")
        self.assertEqual(result, {"is_milestone": True})
        r1.assert_not_called()
        r2.assert_called_once()

    def test_returns_none_when_all_three_fail(self):
        with patch.object(self.scorer, "LLM_ROUTER_ENABLED", True):
            with patch.object(self.scorer, "call_llm_router", return_value=None):
                with patch.object(self.scorer, "call_llm_openai", return_value=None):
                    with patch.object(self.scorer, "call_llm_anthropic", return_value=None):
                        result = self.scorer.call_llm("t", "s")
        self.assertIsNone(result)


class TestScoreArticleRouterOnly(unittest.TestCase):
    """score_article() must allow router-only operation (no paid LLM keys)."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("OPENAI_API_KEY", "test-key")
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
        cls.scorer = importlib.import_module("llm.score_milestone")

    def test_allows_router_only_path_without_paid_keys(self):
        with patch.object(self.scorer, "LLM_ROUTER_ENABLED", True):
            with patch.object(self.scorer, "OPENAI_API_KEY", ""):
                with patch.object(self.scorer, "ANTHROPIC_API_KEY", ""):
                    with patch.object(self.scorer, "call_llm", return_value={"is_milestone": False}):
                        try:
                            self.scorer.score_article({"title": "X", "summary": "y"})
                        except SystemExit as e:
                            self.fail(f"score_article exited when router was enabled: {e}")

    def test_exits_when_no_router_and_no_keys(self):
        with patch.object(self.scorer, "LLM_ROUTER_ENABLED", False):
            with patch.object(self.scorer, "OPENAI_API_KEY", ""):
                with patch.object(self.scorer, "ANTHROPIC_API_KEY", ""):
                    with self.assertRaises(SystemExit):
                        self.scorer.score_article({"title": "X", "summary": "y"})


if __name__ == "__main__":
    unittest.main()
