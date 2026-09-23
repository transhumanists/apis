#!/usr/bin/env python3
"""
LLM Milestone Scorer — transhumanists/apis
Reads articles.json from scrapers/rss_fetcher.py.
For each article, calls an LLM (OpenAI or Anthropic) to extract structured milestone data.
Dynamically adds new categories/subcategories discovered by the LLM — no human intervention required.
Outputs milestones.json, events.json, and Milestones.md (auto-generated from JSON).
"""
import json
import logging
import math
import os
import pathlib
import re
import sys
import threading
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

import requests

try:
    from openai import OpenAI as _OpenAI_class
    OpenAI: Any = _OpenAI_class
except ImportError:
    OpenAI = None

try:
    import anthropic as _anthropic_module
    anthropic: Any = _anthropic_module
except ImportError:
    anthropic = None

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
IN_FILE = ROOT / "data" / "articles.json"
OUT_MILESTONES = ROOT / "data" / "milestones.json"
OUT_EVENTS = ROOT / "data" / "events.json"
OUT_MD = ROOT / "Milestones.md"
EXISTING = ROOT / "data" / "milestones_existing.json"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
LLM_ROUTER_ENABLED = os.environ.get("LLM_ROUTER_ENABLED", "true").lower() in ("1", "true", "yes")
MAX_TOKENS = 1024
MAX_ARTICLES_TO_SCORE = 45

# Router data files (vendored from neohiro/LLM). When LLM_ROUTER_ENABLED=true,
# the FreeModelsRouter picks the best available free model across all configured
# providers and cascades through them on failure.
ROUTER_DATA_DIR = ROOT / "data"
# Module-level reference to FreeModelsRouter so tests can patch it.
# Set on first successful _get_router() call; remains None if router is
# unavailable (e.g. vendored data missing).
FreeModelsRouter = None

CATEGORY_DEFAULTS: dict[str, dict[str, str]] = {
    "Biotechnology":    {"icon": "🧬", "color": "#00e676"},
    "Computing & AGI":  {"icon": "🧠", "color": "#448aff"},
    "Quantum Physics":  {"icon": "⚛️",  "color": "#b388ff"},
    "Energy":           {"icon": "⚡",   "color": "#ffd740"},
    "Cybersecurity":    {"icon": "🛡️", "color": "#ff5252"},
    "Spaceflight":      {"icon": "🚀",  "color": "#00d4ff"},
    "Defense":          {"icon": "🌍",  "color": "#ff9100"},
}

DEFAULT_SUBCATEGORIES: dict[str, list[str]] = {
    "Biotechnology":    ["gene_editing", "medical_implants", "microscopy", "macroscopy", "longevity",
                         "synthetic_biology", "neuroscience", "gene_therapy", "immunotherapy", "biosensors"],
    "Computing & AGI":  ["frontier_models", "agentic_ai", "gpu_efficiency", "benchmarks",
                          "time_to_train", "inference_cost", "code_generation", "math_reasoning", "multimodal"],
    "Quantum Physics":   ["qubit_count", "error_correction", "time_crystals", "quantum_supremacy",
                         "quantum_networking", "gate_fidelity", "coherence", "trapped_ions"],
    "Energy":           ["fusion", "solar_efficiency", "battery_density", "wind_capacity",
                         "storage", "hydrogen", "geothermal"],
    "Cybersecurity":    ["exploits", "mitigations", "encryption", "threat_intelligence",
                          "defense_scores", "zero_day", "ransomware", "supply_chain"],
    "Spaceflight":      ["launch", "payload", "deep_space", "hypersonic", "aeronautics",
                          "reusability", "constellation"],
    "Defense":           ["range", "radius", "fleet_movements", "defense_contracts",
                          "air_defense", "naval", "cyber_ops", "drone_swarm", "hypersonic_glide"],
}

