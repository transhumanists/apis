#!/usr/bin/env python3
"""
LLM Milestone Scorer
Reads articles.json from scrapers/rss_fetcher.py
For each article, calls an LLM (OpenAI, Anthropic, or local) to extract structured milestone data.
Outputs milestones.json (top milestones per category) and events.json (geo-pinned events).
"""
import json, os, sys, re, pathlib, datetime as dt, hashlib, logging
from datetime import timezone
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
try:
    import anthropic
except ImportError:
    anthropic = None

# ---- Config ----
HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
IN_FILE = ROOT / "data" / "articles.json"
OUT_MILESTONES = ROOT / "data" / "milestones.json"
OUT_EVENTS = ROOT / "data" / "events.json"
EXISTING = ROOT / "data" / "milestones_existing.json"  # previous run, for delta

LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MAX_TOKENS = 1024
TEMPERATURE = 0.0

# ---- Logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("score_milestone")

# ---- Categories ----
CATEGORIES = {
    "Biotechnology": {
        "icon": "🧬", "color": "#00e676",
        "subcategories": ["gene_editing", "medical_implants", "microscopy", "macroscopy",
                          "longevity", "synthetic_biology", "neuroscience", "gene_therapy",
                          "immunotherapy", "biosensors"],
    },
    "Computing & AGI": {
        "icon": "🧠", "color": "#448aff",
        "subcategories": ["frontier_models", "agentic_ai", "gpu_efficiency", "benchmarks",
                          "time_to_train", "inference_cost", "code_generation", "math_reasoning"],
    },
    "Quantum Physics": {
        "icon": "⚛️", "color": "#b388ff",
        "subcategories": ["qubit_count", "error_correction", "time_crystals", "quantum_supremacy",
                          "quantum_networking", "gate_fidelity", "coherence", "trapped_ions"],
    },
    "Energy": {
        "icon": "⚡", "color": "#ffd740",
        "subcategories": ["fusion", "solar_efficiency", "battery_density", "wind_capacity",
                          "storage", "hydrogen", "geothermal"],
    },
    "Cybersecurity": {
        "icon": "🛡️", "color": "#ff5252",
        "subcategories": ["exploits", "mitigations", "encryption", "threat_intelligence",
                          "defense_scores", "zero_day", "ransomware", "supply_chain"],
    },
    "Spaceflight": {
        "icon": "🚀", "color": "#00d4ff",
        "subcategories": ["launch", "payload", "deep_space", "hypersonic", "aeronautics",
                          "reusability", "constellation"],
    },
    "Defense": {
        "icon": "🌍", "color": "#ff9100",
        "subcategories": ["range", "radius", "fleet_movements", "defense_contracts",
                          "air_defense", "naval", "cyber_ops", "drone_swarm", "hypersonic_glide"],
    },
}

