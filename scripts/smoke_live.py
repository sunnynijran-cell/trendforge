"""One-off live check once your keys are in .env. Costs ~101 YouTube units
and one tiny Haiku call (well under 1 cent).

    python scripts/smoke_live.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.llm import ClaudeLLM  # noqa: E402
from app.youtube import LiveYouTube  # noqa: E402

ok = True
init_db()
with SessionLocal() as db:
    if not settings.youtube_api_key:
        print("YOUTUBE_API_KEY missing"); ok = False
    else:
        yt = LiveYouTube()
        ids = yt.search(db, "project controls", "GB", 5, datetime.now(timezone.utc) - timedelta(days=30), None)
        vids = yt.videos(db, ids, None)
        print(f"YouTube OK: {len(vids)} videos, top '{vids[0].title if vids else '-'}'")
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY missing"); ok = False
    else:
        out = ClaudeLLM().json(db, "check", "You check things.", 'Return {"ok": true, "issues": []}', None)
        print(f"Claude OK ({settings.model_fast}): {out}")
sys.exit(0 if ok else 1)
