# transhumanists/apis

> **Public API integrations** for the [transhumanists](https://github.com/transhumanists) milestone tracker.

This repository is the **engine** behind the [transhumanists dashboard](https://transhumanists.github.io).
It scrapes 80+ RSS feeds, runs them through an LLM to extract structured milestone data, self-heals broken feeds, posts updates to Facebook, and commits new data to the dashboard repos.

---

## 🧩 Architecture

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ 80+ RSS     │ → │  LLM        │ → │  Dashboard  │ → │  Sites +    │
│  feeds      │   │  scoring    │   │  updater    │   │  Socials    │
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
       │                                       │
       └── self_healer (source checker) ──────┘
```

### Modules

| Module | What it does |
|--------|--------------|
| `scrapers/rss_fetcher.py` | Pulls 80+ feeds in parallel, deduplicates, writes `data/articles.json` |
| `llm/score_milestone.py` | Calls OpenAI / Anthropic, extracts structured milestones, writes `data/milestones.json` + `data/events.json` |
| `self_healer/source_checker.py` | Validates all feeds, finds replacements for dead ones, writes `data/feeds_health.json` |
| `social/facebook_poster.py` | Posts daily milestone digest to [facebook.com/transhumanistsBE](https://facebook.com/transhumanistsBE) via Meta Graph API |
| `github/dashboard_updater.py` | Commits updated data to `transhumanists/milestones` and `transhumanists/transhumanists.github.io` via GitHub API |

### Scheduled Execution

The `transhumanists/milestones` repo contains a GitHub Action that runs the pipeline every 6 hours:
- `00:00 UTC` — full scrape
- `06:00 UTC` — full scrape
- `12:00 UTC` — full scrape
- `18:00 UTC` — full scrape

---

## 🛠️ Setup

```bash
cd apis
pip install -r requirements.txt

export OPENAI_API_KEY="sk-..."               # or ANTHROPIC_API_KEY
export GITHUB_TOKEN="ghp_..."                 # for cross-repo commits
export FB_PAGE_ID="1234567890"                # optional, for FB posting
export FB_PAGE_ACCESS_TOKEN="EAAB..."         # optional, for FB posting

python scrapers/rss_fetcher.py
python llm/score_milestone.py
python self_healer/source_checker.py
python github/dashboard_updater.py
```

### Required GitHub Secrets / Variables

| Secret / Variable | Required? | Purpose |
|-------------------|-----------|---------|
| `OPENAI_API_KEY` | yes (or ANTHROPIC_API_KEY) | LLM milestone extraction |
| `ANTHROPIC_API_KEY` | alternative | LLM milestone extraction |
| `GITHUB_TOKEN` | yes (Actions) | Cross-repo data commits |
| `FB_PAGE_ID` | optional | Facebook posting |
| `FB_PAGE_ACCESS_TOKEN` | optional | Facebook posting |
| `LLM_MODEL` | optional | Override model (default `gpt-4o`) |
| `FACEBOOK_ENABLED` | optional var | Set to `true` to enable FB posting |

---

## 📊 Adding a New RSS Source

1. Add an entry to `scrapers/rss_fetcher.py` `FEEDS` list:

```python
{"url": "https://example.com/feed.rss", "category": "Biotechnology", "weight": 2},
```

2. Add a known geocode for the source in `llm/score_milestone.py` `KNOWN_GEOCODES` for accurate map plotting.

3. If the source belongs to a new category, also add it to `CATEGORIES` in both `score_milestone.py` and `source_checker.py`.

4. Submit a PR.

---

## 🧪 Testing

```bash
pytest tests/
```

Smoke tests use mocked HTTP responses — no live API calls.

---

## 🔗 Related

- 🧬 [transhumanists dashboard](https://transhumanists.github.io/) — live milestones + world map
- 🗺️ [neohiro/worldmap](https://github.com/neohiro/worldmap) — world map mainframe with 5 data dimensions (PROGRESS / EVENTS / AERIAL / ORBITAL / IDENTITY)
- 🤖 [neohiro/LLM](https://github.com/neohiro/LLM) — free LLM knowledge base + router (private)
- 🎬 [FrenzyPenguin Media](https://neohiro.github.io/frenzypenguin-media/) — video deep-dives
- 🌐 [neohiro.github.io](https://neohiro.github.io/) — main site
- 💖 [Sponsor neohiro](https://github.com/sponsors/neohiro) — cover API costs

---

## 🤖 LLM tools manifest

Every deterministic tool in this repo has a JSON contract in
`../neohiro-llm/data/apis_tools_manifest.json`. The LLM uses this to
know which script to call for a given question. Every tool:

- Is **stateless** (no LLM calls inside)
- Accepts `--key value` or stdin JSON
- Emits one JSON object to stdout
- Has `when` + `where` on every record
- Is **cache-first** (never makes unnecessary upstream calls)
- Respects free-tier rate limits (backoff on 429)

The manifest is the **single source of truth** for the LLM's tool-calling
context. Update it when adding or changing a tool.

---

## 📄 License

MIT