# ---- Geocoding heuristic for common sources ----
KNOWN_GEOCODES: dict[str, dict] = {
    "broad institute": {"lat": 42.3375, "lon": -71.1061, "name": "Cambridge, MA, USA"},
    "stanford": {"lat": 37.4321, "lon": -122.1665, "name": "Stanford, CA, USA"},
    "nif": {"lat": 37.6881, "lon": -121.7045, "name": "Livermore, CA, USA"},
    "nifs": {"lat": 35.6762, "lon": 139.6503, "name": "Tokyo, Japan"},
    "eth": {"lat": 47.3769, "lon": 8.5417, "name": "Zürich, Switzerland"},
    "ibm": {"lat": 41.0323, "lon": -73.5543, "name": "Yorktown Heights, NY, USA"},
    "google quantum": {"lat": 37.4219, "lon": -122.0840, "name": "Mountain View, CA, USA"},
    "deepmind": {"lat": 51.5074, "lon": -0.1278, "name": "London, UK"},
    "openai": {"lat": 37.7749, "lon": -122.4194, "name": "San Francisco, CA, USA"},
    "anthropic": {"lat": 37.7749, "lon": -122.4194, "name": "San Francisco, CA, USA"},
    "spacex": {"lat": 28.5728, "lon": -80.6490, "name": "Cape Canaveral, FL, USA"},
    "nasa": {"lat": 28.5237, "lon": -80.6810, "name": "Kennedy Space Center, FL, USA"},
    "nato": {"lat": 50.8609, "lon": 4.3676, "name": "Brussels, Belgium"},
    "cisa": {"lat": 38.8951, "lon": -77.0364, "name": "Washington, DC, USA"},
    "usaf": {"lat": 38.8951, "lon": -77.0364, "name": "Washington, DC, USA"},
    "rafael": {"lat": 32.0853, "lon": 34.7818, "name": "Tel Aviv, Israel"},
    "almaz": {"lat": 55.7558, "lon": 37.6173, "name": "Moscow, Russia"},
    "ipp": {"lat": 54.0956, "lon": 13.4725, "name": "Greifswald, Germany"},
    "nrel": {"lat": 39.7370, "lon": -105.1763, "name": "Golden, CO, USA"},
    "iter": {"lat": 43.7050, "lon": 5.7650, "name": "Saint-Paul-lès-Durance, France"},
    "jaxa": {"lat": 35.6762, "lon": 139.6503, "name": "Tokyo, Japan"},
    "cern": {"lat": 46.2333, "lon": 6.0556, "name": "Geneva, Switzerland"},
    "oxford": {"lat": 51.7520, "lon": -1.2577, "name": "Oxford, UK"},
    "mit": {"lat": 42.3601, "lon": -71.0942, "name": "Cambridge, MA, USA"},
    "caltech": {"lat": 34.1377, "lon": -118.1253, "name": "Pasadena, CA, USA"},
    "hzb": {"lat": 51.2323, "lon": 13.6830, "name": "Berlin, Germany"},
    "siemens": {"lat": 48.1351, "lon": 11.5820, "name": "Munich, Germany"},
    "csiro": {"lat": -33.8688, "lon": 151.2093, "name": "Sydney, Australia"},
    "harvard": {"lat": 42.3375, "lon": -71.1061, "name": "Cambridge, MA, USA"},
    "yale": {"lat": 41.3163, "lon": -72.9223, "name": "New Haven, CT, USA"},
    "israel": {"lat": 31.0461, "lon": 34.8516, "name": "Israel"},
    "isw": {"lat": 38.8951, "lon": -77.0364, "name": "Washington, DC, USA"},
    "cia": {"lat": 38.8951, "lon": -77.0364, "name": "Langley, VA, USA"},
    "mi6": {"lat": 51.4880, "lon": -0.1605, "name": "London, UK"},
    "mossad": {"lat": 31.9686, "lon": 35.5064, "name": "Tel Aviv, Israel"},
    "plassf": {"lat": 39.9042, "lon": 116.4074, "name": "Beijing, China"},
}


def get_geocode(source: str) -> dict:
    s = (source or "").lower()
    for key, geo in KNOWN_GEOCODES.items():
        if key in s:
            return geo
    return {"lat": 0.0, "lon": 0.0, "name": source or "Unknown"}


# ---- LLM call ----
SYSTEM_PROMPT = """You are a senior science & technology analyst who extracts structured milestone data from news articles.

Given an article title and summary, decide:
1. Whether it represents a meaningful MILESTONE — a new record, breakthrough, or numerical achievement in one of these categories:
   - Biotechnology (gene editing, implants, microscopy, longevity, synthetic biology, neuroscience, immunotherapy, biosensors)
   - Computing & AGI (frontier models, agentic AI, GPU efficiency, benchmarks, time-to-train)
   - Quantum Physics (qubit count, error correction, time crystals, supremacy, networking)
   - Energy (fusion, solar efficiency, battery density, wind, storage, hydrogen, geothermal)
   - Cybersecurity (exploits, mitigations, encryption, threat intel, defense scores, zero-days, ransomware, supply chain)
   - Spaceflight (launch, payload, deep space, hypersonic, reusability, constellations)
   - Defense (range, radius, fleet movements, contracts, air defense, naval, cyber ops, drone swarms)

2. If yes, output a JSON object with this exact schema:
{
  "is_milestone": true,
  "category": "<one of the 7 above>",
  "subcategory": "<snake_case one of: see list>",
  "title": "<concise milestone title, max 80 chars>",
  "value": <number or null>,
  "unit": "<string or null>",
  "source": "<organisation/agency name>",
  "date": "<YYYY-MM-DD or null if unknown>",
  "is_record": <true if it's a new all-time record in the field, false otherwise>,
  "is_breakthrough": <true if it's a major qualitative leap, false if incremental>,
  "summary": "<one sentence, max 200 chars>"
}

3. If the article is NOT a milestone, output:
{"is_milestone": false}

Be strict. Marketing, opinion, and policy articles are not milestones. A new model release without benchmark numbers is not a milestone.

Return ONLY the JSON object, no commentary, no markdown fences."""


