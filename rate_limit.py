"""
apis/rate_limit.py — single source of truth for free-tier API budgeting.

Every apis script that makes an upstream call MUST go through this module.
Why: GitHub Actions free tier is 2000 min/month, Render free tier is
750 hr/month, and every paid/limited platform (Groq, OpenRouter, OpenAI,
Anthropic, ipapi.co, OpenSky, Launch Library 2) has its own rate cap.
A misbehaving cron job can drain a quota in minutes.

Design:
- Persistent counters in data/rate_limit_state.json
- Per-platform budget config with: per_minute, per_day, backoff
- Optimistic concurrency: we never block the main thread, we just
  track and refuse to exceed the budget
- Refresh cadence enforced via `next_allowed_after` (ISO timestamp)
- Cold cache is preferred over hot fetch (configurable TTL per data kind)
- All platform errors return RateLimitResult with retry_after seconds
- Thread-safe via simple lock (we never expect multi-thread writers)
"""
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "rate_limit_state.json"

# Platform configs: per_minute, per_day, backoff_seconds, cache_ttl_seconds
PLATFORMS: dict[str, dict] = {
    # ── Free tier providers (LLM chat) ──
    "groq":            {"per_minute": 30, "per_day": 14400, "backoff": 60,  "cache_ttl": 60},
    "cerebras":        {"per_minute": 30, "per_day": 14400, "backoff": 60,  "cache_ttl": 60},
    "sambanova":       {"per_minute": 20, "per_day": 5000,  "backoff": 60,  "cache_ttl": 60},
    "github_models":   {"per_minute": 15, "per_day": 200,   "backoff": 120, "cache_ttl": 120},
    "cloudflare":      {"per_minute": 60, "per_day": 10000, "backoff": 30,  "cache_ttl": 60},
    "openrouter":      {"per_minute": 20, "per_day": 200,   "backoff": 60,  "cache_ttl": 60},
    "google_ai":       {"per_minute": 15, "per_day": 1500,  "backoff": 60,  "cache_ttl": 60},
    "huggingface":     {"per_minute": 30, "per_day": 1000,  "backoff": 30,  "cache_ttl": 60},
    "nvidia":          {"per_minute": 20, "per_day": 5000,  "backoff": 30,  "cache_ttl": 60},
    "cohere":          {"per_minute": 30, "per_day": 1000,  "backoff": 30,  "cache_ttl": 60},
    "ollama":          {"per_minute": 1000, "per_day": 1000000, "backoff": 5, "cache_ttl": 0},  # self-hosted, no cap

    # ── Paid (or limited) commercial APIs ──
    "openai":          {"per_minute": 60, "per_day": 10000, "backoff": 60,  "cache_ttl": 86400},
    "anthropic":       {"per_minute": 60, "per_day": 10000, "backoff": 60,  "cache_ttl": 86400},

    # ── Data sources ──
    "ipapi":           {"per_minute": 60, "per_day": 50000, "backoff": 30,  "cache_ttl": 86400},  # 50k/month free
    "ip-api":          {"per_minute": 45, "per_day": 50000, "backoff": 30,  "cache_ttl": 86400},  # 45 rpm free
    "opensky":         {"per_minute": 10, "per_day": 400,   "backoff": 60,  "cache_ttl": 60},     # anonymous tier
    "ll2":             {"per_minute": 30, "per_day": 5000,  "backoff": 30,  "cache_ttl": 3600},   # thespacedevs
    "globalpetrol":    {"per_minute": 10, "per_day": 100,   "backoff": 120, "cache_ttl": 21600},
    "celestrak":       {"per_minute": 30, "per_day": 5000,  "backoff": 30,  "cache_ttl": 86400},
    "rss_generic":     {"per_minute": 60, "per_day": 50000, "backoff": 30,  "cache_ttl": 3600},
}


@dataclass
class RateLimitResult:
    allowed: bool
    platform: str
    used_this_minute: int
    used_today: int
    per_minute_cap: int
    per_day_cap: int
    retry_after: int = 0  # seconds
    reason: str = ""


@dataclass
class _State:
    platforms: dict = field(default_factory=dict)  # {platform: {minute_window_start, used_minute, day_date, used_day, last_reset, total_calls, total_429, total_5xx, last_error_at, next_allowed_after}}

_lock = threading.Lock()


def _ensure_state() -> _State:
    if not STATE_FILE.exists():
        return _State()
    try:
        d = json.loads(STATE_FILE.read_text())
        s = _State()
        s.platforms = d.get("platforms", {})
        return s
    except (json.JSONDecodeError, KeyError):
        return _State()


def _save_state(s: _State) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"platforms": s.platforms}, indent=2))
    tmp.replace(STATE_FILE)


