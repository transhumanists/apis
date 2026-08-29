#!/usr/bin/env python3
"""
RSS Fetcher — transhumanists/apis
Scrapes 80+ RSS feeds across 7 categories, outputs articles.json.
Self-heals broken feeds on next run via source_checker.py.
"""
import json, time, hashlib, datetime as dt, logging, pathlib, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

# ---- Config ----
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
MAX_WORKERS = 20
FETCH_RETRIES = 2

# ---- Feeds (80+ sources across 7 categories) ----
FEEDS: list[dict] = [
    # Biotechnology
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
    # Computing & AGI
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
    # Quantum Physics
    {"url": "https://phys.org/rss-feed/quantum-physics/", "category": "Quantum Physics", "weight": 3},
    {"url": "https://www.sciencenews.org/feeds/quantum-computing/topstories", "category": "Quantum Physics", "weight": 3},
    {"url": "https://arxiv.org/rss/quant-ph", "category": "Quantum Physics", "weight": 3},
    {"url": "https://www.nature.com/nq.rss", "category": "Quantum Physics", "weight": 2},
    {"url": "https://www.quantamagazine.org/feed/", "category": "Quantum Physics", "weight": 3},
    {"url": "https://www.scientificamerican.com/feed/", "category": "Quantum Physics", "weight": 2},
    {"url": "https://www.technologyreview.com/topic/quantum-computing/feed", "category": "Quantum Physics", "weight": 2},
    # Renewable Energy
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
    # Cybersecurity
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
    # Spaceflight
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
    # Military & Defense
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
    # Intel / OSINT
    {"url": "https://www.bellingcat.com/feed/", "category": "Defense", "weight": 3},
    {"url": "https://www.osintdojo.com/blog/rss.xml", "category": "Defense", "weight": 2},
    {"url": "https://intelnews.org/feed/", "category": "Defense", "weight": 2},
    {"url": "https://www.sipri.org/media/press_releases/feed", "category": "Defense", "weight": 3},
    {"url": "https://www.iiss.org/blogs/military-applications/feed", "category": "Defense", "weight": 2},
]


# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scrape.log"),
    ],
)
log = logging.getLogger("rss_fetcher")


# ---- Fetch a single feed ----
def fetch_feed(feed_def: dict) -> list[dict]:
    url = feed_def["url"]
    category = feed_def["category"]
    weight = feed_def.get("weight", 1)
    articles: list[dict] = []
    errors: list[dict] = []

    for attempt in range(FETCH_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()
            if "html" in content_type and attempt < FETCH_RETRIES - 1:
                log.warning("HTML returned for RSS feed %s (attempt %d), retrying", url, attempt + 1)
                time.sleep(2)
                continue

            parsed = feedparser.parse(resp.content)
            if parsed.bozo and attempt < FETCH_RETRIES - 1:
                log.warning("Bozo feed %s (attempt %d): %s", url, attempt + 1, parsed.bozo_exception)
                time.sleep(1)
                continue

            for entry in parsed.entries[:50]:
                try:
                    link = getattr(entry, "link", None) or getattr(entry, "id", "")
                    article_id = hashlib.sha256(link.encode()).hexdigest()[:16]

                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        try:
                            published = dt.datetime.utcnow().strftime("%Y-%m-%d")
                        except Exception:
                            pass

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
                        "source": parsed.feed.get("title", url),
                        "category": category,
                        "weight": weight,
                        "published": published or dt.datetime.utcnow().strftime("%Y-%m-%d"),
                        "fetched_at": dt.datetime.utcnow().isoformat() + "Z",
                    })
                except Exception as e:
                    log.debug("Error parsing entry from %s: %s", url, e)

            log.info("Fetched %d articles from %s", len(articles), url)
            return articles

        except requests.Timeout:
            log.warning("Timeout fetching %s (attempt %d/%d)", url, attempt + 1, FETCH_RETRIES)
        except requests.HTTPError as e:
            log.warning("HTTP %d for %s (attempt %d/%d)", e.response.status_code, url, attempt + 1, FETCH_RETRIES)
            if e.response.status_code == 404:
                errors.append({"url": url, "error": "HTTP 404", "status": "dead"})
                break
        except requests.RequestException as e:
            log.warning("Error fetching %s: %s (attempt %d/%d)", url, e, attempt + 1, FETCH_RETRIES)

        time.sleep(2 ** attempt)

    errors.append({"url": url, "error": str(e), "status": "error"})
    return articles


# ---- Main ----
def main():
    log.info("Starting RSS fetch — %d feeds across %d categories",
             len(FEEDS), len(set(f["category"] for f in FEEDS)))

    all_articles: list[dict] = []
    dead_feeds: list[dict] = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_feed, f): f for f in FEEDS}
        for future in as_completed(futures):
            feed_def = futures[future]
            try:
                articles = future.result()
                all_articles.extend(articles)
            except Exception as e:
                log.error("Unhandled error for %s: %s", feed_def["url"], e)
                dead_feeds.append({"url": feed_def["url"], "error": str(e), "status": "exception"})

    elapsed = time.time() - start

    # Deduplicate by ID
    seen: set[str] = set()
    unique: list[dict] = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)

    output = {
        "last_update": dt.datetime.utcnow().isoformat() + "Z",
        "feeds_total": len(FEEDS),
        "feeds_dead": dead_feeds,
        "articles_total": len(all_articles),
        "articles_unique": len(unique),
        "elapsed_seconds": round(elapsed, 1),
        "articles": unique,
    }

    OUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info(
        "Done. %d unique articles from %d feeds in %.1fs. Dead: %d",
        len(unique), len(FEEDS), elapsed, len(dead_feeds)
    )

    # Echo for GitHub Actions output
    print(f"::set-output-name=count::{len(unique)}")

    if dead_feeds:
        dead_path = pathlib.Path(__file__).parent.parent / "data" / "dead_feeds.json"
        dead_path.write_text(json.dumps(dead_feeds, indent=2))


if __name__ == "__main__":
    main()