def call_llm_openai(title: str, summary: str) -> Optional[dict]:
    if not OPENAI_API_KEY:
        return None
    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL if "gpt" in LLM_MODEL else "gpt-4o",
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Title: {title}\n\nSummary: {summary[:1500]}"},
            ],
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        log.warning("OpenAI call failed: %s", e)
        return None


def call_llm_anthropic(title: str, summary: str) -> Optional[dict]:
    if not ANTHROPIC_API_KEY:
        return None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=LLM_MODEL if "claude" in LLM_MODEL else "claude-sonnet-4-5",
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"Title: {title}\n\nSummary: {summary[:1500]}"},
            ],
        )
        text = resp.content[0].text
        # Strip code fences if present
        text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
        return json.loads(text)
    except Exception as e:
        log.warning("Anthropic call failed: %s", e)
        return None


def call_llm(title: str, summary: str) -> Optional[dict]:
    if "claude" in LLM_MODEL.lower() or "anthropic" in LLM_MODEL.lower():
        return call_llm_anthropic(title, summary) or call_llm_openai(title, summary)
    return call_llm_openai(title, summary)


# ---- Scoring helpers ----
def normalize_value(value, unit) -> tuple[float, float]:
    """Returns (numeric_score, subcategory_rank_estimate)."""
    if value is None:
        return (0.0, 0.0)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if isinstance(unit, str):
        u = unit.lower()
        if "%" in u:
            return (v, v)
        if "q" == u.strip().lower() or "q=" in u:
            return (v, v)
        if any(x in u for x in ["mach", "speed"]):
            return (v * 5, v)
        if "km" in u:
            return (min(v, 20000) / 200, v)
        if any(x in u for x in ["w", "watts", "kw", "mw", "gw"]):
            return (v, v)
        if any(x in u for x in ["wh/kg", "wh/k", "wh"]):
            return (v / 6, v)
        if "ton" in u:
            return (v * 0.5, v)
        if "ms" in u or "millisec" in u:
            return (v, v)
        if "qubit" in u:
            return (v / 50, v)
        if "neuron" in u:
            return (v, v)
    return (v, v)


def rank_milestone(milestone: dict) -> float:
    """Higher rank = more important."""
    score = 0.0
    if milestone.get("is_record"):
        score += 100
    if milestone.get("is_breakthrough"):
        score += 50
    if milestone.get("value") is not None:
        s, _ = normalize_value(milestone.get("value"), milestone.get("unit"))
        score += s
    # Recency bonus
    date_str = milestone.get("date")
    if date_str:
        try:
            d = dt.datetime.strptime(date_str, "%Y-%m-%d")
            days_old = (dt.datetime.now(timezone.utc) - d.replace(tzinfo=timezone.utc)).days
            score += max(0, 30 - days_old) * 0.5
        except ValueError:
            pass
    return score


