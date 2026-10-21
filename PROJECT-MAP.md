# PROJECT-MAP: TrendForge

**Status (21 Oct 2026):** Live at https://trendforge-vfga.onrender.com on Render's free plan, with Supabase (Singapore). YouTube key added. Claude key not yet added, so the writing is placeholder. Learning mode is built and tested. Publishing is off.

| Area | File | State |
|---|---|---|
| Settings, caps, publishing switch | app/config.py | Done |
| Data model and additive migration | app/db.py | Done. Tested on Postgres 16, including 4 instances starting at once |
| Cost ledger | app/ledger.py | Done, tested |
| YouTube client (videos, channels) | app/youtube.py | Done. Live-tested for videos |
| Claude router | app/llm.py | Done. Live not yet run (no key) |
| Content agents | app/agents/scout, analyst, designer, maker, guards, publisher | Done, live-tested with real YouTube data |
| Learning agents | app/agents/learning.py | Done, tested offline. Live run pending deploy |
| Orchestrator | app/orchestrator.py | Done, tested |
| API and dashboard | app/main.py, app/static/index.html | Done, browser-tested at desktop and phone width |
| Deploy | Render service srv-dbc9ure1egvs739vtu80, Supabase project zlmspdgacoypvvohdqgq | Live |

## Open actions
1. Sunny: add ANTHROPIC_API_KEY in Render.
2. Sunny: add books (title, link, themes) and the "What I cover" subjects.
3. Optional: give Render's GitHub app access to the trendforge repo, so pushes deploy automatically.
4. Optional: upgrade to Render Starter so the app stays awake.
