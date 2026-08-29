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
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from hashlib import sha1
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import anthropic
except ImportError:
    anthropic = None

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
IN_FILE = ROOT / "data" / "articles.json"
OUT_MILESTONES = ROOT / "data" / "milestones.json"
OUT_EVENTS = ROOT / "data" / "events.json"
OUT_MD = ROOT / "Milestones.md"
EXISTING = ROOT / "data" / "milestones_existing.json"

LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MAX_TOKENS = 1024
TEMPERATURE = 0.0
MAX_ARTICLES_TO_SCORE = 200
RATE_LIMIT_DELAY_ARTICLES = 50

CATEGORY_DEFAULTS: dict[str, dict] = {
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

KNOWN_GEOCODES: dict[str, dict] = {
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
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("score_milestone")

CATEGORIES: dict[str, dict] = {
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


def get_geocode(source: str) -> dict:
    s = (source or "").lower()
    for key, geo in KNOWN_GEOCODES.items():
        if key in s:
            return geo
    return {"lat": 0.0, "lon": 0.0, "name": source or "Unknown"}


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
  "date": "<YYYY-MM-DD or null>",
  "is_record": <true if it is a new all-time record>,
  "is_breakthrough": <true if it is a major qualitative leap>,
  "summary": "<one sentence, max 200 chars>"
}

If NOT a milestone, output: {"is_milestone": false}

Return ONLY the JSON object. No markdown fences."""


def call_llm_openai(title: str, summary: str) -> Optional[dict]:
    if not OPENAI_API_KEY or not OpenAI:
        return None
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        model = "gpt-4o" if "gpt" in LLM_MODEL.lower() else LLM_MODEL
        resp = client.chat.completions.create(
            model=model,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Title: {title}\n\nSummary: {summary[:1500]}"},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```json\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.IGNORECASE)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("LLM returned malformed JSON: %s — %s", e, raw[:200] if 'raw' in dir() else 'N/A')
        return None
    except Exception as e:
        log.warning("OpenAI call failed: %s", e)
        return None


def call_llm_anthropic(title: str, summary: str) -> Optional[dict]:
    if not ANTHROPIC_API_KEY or not anthropic:
        return None
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        model = LLM_MODEL if "claude" in LLM_MODEL.lower() else "claude-sonnet-4-20250514"
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
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
    except Exception as e:
        log.warning("Anthropic call failed: %s", e)
        return None


def call_llm(title: str, summary: str) -> Optional[dict]:
    result = call_llm_openai(title, summary)
    if result:
        return result
    return call_llm_anthropic(title, summary)


def normalize_value(value, unit: Optional[str]) -> float:
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
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


def rank_milestone(milestone: dict) -> float:
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
    if category in CATEGORIES and subcategory not in CATEGORIES[category]["subcategories"]:
        CATEGORIES[category]["subcategories"].append(subcategory)


def score_article(article: dict) -> Optional[dict]:
    if not (OPENAI_API_KEY or ANTHROPIC_API_KEY):
        log.error("No LLM API key set — set OPENAI_API_KEY or ANTHROPIC_API_KEY")
        sys.exit(1)

    title = article.get("title", "")
    summary = article.get("summary", "")
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
    date = result.get("date") or article.get("published", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    geo = get_geocode(source)

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


def build_categories_output() -> dict:
    output_categories = {}
    for cat_name, cat_data in CATEGORIES.items():
        subcats = DYNAMIC_SUBCATEGORIES.get(cat_name, cat_data.get("subcategories", []))
        output_categories[cat_name] = {
            "name": cat_name,
            "icon": cat_data.get("icon", "📌"),
            "color": cat_data.get("color", "#aaaaaa"),
            "subcategories": subcats,
            "milestones": [],
        }
    return output_categories


def generate_milestones_md(categories: dict, existing_by_subcat: dict) -> str:
    now = _utc_now()
    lines = [
        "# Human Progress Milestones",
        "",
        f"> **Live dashboard:** [transhumanists.github.io](https://transhumanists.github.io) · **API engine:** [transhumanists/apis](https://github.com/transhumanists/apis)",
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
            f"## {i}. {cat_name} {icon}",
            "",
            f"*Color: {color} · Subcategories: {len(subcats)}*",
            "",
            "| # | Subcategory | Milestone | Value | Source | Date |",
            "|:---|:------------|:-----------|:------|:-------|:-----|",
        ])

        for j, m in enumerate(milestones, 1):
            val = f"**{m.get('value', '—') or '—'}** {m.get('unit', '')}".strip()
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


def main():
    if not IN_FILE.exists():
        log.error("Missing input file: %s", IN_FILE)
        sys.exit(1)

    try:
        raw = json.loads(IN_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("Corrupt articles.json: %s", e)
        sys.exit(1)

    articles = raw.get("articles", []) if isinstance(raw, dict) else raw
    log.info("Loaded %d articles", len(articles))

    existing_by_subcat: dict = {}
    if EXISTING.exists():
        try:
            existing = json.loads(EXISTING.read_text())
            for cat_name, cat_data in existing.get("categories", {}).items():
                for sub_list in cat_data.get("subcategories", []):
                    existing_by_subcat[f"{cat_name}/{sub_list}"] = cat_data.get("milestones", [])
        except Exception as e:
            log.warning("Could not load existing milestones: %s", e)

    articles_sorted = sorted(articles, key=lambda a: -a.get("weight", 0))
    to_score = articles_sorted[:MAX_ARTICLES_TO_SCORE]
    log.info("Scoring top %d articles with %s", len(to_score), LLM_MODEL)

    all_milestones: list[dict] = []
    for i, article in enumerate(to_score):
        if i % 20 == 0:
            log.info("Scoring %d/%d...", i, len(to_score))
        m = score_article(article)
        if m:
            all_milestones.append(m)
        if i > 0 and i % RATE_LIMIT_DELAY_ARTICLES == 0:
            time.sleep(1)

    log.info("Found %d candidate milestones across %d categories", len(all_milestones), len(CATEGORIES))

    by_subcat: dict = {}
    for m in all_milestones:
        key = f"{m['category']}/{m['subcategory']}"
        if key not in by_subcat or rank_milestone(m) > rank_milestone(by_subcat[key]):
            by_subcat[key] = m

    output_categories = build_categories_output()
    events = []

    for cat_name, cat_data in output_categories.items():
        for sub in cat_data.get("subcategories", []):
            key = f"{cat_name}/{sub}"
            if key in by_subcat:
                m = by_subcat[key]
                prev = existing_by_subcat.get(key, [])
                if prev:
                    best_prev = max((rank_milestone(p) for p in prev), default=0)
                    m["is_new"] = rank_milestone(m) >= best_prev
                else:
                    m["is_new"] = True
                output_categories[cat_name]["milestones"].append(m)
                if m.get("geolocation", {}).get("lat"):
                    events.append({
                        "id": "ev-" + m["id"],
                        "title": m["title"],
                        "category": m["category"],
                        "value": f"{m.get('value', '')} {m.get('unit', '') or ''}".strip(),
                        "source": m["source"],
                        "url": m.get("url"),
                        "date": m["date"],
                        "geolocation": m["geolocation"],
                    })

    for cat_name in output_categories:
        output_categories[cat_name]["milestones"].sort(key=lambda m: -rank_milestone(m))

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

    OUT_MILESTONES.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    OUT_EVENTS.write_text(json.dumps(events_out, indent=2, ensure_ascii=False))

    md_content = generate_milestones_md(output_categories, existing_by_subcat)
    OUT_MD.write_text(md_content)

    log.info(
        "Wrote %d milestones across %d categories and %d events",
        sum(len(c["milestones"]) for c in output_categories.values()),
        len(output_categories),
        len(events),
    )


if __name__ == "__main__":
    main()