# ---- Process a single article ----
def score_article(article: dict) -> Optional[dict]:
    if not (OPENAI_API_KEY or ANTHROPIC_API_KEY):
        log.error("No LLM API key set. Set OPENAI_API_KEY or ANTHROPIC_API_KEY.")
        sys.exit(1)

    title = article.get("title", "")
    summary = article.get("summary", "")
    if not title:
        return None

    result = call_llm(title, summary)
    if not result or not result.get("is_milestone"):
        return None

    # Validate category
    category = result.get("category")
    if category not in CATEGORIES:
        log.debug("Unknown category '%s' in article '%s'", category, title)
        return None

    sub = result.get("subcategory")
    if sub not in CATEGORIES[category]["subcategories"]:
        sub = CATEGORIES[category]["subcategories"][0]

    source = result.get("source") or article.get("source", "Unknown")
    date = result.get("date") or article.get("published", dt.datetime.utcnow().strftime("%Y-%m-%d"))
    geo = get_geocode(source)

    return {
        "id": "ms-" + hashlib.sha1(
            f"{category}{sub}{title}{date}".encode()
        ).hexdigest()[:12],
        "title": result.get("title", title)[:200],
        "summary": result.get("summary", summary[:200])[:200],
        "category": category,
        "subcategory": sub,
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


# ---- Main ----
def main():
    if not IN_FILE.exists():
        log.error("Missing input file: %s", IN_FILE)
        sys.exit(1)
    articles = json.loads(IN_FILE.read_text())
    if isinstance(articles, dict):
        articles = articles.get("articles", [])
    log.info("Loaded %d articles", len(articles))

    # Load existing milestones for delta
    existing_by_subcat: dict = {}
    if EXISTING.exists():
        try:
            existing = json.loads(EXISTING.read_text())
            for cat_name, cat in existing.get("categories", {}).items():
                for sub in cat.get("subcategories", []):
                    existing_by_subcat[f"{cat_name}/{sub}"] = cat.get("milestones", [])
        except Exception as e:
            log.warning("Could not load existing milestones: %s", e)

    # Score articles (high-weight first)
    articles_sorted = sorted(articles, key=lambda a: -a.get("weight", 0))
    log.info("Scoring top %d articles with %s", min(200, len(articles_sorted)), LLM_MODEL)

    all_milestones: list[dict] = []
    for i, article in enumerate(articles_sorted[:200]):
        if i % 10 == 0:
            log.info("Scoring %d/%d...", i, min(200, len(articles_sorted)))
        m = score_article(article)
        if m:
            all_milestones.append(m)
        # Rate limiting
        if i % 50 == 0 and i > 0:
            import time
            time.sleep(1)

    log.info("Found %d candidate milestones", len(all_milestones))

    # Group by (category, subcategory) — keep top 1 by rank
    by_subcat: dict = {}
    for m in all_milestones:
        key = f"{m['category']}/{m['subcategory']}"
        if key not in by_subcat or rank_milestone(m) > rank_milestone(by_subcat[key]):
            by_subcat[key] = m

    # Build output schema
    output = {
        "last_update": dt.datetime.utcnow().isoformat() + "Z",
        "version": "1.0.0",
        "categories": {},
    }
    events = []
    for cat_name, cat_def in CATEGORIES.items():
        output["categories"][cat_name] = {
            "name": cat_name,
            "icon": cat_def["icon"],
            "color": cat_def["color"],
            "subcategories": cat_def["subcategories"],
            "milestones": [],
        }
        for sub in cat_def["subcategories"]:
            key = f"{cat_name}/{sub}"
            if key in by_subcat:
                m = by_subcat[key]
                # Mark as new if no existing record or value is higher
                prev = existing_by_subcat.get(key, [])
                if prev:
                    best_prev = max((rank_milestone(p) for p in prev), default=0)
                    m["is_new"] = rank_milestone(m) > best_prev
                output["categories"][cat_name]["milestones"].append(m)
                # Generate event with geolocation
                if m.get("geolocation", {}).get("lat"):
                    events.append({
                        "id": "ev-" + m["id"],
                        "title": m["title"],
                        "category": m["category"],
                        "value": f"{m.get('value', '')} {m.get('unit', '') or ''}".strip(),
                        "source": m["source"],
                        "url": m["url"],
                        "date": m["date"],
                        "geolocation": m["geolocation"],
                    })

    # Sort milestones within each category by rank desc
    for cat_name in output["categories"]:
        output["categories"][cat_name]["milestones"].sort(
            key=lambda m: -rank_milestone(m)
        )

    OUT_MILESTONES.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    OUT_EVENTS.write_text(json.dumps({
        "last_update": dt.datetime.utcnow().isoformat() + "Z",
        "version": "1.0.0",
        "events": events,
    }, indent=2, ensure_ascii=False))
    log.info("Wrote %d milestones across %d categories and %d events",
             sum(len(c["milestones"]) for c in output["categories"].values()),
             len(output["categories"]), len(events))


if __name__ == "__main__":
    main()
