#!/usr/bin/env python3
"""
Facebook Poster — transhumanists/apis/social
Posts milestone highlights to facebook.com/transhumanistsBE
via the Meta Graph API using a Page Access Token.
Requires FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN in environment or secrets.
"""
import json
import logging
import os
import pathlib
from contextlib import suppress
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

ROOT = pathlib.Path(__file__).parent.parent.parent
MILESTONES_JSON = ROOT / "data" / "milestones.json"
POST_HISTORY = ROOT / "data" / "fb_post_history.json"
GRAPH_API_URL = "https://graph.facebook.com/v19.0"

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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_access_token() -> tuple[str | None, str | None]:
    page_id = os.environ.get("FB_PAGE_ID") or os.environ.get("FB_PAGE_ID_SECRET", "")
    token = os.environ.get("FB_PAGE_ACCESS_TOKEN") or os.environ.get("FB_ACCESS_TOKEN_SECRET", "")
    if not page_id or not token:
        log.warning("FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN not set — skipping Facebook post.")
        return None, None
    return page_id, token


def build_message(milestones: dict) -> str:
    """Build a concise, engaging post from the latest milestones."""
    lines = ["🌐 transhumanists — Human Progress Report\n"]
    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    lines.append(f"📅 {today_str}\n")
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
        lines.append(f"{icon} {cat_name}: **{value_str}** by {source} [{date}]")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 Tracked by transhumanists/apis · FrenzyPenguin Media")
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
        if resp.status_code in (200, 201) and "id" in data:
            post_id = data["id"]
            log.info("Posted to Facebook: %s", post_id)
            return {"success": True, "post_id": post_id, "url": f"https://facebook.com/{page_id}/posts/{post_id}"}
        err = data.get("error", {})
        log.error("Facebook API error %s: %s", err.get("code"), err.get("message"))
        return {"success": False, "error": err.get("message", str(data))}
    except requests.RequestException as e:
        log.error("HTTP error posting to Facebook: %s", e)
        return {"success": False, "error": str(e)}


def should_post() -> bool:
    """Post max once per UTC day."""
    if not POST_HISTORY.exists():
        return True
    try:
        hist = json.loads(POST_HISTORY.read_text())
        last = hist.get("last_post_date", "")
        if last == _utc_date():
            log.info("Already posted today (%s) — skipping.", last)
            return False
    except Exception:
        pass
    return True


def record_post(post_id: str) -> None:
    hist = {}
    if POST_HISTORY.exists():
        with suppress(Exception):
            hist = json.loads(POST_HISTORY.read_text())
    hist["last_post_date"] = _utc_date()
    hist["last_post_id"] = post_id
    tmp = POST_HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(hist, indent=2))
    tmp.replace(POST_HISTORY)


def main():
    if not should_post():
        log.info("Post skipped.")
        return

    page_id, token = get_access_token()
    if not page_id or not token:
        log.warning("Facebook credentials not configured — no post sent.")
        print("::warning::Facebook credentials not set. Set FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN.")
        return

    if not MILESTONES_JSON.exists():
        log.error("Missing milestones data: %s", MILESTONES_JSON)
        raise SystemExit(1)

    try:
        milestones = json.loads(MILESTONES_JSON.read_text())
    except json.JSONDecodeError as e:
        log.error("Corrupt milestones.json: %s", e)
        raise SystemExit(1)

    message = build_message(milestones)

    log.info("Posting milestone update to Facebook...")
    result = post_to_facebook(page_id, token, message)

    if result.get("success"):
        record_post(result["post_id"])
        print(f"::notice::Posted: {result.get('url')}")
    else:
        print(f"::error::Facebook post failed: {result.get('error')}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
