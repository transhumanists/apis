#!/usr/bin/env python3
"""
Facebook Poster — transhumanists/apis/social
Posts milestone highlights to facebook.com/transhumanistsBE
via the Meta Graph API using a Page Access Token.
Requires FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN in environment or secrets.
"""
import os, sys, json, logging, pathlib, datetime as dt
from typing import Optional

try:
    import requests
except ImportError:
    requests = None

ROOT = pathlib.Path(__file__).parent.parent.parent
MILESTONES_JSON = ROOT / "data" / "milestones.json"
POST_HISTORY = ROOT / "data" / "fb_post_history.json"

FB_API = "https://graph.facebook.com/v19.0"
GRAPH_VERSION = "v19.0"
GRAPH_API_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("facebook_poster")

CATEGORY_ICONS = {
    "Biotechnology": "🧬",
    "Computing & AGI": "🧠",
    "Quantum Physics": "⚛️",
    "Energy": "⚡",
    "Cybersecurity": "🛡️",
    "Spaceflight": "🚀",
    "Defense": "🌍",
}


def get_access_token() -> tuple[Optional[str], Optional[str]]:
    page_id = os.environ.get("FB_PAGE_ID") or os.environ.get("FB_PAGE_ID_SECRET", "")
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN") or os.environ.get("FB_ACCESS_TOKEN_SECRET", "")
    if not page_id or not token:
        log.warning("FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN not set — skipping Facebook post.")
        return None, None
    return page_id, token


def build_message(milestones: dict) -> str:
    """Build a concise, engaging post from the latest milestones."""
    lines = ["🌐 transhumanists — Human Progress Report\n"]

    today = dt.datetime.now(dt.timezone.utc).strftime("%B %d, %Y")
    lines.append(f"📅 {today}\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━\n")

    for cat_name, cat_data in milestones.get("categories", {}).items():
        icon = CATEGORY_ICONS.get(cat_name, "📌")
        ms = cat_data.get("milestones", [])
        if not ms:
            continue
        top = ms[0]
        value_str = f"{top.get('value', '?')} {top.get('unit', '')}".strip()
        source = top.get("source", "Unknown")
        date = top.get("date", "")
        lines.append(
            f"{icon} {cat_name}: **{value_str}** by {source} [{date}]"
        )

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 Tracked by transhumanists/apis · frenZypenguin Media")
    lines.append("🔗 https://transhumanists.github.io")
    return "\n".join(lines)


def post_to_facebook(page_id: str, token: str, message: str) -> dict:
    if not requests:
        log.error("requests library not installed")
        return {"success": False, "error": "no requests library"}

    url = f"{GRAPH_API_URL}/{page_id}/feed"
    payload = {
        "message": message,
        "access_token": token,
    }
    try:
        resp = requests.post(url, data=payload, timeout=20)
        data = resp.json()
        if resp.status_code == 200 and "id" in data:
            post_id = data["id"]
            log.info("Posted to Facebook: %s", post_id)
            return {"success": True, "post_id": post_id, "url": f"https://facebook.com/{page_id}/posts/{post_id}"}
        else:
            err = data.get("error", {})
            log.error("Facebook API error %s: %s", err.get("code"), err.get("message"))
            return {"success": False, "error": err.get("message", str(data))}
    except requests.RequestException as e:
        log.error("HTTP error posting to Facebook: %s", e)
        return {"success": False, "error": str(e)}


def should_post() -> bool:
    """Post max once per day."""
    if not POST_HISTORY.exists():
        return True
    try:
        hist = json.loads(POST_HISTORY.read_text())
        last = hist.get("last_post_date", "")
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        if last == today:
            log.info("Already posted today (%s) — skipping.", today)
            return False
    except Exception:
        pass
    return True


def record_post(post_id: str):
    hist = {}
    if POST_HISTORY.exists():
        try:
            hist = json.loads(POST_HISTORY.read_text())
        except Exception:
            pass
    hist["last_post_date"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    hist["last_post_id"] = post_id
    POST_HISTORY.write_text(json.dumps(hist, indent=2))


def main():
    if not should_post():
        log.info("Post skipped.")
        return

    page_id, token = get_access_token()
    if not page_id or not token:
        log.warning("Facebook credentials not configured — no post sent.")
        print("::error::Facebook credentials not set. Set FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN.")
        sys.exit(0)  # Don't fail the workflow

    if not MILESTONES_JSON.exists():
        log.error("Missing milestones data: %s", MILESTONES_JSON)
        sys.exit(1)

    milestones = json.loads(MILESTONES_JSON.read_text())
    message = build_message(milestones)

    log.info("Posting milestone update to Facebook...")
    result = post_to_facebook(page_id, token, message)

    if result.get("success"):
        record_post(result["post_id"])
        print(f"::notice::Posted: {result.get('url')}")
    else:
        print(f"::error::Facebook post failed: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