KNOWN_GEOCODES: dict[str, dict[str, object]] = {
    "broad institute": {"lat": 42.3375, "lon": -71.1061, "name": "Cambridge, MA, USA"},
    "stanford":        {"lat": 37.4321, "lon": -122.1665, "name": "Stanford, CA, USA"},
    "nif":             {"lat": 37.6881, "lon": -121.7045, "name": "Livermore, CA, USA"},
    "nifs":            {"lat": 35.6762, "lon": 139.6503, "name": "Tokyo, Japan"},
    "eth":             {"lat": 47.3769, "lon": 8.5417, "name": "Zürich, Switzerland"},
    "ibm":             {"lat": 41.0323, "lon": -73.5543, "name": "Yorktown Heights, NY, USA"},
    "google quantum":   {"lat": 37.4219, "lon": -122.0840, "name": "Mountain View, CA, USA"},
    "deepmind":        {"lat": 51.5074, "lon": -0.1278, "name": "London, UK"},
    "openai":          {"lat": 37.7749, "lon": -122.4194, "name": "San Francisco, CA, USA"},
    "anthropic":       {"lat": 37.7749, "lon": -122.4194, "name": "San Francisco, CA, USA"},
    "spacex":          {"lat": 28.5728, "lon": -80.6490, "name": "Cape Canaveral, FL, USA"},
    "nasa":             {"lat": 28.5237, "lon": -80.6810, "name": "Kennedy Space Center, FL, USA"},
    "nato":             {"lat": 50.8609, "lon": 4.3676, "name": "Brussels, Belgium"},
    "cisa":             {"lat": 38.8951, "lon": -77.0364, "name": "Washington, DC, USA"},
    "usaf":             {"lat": 38.8951, "lon": -77.0364, "name": "Washington, DC, USA"},
    "rafael":           {"lat": 32.0853, "lon": 34.7818, "name": "Tel Aviv, Israel"},
    "almaz":            {"lat": 55.7558, "lon": 37.6173, "name": "Moscow, Russia"},
    "ipp":              {"lat": 54.0956, "lon": 13.4725, "name": "Greifswald, Germany"},
    "nrel":             {"lat": 39.7370, "lon": -105.1763, "name": "Golden, CO, USA"},
    "iter":             {"lat": 43.7050, "lon": 5.7650, "name": "Saint-Paul-lès-Durance, France"},
    "jaxa":             {"lat": 35.6762, "lon": 139.6503, "name": "Tokyo, Japan"},
    "cern":             {"lat": 46.2333, "lon": 6.0556, "name": "Geneva, Switzerland"},
    "oxford":           {"lat": 51.7520, "lon": -1.2577, "name": "Oxford, UK"},
    "mit":              {"lat": 42.3601, "lon": -71.0942, "name": "Cambridge, MA, USA"},
    "caltech":          {"lat": 34.1377, "lon": -118.1253, "name": "Pasadena, CA, USA"},
    "hzb":              {"lat": 51.2323, "lon": 13.6830, "name": "Berlin, Germany"},
    "csiro":            {"lat": -33.8688, "lon": 151.2093, "name": "Sydney, Australia"},
    "harvard":          {"lat": 42.3375, "lon": -71.1061, "name": "Cambridge, MA, USA"},
    "israel":           {"lat": 31.0461, "lon": 34.8516, "name": "Israel"},
    "isw":              {"lat": 38.8951, "lon": -77.0364, "name": "Washington, DC, USA"},
    "cia":              {"lat": 38.8951, "lon": -77.0364, "name": "Langley, VA, USA"},
    "mi6":              {"lat": 51.4880, "lon": -0.1605, "name": "London, UK"},
    "mossad":           {"lat": 31.9686, "lon": 35.5064, "name": "Tel Aviv, Israel"},
    "plassf":           {"lat": 39.9042, "lon": 116.4074, "name": "Beijing, China"},
    "alkermes":         {"lat": 42.3765, "lon": -71.2356, "name": "Waltham, MA, USA"},
    "moderna":          {"lat": 42.3644, "lon": -71.0876, "name": "Cambridge, MA, USA"},
    "fda":              {"lat": 39.0555, "lon": -77.0380, "name": "Silver Spring, MD, USA"},
    "nature biotechnology": {"lat": 32.0603, "lon": 118.7969, "name": "Nanjing, China"},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("score_milestone")

CATEGORIES: dict[str, dict[str, Any]] = {
    name: {
        "icon": CATEGORY_DEFAULTS.get(name, {}).get("icon", "📌"),
        "color": CATEGORY_DEFAULTS.get(name, {}).get("color", "#aaaaaa"),
        "subcategories": list(v),
    }
    for name, v in DEFAULT_SUBCATEGORIES.items()
}

DYNAMIC_SUBCATEGORIES: dict[str, list[str]] = {
    name: list(v) for name, v in DEFAULT_SUBCATEGORIES.items()
}

# The canonical mirror and the site spell three categories long ("Renewable
# Energy", "Spaceflight & Aeronautics", "Military & Defense"); freshly scored
# records carry the short LLM keys ("Energy", "Spaceflight", "Defense").
# Normalising between the two keeps the append-only merge keyed consistently -
# a mismatch here silently drops every record of those categories on the next
# pipeline run (observed 2026-09-22 while auditing the restored 40-record DB).
CATEGORY_KEY_TO_DISPLAY = {
    "Energy": "Renewable Energy",
    "Spaceflight": "Spaceflight & Aeronautics",
    "Defense": "Military & Defense",
}
CATEGORY_DISPLAY_TO_KEY = {v: k for k, v in CATEGORY_KEY_TO_DISPLAY.items()}


def normalize_category(cat: str) -> str:
    """Map a stored category to the LLM-side key it belongs under."""
    return CATEGORY_DISPLAY_TO_KEY.get(cat, cat)


def display_category(cat: str) -> str:
    """Long display name a category key publishes under in the canonical DB."""
    return CATEGORY_KEY_TO_DISPLAY.get(cat, cat)


def build_existing_by_subcat(raw_existing: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Flatten a canonical categories JSON into {Category/subcategory: [...]}.

    Normalises each record's stored category (which may be a long display name
    such as "Renewable Energy") back to the LLM-side key so the keys line up
    with freshly scored records and the append-only merge retains everything.
    """
    if not isinstance(raw_existing, dict):
        log.warning("Existing milestones is not a JSON object — ignoring (keys: %s)", type(raw_existing).__name__)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for _cat_name, cat_data in (raw_existing.get("categories") or {}).items():
        if not isinstance(cat_data, dict):
            continue
        for m in cat_data.get("milestones", []) or []:
            if not isinstance(m, dict):
                continue
            rec = dict(m)
            rec["category"] = normalize_category(rec.get("category") or "Unknown")
            sub = rec.get("subcategory") or "general"
            out.setdefault(f"{rec['category']}/{sub}", []).append(rec)
    return out


def _match_gazetteer(text: str) -> dict[str, Any] | None:
    """Best gazetteer entry whose key is a substring of ``text``.

    Longest key wins so a shorter key never shadows a longer one that shares it
    as a prefix (e.g. "nif" must not capture "nifs" - they are different labs
    on different continents).
    """
    blob = (text or "").lower()
    best: tuple[int, dict[str, Any]] | None = None
    for key, geo in KNOWN_GEOCODES.items():
        if key in blob:
            if best is None or len(key) > best[0]:
                best = (len(key), geo)
    return best[1] if best is not None else None


def get_geocode(source: str, location: str = "") -> dict[str, Any]:
    """Resolve a milestone to a map pin.

    Tries the curated gazetteer against the source name first, then against the
    LLM-reported location ("<city>, <country>") so every milestone gets a pin
    instead of falling back to lat/lon 0.0 (which drops it off the world map).
    """
    geo = _match_gazetteer(source)
    if geo is not None:
        return geo

    ref = (location or "").lower()
    for key, candidate in KNOWN_GEOCODES.items():
        city = str(candidate.get("name", "")).lower()
        city_name = city.split(",")[0].strip()
        if city_name and (city_name in ref or key in ref):
            return candidate
    return {"lat": 0.0, "lon": 0.0, "name": source or location or "Unknown"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


SYSTEM_PROMPT = """You are a senior science & technology analyst who extracts structured milestone data from news articles.

Given an article title and summary, decide if it represents a meaningful MILESTONE — a new record, breakthrough, or numerical achievement.

The following categories are KNOWN and have these subcategories:
- Biotechnology (gene_editing, medical_implants, microscopy, macroscopy, longevity, synthetic_biology, neuroscience, gene_therapy, immunotherapy, biosensors)
- Computing & AGI (frontier_models, agentic_ai, gpu_efficiency, benchmarks, time_to_train, inference_cost, code_generation, math_reasoning, multimodal)
- Quantum Physics (qubit_count, error_correction, time_crystals, quantum_supremacy, quantum_networking, gate_fidelity, coherence, trapped_ions)
- Energy (fusion, solar_efficiency, battery_density, wind_capacity, storage, hydrogen, geothermal)
- Cybersecurity (exploits, mitigations, encryption, threat_intelligence, defense_scores, zero_day, ransomware, supply_chain)
- Spaceflight (launch, payload, deep_space, hypersonic, aeronautics, reusability, constellation)
- Defense (range, radius, fleet_movements, defense_contracts, air_defense, naval, cyber_ops, drone_swarm, hypersonic_glide)

If the article fits NONE of the above categories, you MAY suggest a NEW category name and a single new subcategory for it, in snake_case.

Output JSON only:
{
  "is_milestone": true,
  "category": "<one of the 7 known names, or a NEW category name you are confident about (max 40 chars)>",
  "is_new_category": <true if this is a brand-new category not listed above>,
  "subcategory": "<snake_case, existing if possible, otherwise new>",
  "title": "<concise milestone title, max 80 chars>",
  "value": <number or null>,
  "unit": "<string or null>",
  "source": "<organisation/agency name>",
  "location": "<city, country> of the organisation or lab behind this milestone (required)",
  "date": "<YYYY-MM-DD or null>",
  "is_record": <true if it is a new all-time record>,
  "is_breakthrough": <true if it is a major qualitative leap>,
  "summary": "<one sentence, max 200 chars>"
}

If NOT a milestone, output: {"is_milestone": false}

Return ONLY the JSON object. No markdown fences."""


def call_llm_openai(title: str, summary: str) -> Any | None:
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    raw: str = ""
    try:
        client = _get_openai_client()
        if client is None:
            return None
        resp = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Title: {title}\n\nSummary: {summary[:1500]}"},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.IGNORECASE)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("LLM returned malformed JSON: %s — %s", e, raw[:200])
        return None
    except (requests.RequestException, ValueError, OSError) as e:
        log.warning("OpenAI call failed: %s", e)
        return None


def call_llm_anthropic(title: str, summary: str) -> Any | None:
    if not ANTHROPIC_API_KEY or anthropic is None:
        return None
    try:
        client = _get_anthropic_client()
        if client is None:
            return None
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=MAX_TOKENS,
            temperature=0.0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Title: {title}\n\nSummary: {summary[:1500]}"}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"```\s*$", "", text, flags=re.IGNORECASE)
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("Anthropic returned malformed JSON: %s", e)
        return None
    except (requests.RequestException, ValueError, OSError) as e:
        log.warning("Anthropic call failed: %s", e)
        return None


_OPENAI_CLIENT_SINGLETON: Any = None
_OPENAI_CLIENT_INIT_ERROR: Exception | None = None
_ANTHROPIC_CLIENT_SINGLETON: Any = None
_ANTHROPIC_CLIENT_INIT_ERROR: Exception | None = None


def _get_openai_client() -> Any:
    """Lazy-init OpenAI client singleton. Reused across all articles in a run."""
    global _OPENAI_CLIENT_SINGLETON, _OPENAI_CLIENT_INIT_ERROR
    if _OPENAI_CLIENT_SINGLETON is not None:
        return _OPENAI_CLIENT_SINGLETON
    if _OPENAI_CLIENT_INIT_ERROR is not None:
        return None
    try:
        _OPENAI_CLIENT_SINGLETON = OpenAI(api_key=OPENAI_API_KEY)
        return _OPENAI_CLIENT_SINGLETON
    except (requests.RequestException, ValueError, TypeError, OSError) as e:
        _OPENAI_CLIENT_INIT_ERROR = e
        log.warning("OpenAI client init failed: %s", e)
        return None


def _get_anthropic_client() -> Any:
    """Lazy-init Anthropic client singleton. Reused across all articles in a run."""
    global _ANTHROPIC_CLIENT_SINGLETON, _ANTHROPIC_CLIENT_INIT_ERROR
    if _ANTHROPIC_CLIENT_SINGLETON is not None:
        return _ANTHROPIC_CLIENT_SINGLETON
    if _ANTHROPIC_CLIENT_INIT_ERROR is not None:
        return None
    try:
        _ANTHROPIC_CLIENT_SINGLETON = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return _ANTHROPIC_CLIENT_SINGLETON
    except (requests.RequestException, ValueError, TypeError, OSError) as e:
        _ANTHROPIC_CLIENT_INIT_ERROR = e
        log.warning("Anthropic client init failed: %s", e)
        return None


def _reset_clients() -> None:
    """Test hook: clear cached clients so next call re-inits."""
    global _OPENAI_CLIENT_SINGLETON, _OPENAI_CLIENT_INIT_ERROR
    global _ANTHROPIC_CLIENT_SINGLETON, _ANTHROPIC_CLIENT_INIT_ERROR
    _OPENAI_CLIENT_SINGLETON = None
    _OPENAI_CLIENT_INIT_ERROR = None
    _ANTHROPIC_CLIENT_SINGLETON = None
    _ANTHROPIC_CLIENT_INIT_ERROR = None


_ROUTER_SINGLETON = None
_ROUTER_IMPORT_ERROR = None
_ROUTER_INIT_LOCK = threading.Lock()


def _ensure_no_bom(path: pathlib.Path) -> None:
    """Strip UTF-8 BOM from path if present.

    Vendored JSON files may arrive with BOM (e.g. when downloaded via
    gh api | Out-File on Windows). json.load() rejects BOM by default.
    Defensive: runs once per vendored file on first _get_router() call.
    """
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            path.write_bytes(raw[3:])
            log.debug("Stripped UTF-8 BOM from %s", path.name)
    except OSError:
        pass


def _get_router() -> Any:
    """Lazy-init FreeModelsRouter singleton.

Re-reading 4 JSON files per article (200 calls) is wasteful; the router
also throttles its router_state.json persistence (STATE_SAVE_EVERY) and is
flushed once at the end of the batch. Cache the router instance for the
lifetime of this process.

    Thread-safe: double-check locking pattern. Other threads block on the
    lock while the first thread completes init, then return the cached value.
    """
    global _ROUTER_SINGLETON, _ROUTER_IMPORT_ERROR, FreeModelsRouter
    if _ROUTER_SINGLETON is not None or _ROUTER_IMPORT_ERROR is not None:
        return _ROUTER_SINGLETON
    with _ROUTER_INIT_LOCK:
        if _ROUTER_SINGLETON is not None or _ROUTER_IMPORT_ERROR is not None:
            return _ROUTER_SINGLETON
        try:
            import sys as _sys
            _sys.path.insert(0, str(HERE))
            from router import FreeModelsRouter as _RouterClass
        except (ImportError, ModuleNotFoundError, SyntaxError) as e:
            _ROUTER_IMPORT_ERROR = e
            log.debug("Could not import FreeModelsRouter: %s", e)
            return None
        try:
            FreeModelsRouter = _RouterClass
            for _f in ("providers.json", "models.json", "free_models.json", "unlimited.json"):
                _ensure_no_bom(ROUTER_DATA_DIR / _f)
            _ROUTER_SINGLETON = _RouterClass(
                providers_json=str(ROUTER_DATA_DIR / "providers.json"),
                models_json=str(ROUTER_DATA_DIR / "models.json"),
                free_json=str(ROUTER_DATA_DIR / "free_models.json"),
                unlimited_json=str(ROUTER_DATA_DIR / "unlimited.json"),
                state_path=str(ROUTER_DATA_DIR / "router_state.json"),
            )
        except (ValueError, TypeError, OSError) as e:
            log.warning("FreeModelsRouter init failed: %s", e)
            return None
        return _ROUTER_SINGLETON


def _reset_router() -> None:
    """Test hook: clear the cached router and LLM clients so next call re-inits."""
    global _ROUTER_SINGLETON, _ROUTER_IMPORT_ERROR, FreeModelsRouter
    global _OPENAI_CLIENT_SINGLETON, _OPENAI_CLIENT_INIT_ERROR
    global _ANTHROPIC_CLIENT_SINGLETON, _ANTHROPIC_CLIENT_INIT_ERROR
    _ROUTER_SINGLETON = None
    _ROUTER_IMPORT_ERROR = None
    FreeModelsRouter = None
    _OPENAI_CLIENT_SINGLETON = None
    _OPENAI_CLIENT_INIT_ERROR = None
    _ANTHROPIC_CLIENT_SINGLETON = None
    _ANTHROPIC_CLIENT_INIT_ERROR = None


def call_llm_router(title: str, summary: str) -> Any | None:
    router = _get_router()
    if router is None:
        return None

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Title: {title}\n\nSummary: {summary[:1500]}"},
    ]

    result = router.chat(
        messages=messages,
        preferred_tier="free",
        task="classify",
        max_tokens=MAX_TOKENS,
    )

    if result.error:
        log.warning("Router returned error after %d attempts: %s", result.attempts, result.error)
        return None

    raw = result.content.strip()
    raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.IGNORECASE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Router returned malformed JSON (model=%s, provider=%s): %s — %s",
                    result.model, result.provider, e, raw[:200])
        return None


def call_llm(title: str, summary: str) -> Any | None:
    if LLM_ROUTER_ENABLED:
        result = call_llm_router(title, summary)
        if result:
            return result
        log.debug("Router returned no result — falling back to OpenAI")
    result = call_llm_openai(title, summary)
    if result:
        return result
    return call_llm_anthropic(title, summary)


_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def _strip_thousands(value: str) -> str:
    """Convert an en-US thousands-separated literal ("10,000") to plain digits.

    LLMs routinely emit "10,000" even though the schema asks for a bare number;
    without this, ``float()`` rejects it and a genuine record silently deflates
    to 0.0 in ranking. Only the canonical ``digits(,digits{3})`` shape is
    accepted, so European decimals ("0,5") and stray commas fail closed (→ 0.0).
    """
    if _THOUSANDS_RE.fullmatch(value):
        return value.replace(",", "")
    return value


def normalize_value(value: Any, unit: str | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = _strip_thousands(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    # NaN/Infinity must never enter the rank pipeline: an LLM value string like
    # "nan" or "1e400" would otherwise produce a NaN rank, and any comparison
    # with NaN is False — silently destabilising sort order and the
    # is_new/supersede logic in merge_with_existing.
    if not math.isfinite(v):
        return 0.0
    if not unit:
        return v
    u = unit.lower()
    if "%" in u:
        return v
    if "q" in u and "=" not in u and len(u) <= 2:
        return v
    if "mach" in u or "speed" in u:
        return v * 5
    if "km" in u:
        return min(v, 20000) / 200
    if "ton" in u:
        return v * 0.5
    if "qubit" in u:
        return v / 50
    if "wh/kg" in u or "wh/l" in u:
        return v / 6
    return v


def _stable_id(m: dict[str, Any]) -> str:
    """Reproducible id for a milestone record that lacks one (defensive)."""
    return "ms-" + sha1(
        f"{m.get('category', 'Unknown')}{m.get('subcategory', 'general')}"
        f"{m.get('title', '')}{m.get('date', '')}".encode()
    ).hexdigest()[:12]


def milestone_sort_key(milestone: dict[str, Any]) -> tuple[float, float]:
    """Chronological-first ordering key for a category's milestone list.

    Newest milestones lead; older (e.g. arXiv-backfilled) records settle further
    down the list at their historical position instead of floating by rank.
    Rank breaks ties so equal-dated records still order deterministically.
    """
    date_str = milestone.get("date") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        parsed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        parsed = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (parsed.timestamp(), rank_milestone(milestone))


def rank_milestone(milestone: dict[str, Any]) -> float:
    score = 0.0
    if milestone.get("is_record"):
        score += 100
    if milestone.get("is_breakthrough"):
        score += 50
    if milestone.get("value") is not None:
        score += normalize_value(milestone.get("value"), milestone.get("unit"))
    date_str = milestone.get("date")
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            days_old = (datetime.now(timezone.utc) - d.replace(tzinfo=timezone.utc)).days
            score += max(0, 30 - days_old) * 0.5
        except ValueError:
            pass
    return score


def ensure_category(category: str, color: str) -> None:
    if category not in CATEGORIES:
        log.info("Auto-discovered new category: %s — adding to tracker", category)
        CATEGORIES[category] = {
            "icon": "📌",
            "color": color,
            "subcategories": [],
        }
        DYNAMIC_SUBCATEGORIES[category] = []


def ensure_subcategory(category: str, subcategory: str) -> None:
    if category not in DYNAMIC_SUBCATEGORIES:
        DYNAMIC_SUBCATEGORIES[category] = []
    if subcategory not in DYNAMIC_SUBCATEGORIES[category]:
        log.info("Auto-discovered new subcategory: %s/%s — adding", category, subcategory)
        DYNAMIC_SUBCATEGORIES[category].append(subcategory)
    cat_entry: dict[str, Any] | None = CATEGORIES.get(category)
    if cat_entry is not None and subcategory not in cat_entry.get("subcategories", []):
        subs: list[str] = cat_entry["subcategories"]
        subs.append(subcategory)


def score_article(article: dict[str, Any]) -> dict[str, Any] | None:
    if not LLM_ROUTER_ENABLED and not (OPENAI_API_KEY or ANTHROPIC_API_KEY):
        log.error("No LLM API key set — set OPENAI_API_KEY or ANTHROPIC_API_KEY (or enable LLM_ROUTER_ENABLED=true with at least one free provider key)")
        sys.exit(1)

    title = article.get("title") or ""
    summary = article.get("summary") or ""
    if not title:
        return None

    result = call_llm(title, summary)
    if not result or not result.get("is_milestone"):
        return None

    category = result.get("category", "")
    subcategory = result.get("subcategory", "general")
    is_new_cat = result.get("is_new_category", False)

    if is_new_cat and category:
        color = "#aaaaaa"
        ensure_category(category, color)

    if category and subcategory:
        ensure_subcategory(category, subcategory)
    elif category:
        ensure_subcategory(category, "general")

    source = result.get("source") or article.get("source", "Unknown")
    date = result.get("date") or article.get("published") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    geo = get_geocode(source, result.get("location", ""))

    safe_cat = category or "Unknown"
    safe_sub = subcategory or "general"
    mid = "ms-" + sha1(f"{safe_cat}{safe_sub}{title}{date}".encode()).hexdigest()[:12]

    return {
        "id": mid,
        "title": (result.get("title") or title)[:200],
        "summary": (result.get("summary") or summary[:200])[:200],
        "category": safe_cat,
        "subcategory": safe_sub,
        "value": result.get("value"),
        "unit": result.get("unit"),
        "source": source,
        "date": date,
        "url": article.get("url"),
        "is_record": result.get("is_record", False),
        "is_breakthrough": result.get("is_breakthrough", False),
        "is_new": True,
        "geolocation": {"lat": geo["lat"], "lon": geo["lon"]},
    }


def build_categories_output() -> dict[str, Any]:
    output_categories: dict[str, Any] = {}
    for cat_name, cat_data in CATEGORIES.items():
        subcats = list(DYNAMIC_SUBCATEGORIES.get(cat_name, cat_data.get("subcategories", [])))
        output_categories[cat_name] = {
            "name": display_category(cat_name),
            "icon": cat_data.get("icon", "📌"),
            "color": cat_data.get("color", "#aaaaaa"),
            "subcategories": subcats,
            "milestones": [],
        }
    return output_categories


def generate_milestones_md(categories: dict[str, Any]) -> str:
    now = _utc_now()
    lines = [
        "# Human Progress Milestones",
        "",
        "> **Live dashboard:** [transhumanists.github.io](https://transhumanists.github.io) · **API engine:** [transhumanists/apis](https://github.com/transhumanists/apis)",
        "",
        f"*Auto-generated: {now} · {sum(len(c.get('milestones', [])) for c in categories.values())} active milestones across {len(categories)} categories*",
        "",
        "---",
        "",
    ]

    for i, (cat_name, cat_data) in enumerate(categories.items(), 1):
        subcats = cat_data.get("subcategories", [])
        icon = cat_data.get("icon", "📌")
        color = cat_data.get("color", "#aaaaaa")
        milestones = cat_data.get("milestones", [])

        lines.extend([
            f"## {i}. {cat_data.get('name') or cat_name} {icon}",
            "",
            f"*Color: {color} · Subcategories: {len(subcats)}*",
            "",
            "| # | Subcategory | Milestone | Value | Source | Date |",
            "|:---|:------------|:-----------|:------|:-------|:-----|",
        ])

        for j, m in enumerate(milestones, 1):
            val = '—' if m.get("value") is None else f"**{m['value']}** {m.get('unit', '')}".strip()
            title = m.get("title", "—")[:50]
            source = m.get("source", "—")[:20]
            date = m.get("date", "—")
            sub = m.get("subcategory", "general")
            new_marker = "🆕" if m.get("is_new") else ""
            lines.append(f"| {j} | `{sub}` | {title} {new_marker} | {val} | {source} | {date} |")

        lines.append("")

    lines.extend([
        "---",
        f"*Last auto-generated: {now} · Pipeline: `transhumanists/apis`*",
    ])
    return "\n".join(lines) + "\n"


def event_value(m: dict[str, Any]) -> str:
    """Value string for an event/map pin.

    Milestones without a numeric metric publish their title (never a summary
    string presented as if it were a metric value).
    """
    if m.get("value") is not None:
        return f"{m.get('value')} {m.get('unit') or ''}".strip()
    return m.get("title") or ""


def merge_with_existing(existing_by_subcat: dict[str, list[dict[str, Any]]],
                        by_subcat: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merge freshly scored milestones with everything previously published.

    The published database is APPEND-ONLY: records are never dropped, only
    superseded id-for-id by a strictly-fresher/better record of the same
    metric. A run that scores a handful of articles must never collapse the
    dataset (regression seen 2026-09-22: 37 -> 4 -> 3 milestones).

    Returns a dict of "Category/subcategory" -> list of milestone records.
    """
    merged: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for _key, records in existing_by_subcat.items():
        for m in records:
            mid = m.get("id") or _stable_id(m)
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            rec = dict(m)
            rec["is_new"] = False
            merged.setdefault(f"{rec.get('category', 'Unknown')}/{rec.get('subcategory', 'general')}", []).append(rec)
    for key, m in by_subcat.items():
        prev = existing_by_subcat.get(key, [])
        best_prev = max((rank_milestone(p) for p in prev), default=0.0)
        fresh = dict(m)
        fresh["is_new"] = rank_milestone(fresh) >= best_prev
        mid = fresh.get("id") or _stable_id(fresh)
        bucket = merged.setdefault(key, [])
        replaced = False
        for i, rec in enumerate(bucket):
            if (rec.get("id") or _stable_id(rec)) == mid:
                bucket[i] = fresh
                replaced = True
                break
        if not replaced:
            seen_ids.add(mid)
            bucket.append(fresh)
    return merged


def main() -> None:
    if not IN_FILE.exists():
        log.error("Missing input file: %s", IN_FILE)
        sys.exit(1)

    try:
        raw = json.loads(IN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("Corrupt articles.json: %s", e)
        sys.exit(1)

    articles = raw.get("articles", []) if isinstance(raw, dict) else raw
    log.info("Loaded %d articles", len(articles))

    existing_by_subcat: dict[str, list[dict[str, Any]]] = {}
    if EXISTING.exists():
        try:
            existing = json.loads(EXISTING.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                # Flatten + normalise display-name categories back to LLM-side
                # keys (see CATEGORY_DISPLAY_TO_KEY) so the append-only merge
                # retains every previously published record instead of dropping
                # anything keyed under a long display name.
                existing_by_subcat = build_existing_by_subcat(existing)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("Could not load existing milestones: %s", e)

    articles_sorted = sorted(articles, key=lambda a: -a.get("weight", 0))
    to_score = articles_sorted[:MAX_ARTICLES_TO_SCORE]
    log.info("Scoring top %d articles (router=%s)", len(to_score), LLM_ROUTER_ENABLED)

    all_milestones: list[dict[str, Any]] = []
    for i, article in enumerate(to_score):
        if i % 20 == 0:
            log.info("Scoring %d/%d...", i, len(to_score))
        m = score_article(article)
        if m:
            all_milestones.append(m)

    log.info("Found %d candidate milestones across %d categories", len(all_milestones), len(CATEGORIES))

    router = _get_router()
    if router is not None:
        router.flush_state()

    by_subcat: dict[str, Any] = {}
    for m in all_milestones:
        key = f"{m['category']}/{m['subcategory']}"
        if key not in by_subcat or rank_milestone(m) > rank_milestone(by_subcat[key]):
            by_subcat[key] = m

    output_categories = build_categories_output()
    events: list[dict[str, Any]] = []
    seen_events: set[str] = set()

    # ---- Retention merge ------------------------------------------------
    # The published database is APPEND-ONLY (see merge_with_existing).
    merged = merge_with_existing(existing_by_subcat, by_subcat)

    # ---- Distribute merged records into their categories ----------------
    for key, records in merged.items():
        display, sub = key.split("/", 1)
        cat_data = output_categories.get(display) or output_categories.get(normalize_category(display))
        if cat_data is None:
            log.warning("Retained milestone(s) for unknown category %r — skipped", display)
            continue
        if sub not in cat_data["subcategories"]:
            cat_data["subcategories"].append(sub)
        for m in records:
            m["category"] = display_category(m["category"])
            cat_data["milestones"].append(m)
            geo = m.get("geolocation") or {}
            if geo.get("lat") and geo.get("lon"):
                ev_id = "ev-" + (m.get("id") or _stable_id(m))
                if ev_id in seen_events:
                    continue
                seen_events.add(ev_id)
                events.append({
                    "id": ev_id,
                    "title": m["title"],
                    "category": m["category"],
                    "value": event_value(m),
                    "source": m["source"],
                    "url": m.get("url"),
                    "date": m["date"],
                    "geolocation": geo,
                })

    for cat_name in output_categories:
        # Newest-first chronological per category; backfilled older milestones
        # therefore appear further down the list at their true historical slot.
        output_categories[cat_name]["milestones"].sort(key=milestone_sort_key, reverse=True)

    now = _utc_now()
    output = {
        "last_update": now,
        "version": "2.0.0",
        "categories": output_categories,
    }
    events_out = {
        "last_update": now,
        "version": "2.0.0",
        "events": events,
    }

    OUT_MILESTONES.parent.mkdir(parents=True, exist_ok=True)
    OUT_MILESTONES.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    OUT_EVENTS.write_text(json.dumps(events_out, indent=2, ensure_ascii=False), encoding="utf-8")

    md_content = generate_milestones_md(output_categories)
    OUT_MD.write_text(md_content, encoding="utf-8")

    # Persist the merged dataset so a later local/dry run retains it even if
    # the upstream fetch of milestones_existing.json is unavailable.
    try:
        EXISTING.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not persist existing-milestones snapshot: %s", e)

    log.info(
        "Wrote %d milestones across %d categories and %d events",
        sum(len(c["milestones"]) for c in output_categories.values()),
        len(output_categories),
        len(events),
    )


if __name__ == "__main__":
    main()
