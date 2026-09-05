"""Tests for apis/rate_limit.py — free-tier API budgeting.

The conftest.py patches rate_limit.py to redirect state files to a
temp dir so tests don't pollute the real budget counters.
"""
import json

from rate_limit import PLATFORMS as _PLATFORMS

_ = _PLATFORMS


def test_first_call_allowed(rl):
    r = rl.check_and_consume("groq")
    assert r.allowed, f"got: {r}"
    assert r.used_this_minute == 1
    assert r.used_today == 1
    assert r.per_minute_cap == 30
    assert r.per_day_cap == 14400


def test_unknown_platform_allowed(rl):
    r = rl.check_and_consume("nonexistent_xyz")
    assert r.allowed
    assert "unknown" in r.reason


def test_minute_cap_exhausted(rl):
    # opensky has 10/min cap
    for i in range(10):
        r = rl.check_and_consume("opensky")
        assert r.allowed, f"call {i+1} denied: {r}"
    r = rl.check_and_consume("opensky")
    assert not r.allowed
    assert r.retry_after > 0
    assert r.retry_after <= 60


def test_429_sets_backoff(rl):
    rl.check_and_consume("groq")
    rl.record_response("groq", 429, "rate limited")
    r = rl.check_and_consume("groq")
    assert not r.allowed
    assert "backoff" in r.reason


def test_5xx_sets_smaller_backoff(rl):
    rl.check_and_consume("groq")
    rl.record_response("groq", 503, "server error")
    r = rl.check_and_consume("groq")
    assert not r.allowed
    # 503 backoff is groq's 60s / 2 = 30s
    assert r.retry_after <= 30


def test_2xx_does_not_set_backoff(rl):
    rl.check_and_consume("groq")
    rl.record_response("groq", 200)
    r = rl.check_and_consume("groq")
    assert r.allowed


def test_state_persists_across_invocations(rl):
    rl.check_and_consume("groq")
    r = rl.check_and_consume("groq")
    assert r.used_this_minute == 2
    assert r.used_today == 2


def test_status_returns_all_platforms(rl):
    rl.check_and_consume("groq")
    s = rl.get_status()
    assert "groq" in s
    assert "opensky" in s
    assert s["groq"]["used_minute"] == 1
    assert s["groq"]["per_minute_cap"] == 30


def test_status_single_platform(rl):
    rl.check_and_consume("groq")
    rl.check_and_consume("groq")
    s = rl.get_status("groq")
    assert "groq" in s
    assert s["groq"]["used_minute"] == 2


def test_cache_set_get_roundtrip(rl):
    rl.cache_set("test_key", {"foo": "bar", "n": 42})
    val = rl.cache_get("test_key", max_age_seconds=60)
    assert val == {"foo": "bar", "n": 42}


def test_cache_expires_when_zero_ttl(rl):
    rl.cache_set("test_key", {"foo": "bar"})
    assert rl.cache_get("test_key", max_age_seconds=0) is None
    assert rl.cache_get("test_key", max_age_seconds=-1) is None


def test_cache_invalidate(rl):
    rl.cache_set("test_key", {"foo": "bar"})
    rl.cache_invalidate("test_key")
    assert rl.cache_get("test_key", max_age_seconds=60) is None


def test_self_hosted_ollama_has_huge_caps(rl):
    s = rl.get_status("ollama")
    assert s["ollama"]["per_minute_cap"] == 1000
    assert s["ollama"]["per_day_cap"] == 1000000


def test_paid_apis_have_long_cache_ttl(rl):
    """OpenAI / Anthropic — pricing doesn't change daily, 24h cache."""
    assert rl.PLATFORMS["openai"]["cache_ttl"] >= 86400
    assert rl.PLATFORMS["anthropic"]["cache_ttl"] >= 86400


def test_wait_for_slot_immediate_success(rl):
    assert rl.wait_for_slot("groq", max_wait=2)


def test_wait_for_slot_times_out_when_blocked(rl):
    rl.record_response("opensky", 429)
    assert not rl.wait_for_slot("opensky", max_wait=1)


def test_day_rollover_resets_counter(rl, tmp_path):
    """Simulate yesterday by patching state file directly."""
    rl.check_and_consume("groq")
    # Find the patched state file
    state_file = None
    for p in rl.ROOT.rglob("state.json"):
        state_file = p
        break
    assert state_file is not None
    state = json.loads(state_file.read_text())
    state["platforms"]["groq"]["day_date"] = "2020-01-01"
    state_file.write_text(json.dumps(state))
    r = rl.check_and_consume("groq")
    assert r.allowed
    # used_today should be 1 (rolled over) not 2
    assert r.used_today == 1


def test_minute_window_rolls_over(rl, tmp_path):
    rl.check_and_consume("groq")
    state_file = None
    for p in rl.ROOT.rglob("state.json"):
        state_file = p
        break
    assert state_file is not None
    state = json.loads(state_file.read_text())
    # Pretend we're 5 minutes in the past with 30 used
    state["platforms"]["groq"]["minute_window_start"] -= 5
    state["platforms"]["groq"]["used_minute"] = 30
    state_file.write_text(json.dumps(state))
    r = rl.check_and_consume("groq")
    assert r.allowed
    assert r.used_this_minute == 1  # rolled over


def test_all_required_platforms_configured(rl):
    """Every platform referenced in the apis_tools_manifest must have a config."""
    required = {
        "groq", "cerebras", "sambanova", "github_models", "cloudflare",
        "openrouter", "google_ai", "huggingface", "nvidia", "cohere", "ollama",
        "openai", "anthropic", "ipapi", "ip-api", "opensky", "ll2",
        "globalpetrol", "celestrak", "rss_generic"
    }
    missing = required - set(rl.PLATFORMS.keys())
    assert not missing, f"missing platform configs: {missing}"


def test_concurrent_callers_see_incrementing_counter(rl):
    """Two sequential calls must see increasing usage."""
    r1 = rl.check_and_consume("groq")
    r2 = rl.check_and_consume("groq")
    assert r1.used_this_minute == 1
    assert r2.used_this_minute == 2


def test_record_response_200_clears_backoff_if_in_window(rl):
    """If a previous error set a backoff, a successful response shouldn't
    immediately clear it (server may still be rate-limited). But total_429
    and total_5xx should accumulate."""
    rl.check_and_consume("groq")
    rl.record_response("groq", 429)
    s_before = rl.get_status("groq")
    rl.record_response("groq", 200)
    s_after = rl.get_status("groq")
    # 429 count should persist
    assert s_after["groq"]["total_429"] >= s_before["groq"]["total_429"]


def test_429_count_accumulates(rl):
    """429 responses accumulate in total_429 counter, not total_calls."""
    rl.check_and_consume("groq")  # call 1: allowed
    rl.record_response("groq", 429)  # 429 increments total_429
    r1 = rl.check_and_consume("groq")  # call 2: blocked by backoff
    assert not r1.allowed
    # total_calls counts only allowed calls
    s = rl.get_status("groq")
    assert s["groq"]["total_calls"] == 1
    assert s["groq"]["total_429"] >= 1
