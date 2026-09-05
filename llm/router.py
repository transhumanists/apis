#!/usr/bin/env python3
"""
Free Models Router — neohiro/LLM

Self-managed model selection with cascade fallback.
Picks the best available free model given a request,
with per-country preference, task matching, and health-gating.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ─── Data models ────────────────────────────────────────────────────────────────

@dataclass
class ModelChoice:
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
class ChatResult:
    content: str
    model: str
    provider: str
    country: str
    attempts: int
    latency_ms: float
    error: str | None = None


@dataclass
class RouterState:
    last_healthcheck: str
    healthy_providers: list[str] = field(default_factory=list)
    latency_p50: dict[str, float] = field(default_factory=dict)
    rate_budget_remaining: dict[str, float] = field(default_factory=dict)
    error_budget: dict[str, int] = field(default_factory=dict)
    total_requests: int = 0
    free_requests_used: int = 0
    paid_requests_used: int = 0


# ─── Tier order ────────────────────────────────────────────────────────────────

TIER_ORDER = ["unlimited", "free", "paid"]


# ─── Main router ──────────────────────────────────────────────────────────────

class FreeModelsRouter:
    CASCADE_RETRIES = 3
    HEALTH_GATE_MS = 3000
    VERIFY_DAYS = 7

    def __init__(
        self,
        providers_json: str | Path = "data/providers.json",
        models_json: str | Path = "data/models.json",
        free_json: str | Path = "data/free_models.json",
        unlimited_json: str | Path = "data/unlimited.json",
        state_path: str | Path = "data/router_state.json",
    ):
        self.providers = self._load(providers_json)
        self.models = self._load(models_json)
        self.free_data = self._load(free_json)
        self.unlimited_data = self._load(unlimited_json)
        self.state = self._load_state(state_path)
        self._state_path = Path(state_path)

    def _load(self, path: str | Path) -> dict:
        p = Path(path)
        if not p.exists():
            log.warning("Not found: %s — returning empty dict", p)
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _load_state(self, path: str | Path) -> RouterState:
        p = Path(path)
        if not p.exists():
            return RouterState(last_healthcheck="never")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if "public" in d and "private" in d:
                d = {**d["public"], **d["private"]}
            known = {k: v for k, v in d.items() if k in RouterState.__dataclass_fields__}
            return RouterState(**known)
        except Exception as e:
            log.warning("Could not load state: %s — resetting", e)
            return RouterState(last_healthcheck="never")

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._state_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self.state), f, indent=2)

    # ── Candidate generation ─────────────────────────────────────────────────

    def _candidates(
        self,
        task: str | None = None,
        preferred_tier: str = "any",
        country: str | None = None,
        modality: str | None = None,
        max_tokens: int | None = None,
    ) -> list[ModelChoice]:
        """Generate ranked model choices across all tiers."""
        candidates: list[ModelChoice] = []

        for tier in TIER_ORDER:
            if preferred_tier not in ("any", tier):
                continue
            keys = self._tier_to_keys(tier)
            for key in keys:
                choice = self._evaluate(key, tier, task, country, modality)
                if choice:
                    candidates.append(choice)

        candidates.sort(key=lambda c: c.overall_score, reverse=True)
        return candidates

    def _tier_to_keys(self, tier: str) -> list[str]:
        if tier == "unlimited":
            keys = set()
            for k, v in self.models.items():
                if v.get("tier") == "unlimited":
                    keys.add(k)
            return list(keys)
        if tier == "free":
            flat = []
            for provider_id, prov in self.providers.items():
                for model_id in prov.get("free_models", []):
                    key = f"{provider_id}:{model_id}"
                    if key in self.models:
                        flat.append(key)
                    elif model_id in self.models:
                        flat.append(model_id)
                    else:
                        flat.append(key)
            return flat
        return []

    def _evaluate(
        self,
        key: str,
        tier: str,
        task: str | None,
        country: str | None,
        modality: str | None,
    ) -> ModelChoice | None:
        model_data = self.models.get(key, {})
        provider_id = model_data.get("provider", key.split(":")[0])
        provider = self.providers.get(provider_id, {})

        if not model_data:
            model_data = {"provider": provider_id, "country": "US", "tier": tier}
            model_key = key
        else:
            model_key = key

        country_code = model_data.get("country", "US")
        health = self.state.latency_p50.get(provider_id, 9999)
        health_score = max(0, 1 - (health / self.HEALTH_GATE_MS)) if health < self.HEALTH_GATE_MS else 0

        errors = self.state.error_budget.get(provider_id, 0)
        error_score = max(0, 1 - errors * 0.1)

        country_score = 0.0
        if country and country_code == country:
            country_score = 1.0
        elif country_code in ("LOCAL", "US"):
            country_score = 0.5 if country else 0.3

        task_match = self._task_match(model_data.get("suitable_for", []), task)

        overall = (
            task_match * 0.4
            + health_score * 0.3
            + error_score * 0.2
            + country_score * 0.1
        )

        reason = f"task={task_match:.2f} health={health_score:.2f} errors={errors} country={country_score:.2f}"

        return ModelChoice(
            model=model_key,
            provider=provider_id,
            base_url=provider.get("base_url", ""),
            country=country_code,
            tier=tier,
            task_match=task_match,
            health_score=health_score,
            overall_score=overall,
            reason=reason,
        )

    @staticmethod
    def _task_match(suitable_for: list[str], task: str | None) -> float:
        if not task:
            return 0.5
        task = task.lower()
        weights = {
            "classify": ["classify", "categorize", "extract"],
            "summarize": ["summarize", "condense", "abstract"],
            "reason": ["reason", "think", "chain", "math", "logic"],
            "code": ["code", "programming", "refactor", "review", "completion"],
            "vision": ["vision", "image", "ocr", "visual"],
            "audio": ["audio", "transcribe", "speech", "whisper"],
            "chat": ["chat", "conversation", "qa", "instruct"],
            "rag": ["rag", "retrieve", "context"],
        }
        for intent, synonyms in weights.items():
            if intent in task or any(s in task for s in synonyms):
                return 1.0 if intent in suitable_for else 0.4
        return 0.6

    # ── Public API ───────────────────────────────────────────────────────────

    def pick(
        self,
        task: str | None = None,
        preferred_tier: str = "any",
        country: str | None = None,
        modality: str | None = None,
        max_tokens: int | None = None,
    ) -> ModelChoice:
        """Return the best model for the given constraints."""
        candidates = self._candidates(task, preferred_tier, country, modality, max_tokens)
        if not candidates:
            log.error("No models available for task=%s tier=%s country=%s", task, preferred_tier, country)
            raise RuntimeError("No models available matching constraints")
        best = candidates[0]
        log.info(
            "PICK model=%s provider=%s tier=%s country=%s score=%.3f reason=%s",
            best.model, best.provider, best.tier, best.country, best.overall_score, best.reason
        )
        return best

    def chat(
        self,
        messages: list[dict],
        preferred_tier: str = "free",
        task: str | None = None,
        country: str | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Run a chat completion with cascade fallback."""
        start = time.monotonic()
        attempts = 0
        last_error = None

        for attempt in range(self.CASCADE_RETRIES):
            candidates = self._candidates(task, preferred_tier, country)
            if not candidates:
                break

            choice = candidates[attempt] if attempt < len(candidates) else candidates[0]
            attempts += 1

            try:
                content = self._call_api(choice, messages, max_tokens)
                latency_ms = (time.monotonic() - start) * 1000
                self.state.total_requests += 1
                if choice.tier == "unlimited":
                    pass
                elif choice.tier == "free":
                    self.state.free_requests_used += 1
                else:
                    self.state.paid_requests_used += 1
                self._save_state()
                return ChatResult(
                    content=content,
                    model=choice.model,
                    provider=choice.provider,
                    country=choice.country,
                    attempts=attempts,
                    latency_ms=latency_ms,
                )
            except Exception as e:
                last_error = str(e)
                log.warning("Attempt %d failed for %s: %s", attempt + 1, choice.model, e)
                if choice.provider in self.state.error_budget:
                    self.state.error_budget[choice.provider] += 1
                else:
                    self.state.error_budget[choice.provider] = 1

        self.state.total_requests += 1
        self.state.paid_requests_used += 1
        self._save_state()
        return ChatResult(
            content="",
            model="",
            provider="",
            country="",
            attempts=attempts,
            latency_ms=(time.monotonic() - start) * 1000,
            error=last_error or "All providers exhausted",
        )

    def _call_api(
        self,
        choice: ModelChoice,
        messages: list[dict],
        max_tokens: int | None,
    ) -> str:
        provider = self.providers.get(choice.provider, {})
        base_url = provider.get("base_url", "")

        if choice.provider == "ollama":
            return self._call_ollama(choice.model.replace("ollama:", ""), messages, max_tokens)

        api_key = os.environ.get(provider.get("env_key", ""), "")
        if not api_key and choice.provider not in ("ollama",):
            raise RuntimeError(f"No API key for {choice.provider}: set {provider.get('env_key')}")

        model_id = choice.model.split(":", 1)[1] if ":" in choice.model else choice.model

        payload = {
            "model": model_id,
            "messages": messages,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}") from e

    def _call_ollama(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int | None,
    ) -> str:
        base_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        payload = {"model": model, "messages": messages}
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return result["message"]["content"]
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama unreachable: {e.reason}") from e

    def get_stats(self) -> dict:
        return {
            "total_requests": self.state.total_requests,
            "free_requests_used": self.state.free_requests_used,
            "paid_requests_used": self.state.paid_requests_used,
            "last_healthcheck": self.state.last_healthcheck,
            "healthy_providers": self.state.healthy_providers,
            "latency_p50": self.state.latency_p50,
        }


if __name__ == "__main__":
    import sys
    router = FreeModelsRouter()

    task = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        choice = router.pick(task=task, preferred_tier="any")
        print(json.dumps(asdict(choice), indent=2))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

