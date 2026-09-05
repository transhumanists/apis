#!/usr/bin/env python3
"""
RSS Fetcher — transhumanists/apis
Scrapes 80+ RSS feeds across 7 categories, outputs articles.json.
Self-heals broken feeds on next run via source_checker.py.

Rate-limiting: Every upstream call goes through rate_limit.check_and_consume()
to stay within the free-tier budget of every host. Cached feeds (within
`rss_generic.cache_ttl`) are served from disk instead of re-fetched.
"""
import hashlib
import json
import logging
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup

# Make `apis` importable so we can use the rate_limit module
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from rate_limit import cache_get, cache_set, check_and_consume, record_response

OUT_FILE = pathlib.Path(__file__).parent.parent / "data" / "articles.json"
OUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "transhumanists-milestone-tracker/1.0 "
        "(+https://github.com/transhumanists/apis; "
        "contact@transhumanists.github.io)"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

TIMEOUT = 15
MAX_WORKERS = 6  # reduced from 20 — too many parallel requests triggers RSS host 429s
FETCH_RETRIES = 2
MAX_ARTICLE_SIZE = 1024 * 1024
MAX_ARTICLES_PER_FEED = 50
ENABLE_CACHE = True  # set False to force fresh fetch

FEEDS: list[dict[str, Any]] = [
    {"url": "https://www.nature.com/nbt.rss", "category": "Biotechnology", "weight": 3},
    {"url": "https://www.biorxiv.org/rss/all.xml", "category": "Biotechnology", "weight": 2},
    {"url": "https://www.sciencedirect.com/science/article/feed/atom", "category": "Biotechnology", "weight": 2},
    {"url": "https://feeds.feedburner.com/ScienceDaily_Biotechnology?format=xml", "category": "Biotechnology", "weight": 2},
    {"url": "https://www.news-medical.net/rss/biotechnology.aspx", "category": "Biotechnology", "weight": 1},
    {"url": "https://www.fiercebiotech.com/rss/xml", "category": "Biotechnology", "weight": 2},
    {"url": "https://www.genengnews.com/feed/", "category": "Biotechnology", "weight": 2},
    {"url": "https://www.statnews.com/category/science/feed/", "category": "Biotechnology", "weight": 2},
    {"url": "https://feeds.feedburner.com/genomeweb/", "category": "Biotechnology", "weight": 1},
    {"url": "https://www.the-scientist.com/rss", "category": "Biotechnology", "weight": 2},
    {"url": "https://arxiv.org/rss/cs.AI", "category": "Computing & AGI", "weight": 3},
    {"url": "https://arxiv.org/rss/cs.LG", "category": "Computing & AGI", "weight": 3},
    {"url": "https://arxiv.org/rss/cs.CL", "category": "Computing & AGI", "weight": 3},
    {"url": "https://feeds.feedburner.com/ventureconx", "category": "Computing & AGI", "weight": 1},
    {"url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "category": "Computing & AGI", "weight": 2},
    {"url": "https://feeds.feedburner.com/technologyreview", "category": "Computing & AGI", "weight": 2},
    {"url": "https://arxiv.org/rss/cs.CV", "category": "Computing & AGI", "weight": 2},
    {"url": "https://deepmind.com/blog/feed/b/", "category": "Computing & AGI", "weight": 3},
    {"url": "https://openai.com/blog/rss/", "category": "Computing & AGI", "weight": 3},
    {"url": "https://www.anthropic.com/news.rss", "category": "Computing & AGI", "weight": 3},
    {"url": "https://huggingface.co/blog/feed.xml", "category": "Computing & AGI", "weight": 2},
    {"url": "https://blog.google/technology/ai/rss/", "category": "Computing & AGI", "weight": 3},
    {"url": "https://phys.org/rss-feed/quantum-physics/", "category": "Quantum Physics", "weight": 3},
    {"url": "https://www.sciencenews.org/feeds/quantum-computing/topstories", "category": "Quantum Physics", "weight": 3},
    {"url": "https://arxiv.org/rss/quant-ph", "category": "Quantum Physics", "weight": 3},
    {"url": "https://www.nature.com/nq.rss", "category": "Quantum Physics", "weight": 2},
    {"url": "https://www.quantamagazine.org/feed/", "category": "Quantum Physics", "weight": 3},
    {"url": "https://www.scientificamerican.com/feed/", "category": "Quantum Physics", "weight": 2},
    {"url": "https://www.technologyreview.com/topic/quantum-computing/feed", "category": "Quantum Physics", "weight": 2},
    {"url": "https://phys.org/rss-feed/energy-and-fuels/", "category": "Energy", "weight": 3},
    {"url": "https://www.sciencedaily.com/rss/matter_energy/fusion.rss", "category": "Energy", "weight": 3},
    {"url": "https://ieeexplore.ieee.org/rss/recent.xhtml", "category": "Energy", "weight": 2},
    {"url": "https://www.nrel.gov/news/rss/", "category": "Energy", "weight": 2},
    {"url": "https://www.energy.gov/feeds/", "category": "Energy", "weight": 2},
    {"url": "https://cleantechnica.com/feed/", "category": "Energy", "weight": 2},
    {"url": "https://www.greentechmedia.com/rss", "category": "Energy", "weight": 2},
    {"url": "https://PV-magazine.com/feed/", "category": "Energy", "weight": 1},
    {"url": "https://www.reuters.com/energy.rss", "category": "Energy", "weight": 3},
    {"url": "https://wwwITER.org/news/feeds", "category": "Energy", "weight": 3},
    {"url": "https://feeds.feedburner.com/TheHackersNews", "category": "Cybersecurity", "weight": 3},
    {"url": "https://www.schneier.com/feed/atom/", "category": "Cybersecurity", "weight": 2},
    {"url": "https://krebsonsecurity.com/feed/", "category": "Cybersecurity", "weight": 3},
    {"url": "https://www.darkreading.com/rss.xml", "category": "Cybersecurity", "weight": 2},
    {"url": "https://feeds.feedburner.com/riskybiz", "category": "Cybersecurity", "weight": 2},
    {"url": "https://www.cisa.gov/uscert/ncas/current-activity.xml", "category": "Cybersecurity", "weight": 3},
    {"url": "https://nvd.nist.gov/feeds/cve/item/microsoft.rdf", "category": "Cybersecurity", "weight": 2},
    {"url": "https://www.wired.com/feed/category/security/latest/rss", "category": "Cybersecurity", "weight": 2},
    {"url": "https://arstechnica.com/security/feed/", "category": "Cybersecurity", "weight": 2},
    {"url": "https://www.securityweek.com/feed/", "category": "Cybersecurity", "weight": 2},
    {"url": "https://unit42.paloaltonetworks.com/feed/", "category": "Cybersecurity", "weight": 2},
    {"url": "https://spacenews.com/feed/", "category": "Spaceflight", "weight": 3},
    {"url": "https://www.space.com/feeds/all/", "category": "Spaceflight", "weight": 3},
    {"url": "https://www.nasa.gov/rss/dyn/breaking_news.rss", "category": "Spaceflight", "weight": 3},
    {"url": "https://www.planetary.org/blogs.rss", "category": "Spaceflight", "weight": 2},
    {"url": "https://www.esa.int/rssfeed/Our_Activities/Space_Transportation", "category": "Spaceflight", "weight": 2},
    {"url": "https://spaceflightnow.com/feed/", "category": "Spaceflight", "weight": 3},
    {"url": "https://www.thespacereview.com/rss.xml", "category": "Spaceflight", "weight": 2},
    {"url": "https://arstechnica.com/space/feed/", "category": "Spaceflight", "weight": 2},
    {"url": "https://www.nasaspaceflight.com/feed/", "category": "Spaceflight", "weight": 3},
    {"url": "https://www.spacex.com/feed/", "category": "Spaceflight", "weight": 3},
    {"url": "https://blogs.nasa.gov/feed/rss/", "category": "Spaceflight", "weight": 2},
    {"url": "https://www.defensenews.com/feed/", "category": "Defense", "weight": 2},
    {"url": "https://www.janes.com/defence-news/rss", "category": "Defense", "weight": 3},
    {"url": "https://www.reuters.com/world/rss", "category": "Defense", "weight": 2},
    {"url": "https://breakingdefense.com/feed/", "category": "Defense", "weight": 3},
    {"url": "https://www.nato.int/cps/en/natohq/news.rss", "category": "Defense", "weight": 3},
    {"url": "https://www.cbsnews.com/latest/rss/world/", "category": "Defense", "weight": 1},
    {"url": "https://www.bbc.com/news/world/rss", "category": "Defense", "weight": 1},
    {"url": "https://www.npr.org/rss/rss.php?id=1004", "category": "Defense", "weight": 1},
    {"url": "https://www.iswresearch.org/feed", "category": "Defense", "weight": 3},
    {"url": "https://www.criticalthreats.org/feed", "category": "Defense", "weight": 2},
    {"url": "https://www.cna.org/news-analysis/rss", "category": "Defense", "weight": 2},
    {"url": "https://www.thedrive.com/the-war-zone/feed", "category": "Defense", "weight": 2},
    {"url": "https://www.bellingcat.com/feed/", "category": "Defense", "weight": 3},
    {"url": "https://www.osintdojo.com/blog/rss.xml", "category": "Defense", "weight": 2},
    {"url": "https://intelnews.org/feed/", "category": "Defense", "weight": 2},
    {"url": "https://www.sipri.org/media/press_releases/feed", "category": "Defense", "weight": 3},
    {"url": "https://www.iiss.org/blogs/military-applications/feed", "category": "Defense", "weight": 2},
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scrape.log"),
    ],
)
log = logging.getLogger("rss_fetcher")


