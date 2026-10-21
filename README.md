# TrendForge

A team of agents that watches YouTube for anything you point it at, works out what is popular, designs and makes content for your projects, and publishes it once you approve.

```
Scout ──> Analyst ──> Designer ──> Maker ──> [YOU APPROVE] ──> Publisher
YouTube    maths +      Sonnet       Sonnet +     dashboard        export /
API        Haiku                     guards                        webhook
```

## Top-down view (what each agent does)

| Agent | Job | Engine | Cost |
|---|---|---|---|
| Scout | Searches YouTube for each project topic and pulls the stats | YouTube Data API v3 | 100 units a topic + 1 unit per 50 videos |
| Analyst | Scores videos: 70% views per hour, 20% engagement, 10% recency, ranked within the run. Groups the leaders into themes | Maths + Claude Haiku | 1 cheap call |
| Designer | Turns the top themes into briefs that fit the project's audience, rules and formats. Learns from past results | Claude Sonnet | 1 call |
| Maker | Writes each brief into a finished item | Claude Sonnet | 1 call per brief |
| Guards | Code checks: too long, banned terms, 8+ words copied from any video in the run, impersonation. Then a Haiku editor adds advisory notes on brand rules and unsupported claims. You make the call | Code + Claude Haiku | 1 cheap call per item |
| You | Edit, approve or reject in the Review queue | Dashboard | |
| Publisher | Ships approved items only: Markdown export, or a webhook to Zapier, Make or n8n (and from there to Metricool, LinkedIn or a CMS) | Code | Free |

## Bottom-up view (where to look)

- **Run log tab**: every agent step in order, with warnings and errors.
- **Usage meters**: YouTube units used today, Claude spend this month.
- **Recent runs**: quota and Claude cost for each run.
- `app/agents/*.py`: one file per agent. Each one is short and readable.

## Safety and cost controls

- Nothing publishes without your approval. This is enforced in code and tested.
- Before a run starts, the dashboard shows what it will cost.
- YouTube: the default allowance is 10,000 units a day and resets at midnight Pacific. 500 units are kept in reserve, and a run that won't fit is refused before it starts.
- Claude: a monthly cap ($50) and a per-run cap ($2). The worst-case cost of each call is reserved against both caps before the call is made. This means concurrent runs cannot jointly overshoot. The real cost is logged afterwards.
- YouTube retries are billed by Google, so each attempt is booked.
- Your API key goes in a request header, so it never appears in a URL, error message or log.
- Every live run sends a heartbeat every 20 seconds. If the server restarts and the heartbeat stops, the run is marked as interrupted, so the project is never locked. During an overlapping deploy, live runs on the other instance are left alone.
- Publishing is atomic. A double-click or a second tab cannot post the same item twice.
- On Render, `REQUIRE_POSTGRES=1` stops the app from starting on throwaway SQLite. This means the cost ledger always survives a deploy.
- Models: Haiku for routine work, Sonnet for creative work. Opus is never used.
- If a later step fails, the spend is still recorded.
- One run per project at a time.
- It uses the official API, not scraping, so it stays within YouTube's terms.

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env              # add your keys, or leave them blank for offline demo mode
uvicorn app.main:app --port 8000  # open http://localhost:8000
```

With no keys set, it runs on recorded YouTube data and a fake model. You can try the full flow for free.

## First live check

```bash
python scripts/smoke_live.py      # ~101 YouTube units, under 1p of Claude
```

## Deploy (Render + Supabase)

1. Supabase: create a project and copy the Postgres connection string (Session pooler). Use the form `postgresql+psycopg://...`. The tables are created on first start.
2. Render: New > Blueprint > this repo. It uses `render.yaml`. Fill in `DATABASE_URL`, `YOUTUBE_API_KEY` and `ANTHROPIC_API_KEY`. `ADMIN_TOKEN` is generated for you. Copy it, because the dashboard asks for it.
3. Keep it to one worker. Runs happen in a background thread inside the web process.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite has 68 tests. They pass on SQLite, and on real Postgres 16 with the browser test excluded (`TEST_DATABASE_URL=postgres://... python -m pytest`):

- Unit tests: scoring maths, guards and parsing.
- Whole-pipeline runs: success, no topics, quota exhausted, budget cap and reservations, copied wording (cited or not), editor blocks, malformed model output, restart recovery, stale runs, lock release.
- The full HTTP API flow: run, review, publish and feedback.
- Live adapters against mocked HTTP: request shape, key kept out of URLs and errors, 50-ID batching, 5xx retry billed per attempt, Google quota error, model routing, cost maths, JSON retry, webhook success and failure, double-publish protection, config parsing.
- A real Chromium browser run through the dashboard, which saves screenshots to `evidence/`.

## Not built yet (next steps)

- Image and video making: hook the Maker to Canva or HeyGen for visual formats.
- Direct posting: currently done by webhook. Metricool can be added as a named target.
- Scheduled runs, for example a daily cron on Render.
- Pulling performance automatically. At the moment you record results by hand in the Review queue.
- Transcript analysis. Captions need OAuth from the video owner, so the Scout uses titles, tags, descriptions and stats only.