def _now_minute_window() -> int:
    return int(time.time() // 60)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _init_platform(state: _State, platform: str) -> dict:
    if platform not in state.platforms:
        state.platforms[platform] = {
            "minute_window_start": _now_minute_window(),
            "used_minute": 0,
            "day_date": _today_key(),
            "used_day": 0,
            "total_calls": 0,
            "total_429": 0,
            "total_5xx": 0,
            "last_error_at": None,
            "next_allowed_after": 0,
        }
    p = state.platforms[platform]
    # Roll minute window if changed
    cur_min = _now_minute_window()
    if p["minute_window_start"] != cur_min:
        p["minute_window_start"] = cur_min
        p["used_minute"] = 0
    # Roll day if changed
    cur_day = _today_key()
    if p["day_date"] != cur_day:
        p["day_date"] = cur_day
        p["used_day"] = 0
    return p


def check_and_consume(platform: str) -> RateLimitResult:
    """Reserve a slot for `platform`. Increments counters on success.

    Returns RateLimitResult.allowed=False if budget exhausted.
    Caller MUST respect retry_after (seconds) before next call.
    """
    if platform not in PLATFORMS:
        # Unknown platform — allow but log
        return RateLimitResult(
            allowed=True, platform=platform,
            used_this_minute=0, used_today=0,
            per_minute_cap=999999, per_day_cap=999999,
            reason="unknown platform (no cap enforced)"
        )
    cfg = PLATFORMS[platform]

    with _lock:
        state = _ensure_state()
        p = _init_platform(state, platform)

        # Backoff gate
        now = int(time.time())
        if p["next_allowed_after"] > now:
            return RateLimitResult(
                allowed=False, platform=platform,
                used_this_minute=p["used_minute"], used_today=p["used_day"],
                per_minute_cap=cfg["per_minute"], per_day_cap=cfg["per_day"],
                retry_after=p["next_allowed_after"] - now,
                reason="in backoff window"
            )

        # Daily cap
        if p["used_day"] >= cfg["per_day"]:
            next_day = datetime.now(timezone.utc)
            secs_to_midnight = 86400 - (next_day.hour * 3600 + next_day.minute * 60 + next_day.second)
            return RateLimitResult(
                allowed=False, platform=platform,
                used_this_minute=p["used_minute"], used_today=p["used_day"],
                per_minute_cap=cfg["per_minute"], per_day_cap=cfg["per_day"],
                retry_after=secs_to_midnight,
                reason="daily cap exhausted"
            )

        # Per-minute cap
        if p["used_minute"] >= cfg["per_minute"]:
            secs_to_next_min = 60 - (int(time.time()) % 60)
            return RateLimitResult(
                allowed=False, platform=platform,
                used_this_minute=p["used_minute"], used_today=p["used_day"],
                per_minute_cap=cfg["per_minute"], per_day_cap=cfg["per_day"],
                retry_after=secs_to_next_min,
                reason="per-minute cap exhausted"
            )

        # Consume a slot
        p["used_minute"] += 1
        p["used_day"] += 1
        p["total_calls"] += 1
        _save_state(state)
        return RateLimitResult(
            allowed=True, platform=platform,
            used_this_minute=p["used_minute"], used_today=p["used_day"],
            per_minute_cap=cfg["per_minute"], per_day_cap=cfg["per_day"],
        )


def record_response(platform: str, status: int, error: str = "") -> None:
    """Update backoff window based on platform response.

    429 -> set next_allowed_after = now + backoff
    5xx -> set next_allowed_after = now + backoff/2
    other -> nothing
    """
    if platform not in PLATFORMS:
        return
    cfg = PLATFORMS[platform]
    with _lock:
        state = _ensure_state()
        p = _init_platform(state, platform)
        now = int(time.time())
        if status == 429:
            p["total_429"] += 1
            p["next_allowed_after"] = now + cfg["backoff"]
            p["last_error_at"] = datetime.now(timezone.utc).isoformat()
        elif 500 <= status < 600:
            p["total_5xx"] += 1
            p["next_allowed_after"] = now + max(5, cfg["backoff"] // 2)
            p["last_error_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)


def get_status(platform: str | None = None) -> dict:
    """Get current usage. For dashboards / debugging."""
    with _lock:
        state = _ensure_state()
        out = {}
        plats = [platform] if platform else list(PLATFORMS.keys())
        for p in plats:
            if p not in PLATFORMS:
                continue
            cfg = PLATFORMS[p]
            s = state.platforms.get(p, {})
            out[p] = {
                "per_minute_cap": cfg["per_minute"],
                "per_day_cap": cfg["per_day"],
                "used_minute": s.get("used_minute", 0),
                "used_day": s.get("used_day", 0),
                "total_calls": s.get("total_calls", 0),
                "total_429": s.get("total_429", 0),
                "total_5xx": s.get("total_5xx", 0),
                "next_allowed_after": s.get("next_allowed_after", 0),
                "now": int(time.time()),
            }
        return out


def reset(platform: str | None = None) -> None:
    """Reset counters. Used by tests."""
    with _lock:
        state = _ensure_state()
        if platform:
            state.platforms.pop(platform, None)
        else:
            state.platforms.clear()
        _save_state(state)


def wait_for_slot(platform: str, max_wait: int = 300) -> bool:
    """Block (polling) until a slot is available or max_wait seconds elapses.

    Returns True if slot acquired, False if timed out.
    Use this only in long-running scripts. For batch jobs, use
    check_and_consume() to skip requests when budget exhausted.
    """
    waited = 0
    while waited < max_wait:
        res = check_and_consume(platform)
        if res.allowed:
            return True
        wait = min(res.retry_after, 10, max_wait - waited)
        if wait <= 0:
            return False
        time.sleep(wait)
        waited += wait
    return False


# ── Simple disk-backed cache ──────────────────────────────────────────
CACHE_DIR = ROOT / "data" / "rate_limit_cache"


def cache_get(key: str, max_age_seconds: int) -> dict | None:
    """Return cached value if fresh, else None."""
    if max_age_seconds <= 0:
        return None
    p = CACHE_DIR / f"{key}.json"
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text())
        if int(time.time()) - meta.get("_cached_at", 0) > max_age_seconds:
            return None
        return meta.get("value")
    except (json.JSONDecodeError, KeyError):
        return None


def cache_set(key: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{key}.json"
    p.write_text(json.dumps({"_cached_at": int(time.time()), "value": value}))


def cache_invalidate(key: str) -> None:
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        p.unlink()
