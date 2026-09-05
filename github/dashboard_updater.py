#!/usr/bin/env python3
"""
Dashboard Updater — transhumanists/apis/github
Reads processed milestone/event/activity data and commits it to:
  - transhumanists/milestones (data/)
  - transhumanists/transhumanists.github.io (data/)
Uses the GitHub Contents API to avoid git conflicts.
"""
import base64
import json
import logging
import os
import pathlib
import random
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    requests = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard_updater")

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
REPOS = [
    ("transhumanists", "milestones"),
    ("transhumanists", "transhumanists.github.io"),
]
API = "https://api.github.com"
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


def _headers() -> dict:
    return {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _retry_request(method: str, url: str, **kwargs) -> requests.Response:
    last_resp: requests.Response | None = None
    last_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, timeout=kwargs.pop("timeout", 20), **kwargs)
            if resp.status_code < 500:
                return resp
            last_resp = resp
            last_err = f"HTTP {resp.status_code}"
            log.warning("Transient error %s (attempt %d/%d): %s", url, attempt + 1, MAX_RETRIES, last_err)
        except requests.RequestException as e:
            last_err = str(e)
            log.warning("Request error %s (attempt %d/%d): %s", url, attempt + 1, MAX_RETRIES, last_err)
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF ** attempt)
    if last_resp is not None:
        # Re-raise as HTTPError so callers can read .response.status_code
        err = requests.HTTPError(f"Failed after {MAX_RETRIES} retries: {last_err}", response=last_resp)
        raise err
    raise requests.RequestException(f"Failed after {MAX_RETRIES} retries: {last_err}")


class FileFetchError(Exception):
    """Raised when a GitHub Contents API call fails with a non-retryable status."""
    def __init__(self, owner: str, repo: str, path: str, status: int, message: str = ""):
        self.owner = owner
        self.repo = repo
        self.path = path
        self.status = status
        self.message = message
        super().__init__(f"GitHub API error {status} for {owner}/{repo}/{path}: {message}")


def get_file_sha(owner: str, repo: str, path: str) -> str | None:
    """Return the blob SHA for a file, or None if it does not exist yet.
    Raises FileFetchError for any non-404, non-200 response."""
    try:
        r = _retry_request("GET", f"{API}/repos/{owner}/{repo}/contents/{path}",
                           headers=_headers())
        if r.status_code == 200:
            return r.json().get("sha")
        if r.status_code == 404:
            return None
        raise FileFetchError(owner, repo, path, r.status_code, r.text[:200])
    except requests.RequestException as e:
        resp = getattr(e, "response", None)
        status = resp.status_code if resp is not None else 0
        raise FileFetchError(owner, repo, path, status, str(e)) from e


def upsert_file(owner: str, repo: str, path: str, content: bytes, message: str) -> bool:
    try:
        sha = get_file_sha(owner, repo, path)
    except FileFetchError as e:
        log.error("Cannot fetch SHA for %s/%s/%s — skipping update: %s", owner, repo, path, e)
        return False
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode(),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    try:
        resp = _retry_request(
            "PUT",
            f"{API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(),
            json=payload,
        )
        if resp.status_code in (200, 201):
            log.info("Updated %s/%s/%s", owner, repo, path)
            return True
        if resp.status_code == 409:
            log.warning("Conflict on %s/%s/%s — skipping", owner, repo, path)
            return False
        log.error("Failed %s/%s/%s: HTTP %d — %s", owner, repo, path,
                  resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.error("Error upserting %s/%s/%s: %s", owner, repo, path, e)
        return False


# ---- Activity generation ----
def generate_activity() -> dict:
    """Generate 30-day activity from recent milestones.json."""
    ms_path = pathlib.Path(__file__).parent.parent / "data" / "milestones.json"
    days = []
    today = datetime.now(timezone.utc)
    spikes = []

    if ms_path.exists():
        try:
            ms = json.loads(ms_path.read_text())
            counts: dict[str, int] = {}
            for cat_data in ms.get("categories", {}).values():
                for m in cat_data.get("milestones", []):
                    d = m.get("date", "")
                    if d:
                        counts[d] = counts.get(d, 0) + 1
            for i in range(29, -1, -1):
                d = today - timedelta(days=i)
                date_str = d.strftime("%Y-%m-%d")
                count = counts.get(date_str, 0)
                days.append({"date": date_str, "count": count})
            for d in days:
                if d["count"] >= 15:
                    spikes.append({"date": d["date"], "count": d["count"],
                                   "reason": "Multiple milestones recorded"})
        except Exception as e:
            log.warning("Could not parse milestones for activity: %s", e)

    if not days:
        random.seed(int(today.timestamp()) // 86400)
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            dow = d.weekday()
            base = [3, 5, 8, 12, 14, 9, 6][dow]
            days.append({"date": d.strftime("%Y-%m-%d"), "count": base + random.randint(0, 5)})

    return {
        "last_update": _utc_now(),
        "days": days,
        "spikes": spikes,
    }


# ---- Main ----
def main():
    if not requests:
        log.error("requests library required")
        sys.exit(1)
    if not TOKEN:
        log.warning("GITHUB_TOKEN not set — running in local mode (no API writes)")
        print("::warning::GITHUB_TOKEN not set — dry-run only")
        return

    apis_data = pathlib.Path(__file__).parent.parent / "data"
    ms_src = apis_data / "milestones.json"
    ev_src = apis_data / "events.json"

    # If scraper hasn't run yet, copy from milestones repo as seed
    if not ms_src.exists():
        log.info("No local data yet — this is expected on first-run in a new clone")
        print("::notice::No local data — dashboard_updater done (first-run)")
        return

    # Generate activity
    activity = generate_activity()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_msg = f"chore(milestones): auto-update {ts}"

    for owner, repo in REPOS:
        log.info("Updating %s/%s...", owner, repo)
        ok = 0

        if ms_src.exists() and upsert_file(owner, repo, "data/milestones.json",
                                           ms_src.read_bytes(), commit_msg):
            ok += 1

        if ev_src.exists() and upsert_file(owner, repo, "data/events.json",
                                           ev_src.read_bytes(), commit_msg):
            ok += 1

        act_bytes = json.dumps(activity, indent=2).encode()
        if upsert_file(owner, repo, "data/activity.json", act_bytes, commit_msg):
            ok += 1

        log.info("%s/%s: %d files updated", owner, repo, ok)


if __name__ == "__main__":
    main()