def fetch_feed(feed_def: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = feed_def["url"]
    category = feed_def["category"]
    weight = feed_def.get("weight", 1)
    articles: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    error_logged = False
    last_error = ""
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    now_iso = now_utc.isoformat().replace("+00:00", "Z")

    # ── Cache check (cache-first policy) ──────────────────────────────
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:32]
    if ENABLE_CACHE:
        cached = cache_get(cache_key, max_age_seconds=3600)  # 1h TTL for RSS
        if cached is not None:
            log.info("Cache hit for %s (%d articles)", url, len(cached.get("articles", [])))
            return cached.get("articles", []), []
        error_cache = cache_get(f"error_{cache_key}", max_age_seconds=300)
        if error_cache is not None and len(error_cache) > 0:
            log.info("Cache: skipping feed with recent error: %s", url)
            return [], [{"url": url, "error": "recent_cache_error", "status": "skipped_cache"}]

    # ── Rate-limit check ─────────────────────────────────────────────
    res = check_and_consume("rss_generic")
    if not res.allowed:
        log.warning("RSS budget exhausted (retry in %ds) — skipping: %s", res.retry_after, url)
        return [], [{"url": url, "error": f"rate_limit: retry in {res.retry_after}s", "status": "skipped"}]

    for attempt in range(FETCH_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            record_response("rss_generic", resp.status_code)

            if resp.status_code == 429:
                log.warning("RSS 429 %s (attempt %d) — backing off %ds",
                            url, attempt + 1, res.retry_after)
                time.sleep(min(res.retry_after, 60))
                continue

            resp.raise_for_status()

            if len(resp.content) > MAX_ARTICLE_SIZE:
                log.warning("Feed %s (%.1f MB) exceeds 1MB limit — truncating",
                           url, len(resp.content) / 1024 / 1024)
                feed_bytes = resp.content[:MAX_ARTICLE_SIZE]
            else:
                feed_bytes = resp.content

            content_type = resp.headers.get("Content-Type", "").lower()
            if "html" in content_type and attempt < FETCH_RETRIES - 1:
                log.warning("HTML returned for RSS feed %s (attempt %d) — retrying",
                           url, attempt + 1)
                time.sleep(2)
                continue

            parsed = feedparser.parse(feed_bytes)
            if parsed.bozo and attempt < FETCH_RETRIES - 1:
                log.warning("Bozo feed %s (attempt %d): %s",
                           url, attempt + 1, parsed.bozo_exception)
                time.sleep(1)
                continue

            feed_title = (
                parsed.feed.get("title") if getattr(parsed, "feed", None) else url
            )

            for entry in list(parsed.entries)[:MAX_ARTICLES_PER_FEED]:
                try:
                    link = getattr(entry, "link", None) or getattr(entry, "id", "") or ""
                    article_id = hashlib.sha256(link.encode()).hexdigest()[:16]

                    published = ""
                    pp = getattr(entry, "published_parsed", None)
                    if pp:
                        with suppress(Exception):
                            published = time.strftime("%Y-%m-%d", pp)

                    raw_summary = (
                        getattr(entry, "summary", None)
                        or getattr(entry, "description", None)
                        or ""
                    )
                    soup = BeautifulSoup(raw_summary, "lxml")
                    summary_text = soup.get_text(separator=" ", strip=True)[:600]
                    title = getattr(entry, "title", "[no title]") or "[no title]"

                    articles.append({
                        "id": article_id,
                        "title": title.strip(),
                        "summary": summary_text,
                        "url": link,
                        "source": feed_title or url,
                        "category": category,
                        "weight": weight,
                        "published": (published or today_str),
                        "fetched_at": now_iso,
                    })
                except (AttributeError, TypeError, ValueError, KeyError) as e:
                    log.debug("Entry parse error %s: %s", url, e)

            # ── Cache the result ───────────────────────────────────
            if ENABLE_CACHE and articles:
                cache_set(cache_key, {"articles": articles})
                log.debug("Cached %d articles for %s", len(articles), url)

            return articles, errors

        except requests.Timeout:
            last_error = "Timeout"
            log.warning("Timeout %s (attempt %d/%d)", url, attempt + 1, FETCH_RETRIES)
            if attempt < FETCH_RETRIES - 1:
                time.sleep(2 ** attempt)
        except requests.HTTPError as e:
            resp_obj = getattr(e, "response", None)
            status = resp_obj.status_code if resp_obj is not None else 0
            last_error = f"HTTP {status}"
            log.warning("HTTP %d %s (attempt %d/%d)", status, url, attempt + 1, FETCH_RETRIES)
            if status == 404:
                errors.append({"url": url, "error": last_error, "status": "dead"})
                error_logged = True
                break
            if status >= 500 and attempt < FETCH_RETRIES - 1:
                time.sleep(2 ** attempt)
        except requests.RequestException as e:
            last_error = str(e)
            log.warning("Error %s: %s (attempt %d/%d)", url, e, attempt + 1, FETCH_RETRIES)
            if attempt < FETCH_RETRIES - 1:
                time.sleep(2 ** attempt)

    log.info("Fetched %d articles from %s", len(articles), url)

    if not articles and not error_logged:
        errors.append({"url": url, "error": last_error or "Unknown error", "status": "error"})
        if ENABLE_CACHE:
            cache_set(f"error_{cache_key}", {"error": last_error})

    return articles, errors


def main() -> None:
    log.info("Starting RSS fetch — %d feeds across %d categories",
             len(FEEDS), len({f["category"] for f in FEEDS}))

    all_articles: list[dict[str, Any]] = []
    dead_feeds: list[dict[str, Any]] = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_feed, f): f for f in FEEDS}
        for future in as_completed(futures):
            feed_def = futures[future]
            try:
                articles, errors = future.result()
                all_articles.extend(articles)
                dead_feeds.extend(errors)
            except (requests.RequestException, ValueError, KeyError, OSError) as e:
                log.error("Unhandled exception for %s: %s", feed_def["url"], e)
                dead_feeds.append({"url": feed_def["url"], "error": str(e), "status": "exception"})

    elapsed = time.time() - start

    seen: dict[str, dict[str, Any]] = {}
    for a in all_articles:
        if a["id"] not in seen:
            seen[a["id"]] = a
    unique = list(seen.values())

    output = {
        "last_update": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "feeds_total": len(FEEDS),
        "feeds_dead": dead_feeds,
        "articles_total": len(all_articles),
        "articles_unique": len(unique),
        "elapsed_seconds": round(elapsed, 1),
        "articles": unique,
    }

    OUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info("Done. %d unique from %d feeds in %.1fs. Dead: %d",
             len(unique), len(FEEDS), elapsed, len(dead_feeds))

    if dead_feeds:
        dead_path = pathlib.Path(__file__).parent.parent / "data" / "dead_feeds.json"
        dead_path.write_text(json.dumps(dead_feeds, indent=2))


if __name__ == "__main__":
    main()
