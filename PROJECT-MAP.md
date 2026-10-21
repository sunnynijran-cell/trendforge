# PROJECT-MAP: TrendForge

**Status (21 Oct 2026):** Built. Independent review in three rounds (14 findings plus 4 follow-ups, all fixed). 66 tests pass on SQLite, and the suite also passes on Postgres 16. Live keys being created.

| Area | File | State |
|---|---|---|
| Settings and caps | app/config.py | Done |
| Data model (SQLite / Supabase Postgres) | app/db.py | Done. Tested on Postgres 16 locally, Supabase not yet connected |
| Cost ledger | app/ledger.py | Done, tested |
| YouTube client | app/youtube.py | Done. Tested against mocked HTTP. Live not yet run |
| Claude router | app/llm.py | Done. Tested against a mocked SDK. Live not yet run |
| Scout / Analyst / Designer / Maker / Guards / Publisher | app/agents/ | Done, tested |
| Orchestrator and approval gate | app/orchestrator.py | Done, tested |
| API | app/main.py | Done, tested |
| Dashboard | app/static/index.html | Done, browser-tested at desktop and phone width |
| Deploy | Dockerfile, render.yaml | Written. Not yet deployed |

## Open actions
1. Sunny: create the YouTube and Anthropic keys and put them in .env (see README).
2. Run scripts/smoke_live.py.
3. Set up the first real project and its topics, then run it and judge the output quality.
4. Deploy to Render with Supabase.
5. Next build: Canva and HeyGen makers, Metricool target, scheduled runs.
