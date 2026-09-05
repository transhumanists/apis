# Contributing to transhumanists/apis

This repository contains an automated pipeline that scrapes RSS feeds, scores science &
technology milestones using free LLM APIs, and publishes a live dashboard.

## Repository structure

```
.github/workflows/
  pipeline.yml      # Scheduled pipeline: scrape → score → post → commit
  tests.yml         # CI: pytest + ruff + mypy + llm-action smoke test

scrapers/
  rss_fetcher.py    # Concurrent RSS feed fetcher with rate limiting and caching
  source_checker.py # Self-healer: verifies milestone source URLs

llm/
  score_milestone.py # LLM scoring + milestone generation
  router.py          # Vendored FreeModelsRouter (from neohiro/LLM)

self_healer/
  source_checker.py  # HTTP HEAD/GET checks on milestone source URLs

data/
  milestones.json     # Generated output
  articles.json       # Fetched articles
  events.json         # Generated output
  providers.json      # Vendored router data
  models.json         # Vendored router data
  free_models.json    # Vendored router data
  unlimited.json      # Vendored router data
```

## Local development

### Prerequisites

- Python 3.12+
- `pip install -r requirements.txt`

### Running the pipeline locally

```bash
# Scrape feeds
python scrapers/rss_fetcher.py

# Score milestones (requires at least one API key)
OPENAI_API_KEY=sk-... python llm/score_milestone.py

# Run the full self-healer
python self_healer/source_checker.py
```

### Running tests

```bash
pytest tests/ -v
```

### Running linters

```bash
ruff check llm/ scrapers/ tests/
mypy llm/score_milestone.py scrapers/rss_fetcher.py --config-file mypy.ini
```

### Pre-commit

Install pre-commit hooks to run linters and tests before each commit:

```bash
pip install pre-commit
pre-commit install
```

## Adding a new RSS feed

1. Add the feed definition to `FEEDS` in `scrapers/rss_fetcher.py`:
   ```python
   {"url": "https://example.com/feed.xml", "category": "Biotechnology", "weight": 2},
   ```
2. Run `python scrapers/rss_fetcher.py` and verify the feed parses without errors.
3. Add a test in `tests/test_smoke.py` if coverage is desired.

## Adding a new LLM provider

The FreeModelsRouter (`llm/router.py`) handles provider routing. To add a new free provider:

1. Add the provider and its free models to `neohiro/LLM` (upstream).
2. Copy the updated JSON files into `data/` (or update `neohiro/llm-action` to vend the new data).
3. Add the provider's API key as a repo secret (e.g., `NEW_PROVIDER_API_KEY`).
4. Add the secret to the `llm-action` step in `.github/workflows/pipeline.yml`.

## Coding standards

- **Type safety**: All new code in `llm/` and `scrapers/` should pass `mypy --strict`.
- **Linting**: Ruff with `ruff.toml` config. No new `except Exception` bare catches.
- **Tests**: New features should include tests in `tests/`.
- **Line length**: Max 120 characters.
- **No new `print()` statements** — use `log.info()` / `log.warning()`.

## Pull request process

1. Branch from `main`: `git checkout -b feature/your-feature`.
2. Make changes with tests.
3. Ensure `pytest tests/`, `ruff check`, and `mypy` all pass.
4. Open a PR against `main`. CI runs both `pytest` and the `llm-action smoke test`.
5. PR requires 1 approving review. Squash-merge on approval.
6. Branch protection on `main` requires all required checks to pass.

## Secrets reference

| Secret | Description | Required for |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key | OpenAI fallback in scoring |
| `ANTHROPIC_API_KEY` | Anthropic API key | Anthropic fallback in scoring |
| `GROQ_API_KEY` | Groq API key | Free tier scoring |
| `CEREBRAS_API_KEY` | Cerebras API key | Free tier scoring |
| `SAMBANOVA_API_KEY` | Sambanova API key | Free tier scoring |
| `GITHUB_MODELS_API_KEY` | GitHub Models API key | Free tier scoring |
| `CLOUDFLARE_API_KEY` | Cloudflare Workers AI key | Free tier scoring |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID | Required if `CLOUDFLARE_API_KEY` is set |
| `OPENROUTER_API_KEY` | OpenRouter API key | Free tier scoring |
| `GOOGLE_AI_API_KEY` | Google AI Studio key | Free tier scoring |
| `HUGGINGFACE_API_KEY` | HuggingFace API key | Free tier scoring |
| `NVIDIA_API_KEY` | NVIDIA NIM API key | Free tier scoring |
| `COHERE_API_KEY` | Cohere API key | Free tier scoring |
| `MILESTONES_DISPATCH_TOKEN` | Fine-grained PAT with `repo` access to `transhumanists/milestones` and `transhumanists/transhumanists.github.io` | Commit milestones to `milestones` + push to `github.io` |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `LLM_ROUTER_ENABLED` | `true` | Enable FreeModelsRouter cascade. Set to `false` to use only OpenAI/Anthropic. |
| `LLM_MODEL` | `gpt-4o` | Default LLM model (overridden by router when `LLM_ROUTER_ENABLED=true`). |
