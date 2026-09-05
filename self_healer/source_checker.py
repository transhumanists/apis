#!/usr/bin/env python3
"""
Source Health Checker — self-healing
Validates RSS feed URLs. Checks known-dead feeds plus a 30% spot-check sample.
Suggests replacement feeds from a known-good library (cached per-category to avoid races).
Outputs feeds_health.json.
"""
import json
import logging
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser
import requests

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
HEALTH_OUT = ROOT / "data" / "feeds_health.json"
DEAD_FEEDS = ROOT / "data" / "dead_feeds.json"

REPLACEMENTS: dict[str, list[str]] = {
    "Biotechnology": [
        "https://www.nature.com/subjects/biotechnology.rss",
        "https://www.biotechniques.com/feed/",
        "https://www.biotech-now.org/feed",
        "https://www.medgadget.com/feed",
        "https://feeds.biotecnika.org/biotecnika",
    ],
    "Computing & AGI": [
        "https://www.technologyreview.com/feed/",
        "https://feeds.feedburner.com/oreilly/radar",
        "https://towardsdatascience.com/feed",
        "https://distill.pub/rss.xml",
        "https://www.kdnuggets.com/feed",
    ],
    "Quantum Physics": [
        "https://www.quantum-inspire.com/feed/",
        "https://www.ibm.com/blogs/research/category/quantum/feed/",
        "https://ionq.com/news/rss",
        "https://www.rigetti.com/blog.rss",
        "https://www.dwavesys.com/rss.xml",
    ],
    "Energy": [
        "https://energies-ejournal.org/feed/",
        "https://spectrum.ieee.org/energy.rss",
        "https://www.powermag.com/feed/",
        "https://reneweconomy.com.au/feed/",
        "https://www.energy-storage.news/feed/",
    ],
    "Cybersecurity": [
        "https://therecord.media/feed/",
        "https://www.bleepingcomputer.com/feed/",
        "https://www.exploit-db.com/rss.xml",
        "https://www.rapid7.com/blog/feed/",
        "https://blog.talosintelligence.com/feeds/posts/default",
    ],
    "Spaceflight": [
        "https://www.universetoday.com/feed/",
        "https://www.spaceflightinsider.com/feed/",
        "https://orbitaltoday.com/feed/",
        "https://www.rocketlaunch.live/feed",
        "https://www.spacelaunchschedule.com/feed/",
    ],
    "Defense": [
        "https://www.military.com/daily-news/feed",
        "https://www.stripes.com/branches/rss",
        "https://www.armytimes.com/feed",
        "https://www.axios.com/feed",
        "https://www.longwarjournal.org/feed",
    ],
}

HEADERS = {
    "User-Agent": (
        "transhumanists-milestone-tracker/1.0 "
        "(+https://github.com/transhumanists/apis)"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
TIMEOUT = 10
MAX_WORKERS = 16

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("source_checker")

_REPLACEMENT_CACHE: dict[str, str] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def check_url(url: str) -> dict:
    result = {"url": url, "status": "unknown", "http": 0, "items": 0, "checked_at": _utc_now()}
    try:
        r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        result["http"] = r.status_code
        if r.status_code in (200, 301, 302):
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=False)
            result["http"] = r.status_code
            if r.status_code == 200:
                parsed = feedparser.parse(r.content)
                if parsed.entries:
                    result["status"] = "healthy"
                    result["items"] = len(parsed.entries)
                    return result
                result["status"] = "empty"
            else:
                result["status"] = "dead"
        else:
            result["status"] = "dead"
    except requests.Timeout:
        result["status"] = "timeout"
    except requests.RequestException as e:
        result["status"] = "error"
        result["error"] = str(e)
    except Exception as e:
        result["status"] = "exception"
        result["error"] = str(e)
    return result


def find_replacement(category: str, dead_url: str) -> str | None:
    if category in _REPLACEMENT_CACHE:
        cached = _REPLACEMENT_CACHE[category]
        if cached != dead_url:
            return cached
    for url in REPLACEMENTS.get(category, []):
        if url == dead_url:
            continue
        result = check_url(url)
        if result["status"] == "healthy":
            _REPLACEMENT_CACHE[category] = url
            return url
        time.sleep(0.5)
    return None


def main():
    dead: list[dict] = []
    if DEAD_FEEDS.exists():
        dead = json.loads(DEAD_FEEDS.read_text())
        log.info("Loaded %d dead feeds from previous run", len(dead))

    try:
        from scrapers.rss_fetcher import FEEDS
    except Exception:
        FEEDS = []

    urls_to_check: list[dict] = []
    for d in dead:
        urls_to_check.append({"url": d["url"], "category": d.get("category", "Unknown"), "is_dead": True})

    if FEEDS:
        import random
        random.seed(42)
        sample = random.sample(FEEDS, max(1, len(FEEDS) // 3))
        for f in sample:
            urls_to_check.append({"url": f["url"], "category": f.get("category", "Unknown"), "is_dead": False})

    log.info("Checking %d feeds (%d dead, %d spot-check)",
             len(urls_to_check),
             sum(1 for u in urls_to_check if u.get("is_dead")),
             sum(1 for u in urls_to_check if not u.get("is_dead")))

    results: list[dict] = []
    replaced: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_url, u["url"]): u for u in urls_to_check}
        for future in as_completed(futures):
            u = futures[future]
            try:
                r = future.result()
                r["category"] = u["category"]
                results.append(r)
                if r["status"] != "healthy":
                    log.warning("Dead feed: %s (%s) — looking for replacement in %s",
                               u["url"], r["status"], u["category"])
                    rep = find_replacement(u["category"], u["url"])
                    if rep:
                        replaced.append({
                            "old_url": u["url"],
                            "new_url": rep,
                            "category": u["category"],
                            "reason": r["status"],
                        })
                        log.info("Replaced %s with %s", u["url"], rep)
            except Exception as e:
                log.error("Error checking %s: %s", u["url"], e)

    HEALTH_OUT.write_text(json.dumps({
        "last_update": _utc_now(),
        "checked": len(urls_to_check),
        "results": results,
        "replaced": replaced,
    }, indent=2, ensure_ascii=False))
    log.info("Health check done. %d results, %d replacements suggested.",
             len(results), len(replaced))

    if replaced:
        log.info("Suggested replacements:")
        for r in replaced:
            print(f"  {r['category']}: {r['old_url']}  ->  {r['new_url']}")


if __name__ == "__main__":
    main()
