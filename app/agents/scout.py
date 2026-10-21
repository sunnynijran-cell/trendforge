"""Scout: pulls video data for every active topic on the project."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db import Video
from ..youtube import estimate_units
from ..ledger import youtube_headroom, QuotaExceeded
from .base import Ctx


def run(ctx: Ctx) -> int:
    topics = [t for t in ctx.project.topics if t.active]
    if not topics:
        raise ValueError("No active topics on this project. Add at least one search topic.")

    need = estimate_units(len(topics), [t.max_results for t in topics])
    left = youtube_headroom(ctx.db, ctx.cfg)
    if need > left:
        raise QuotaExceeded(f"Scout needs about {need} YouTube units, only {max(left, 0)} left today.")
    ctx.log("scout", f"{len(topics)} topic(s), estimated {need} quota units, {left} available.")

    seen: dict[str, int | None] = {}
    for t in topics:
        after = datetime.now(timezone.utc) - timedelta(days=t.lookback_days)
        ids = ctx.yt.search(ctx.db, t.query, t.region, t.max_results, after, ctx.run.id)
        new = [i for i in ids if i not in seen]
        for i in new:
            seen[i] = t.id
        ctx.log("scout", f"'{t.query}' ({t.region}, last {t.lookback_days}d): {len(ids)} hits, {len(new)} new.")

    stats = ctx.yt.videos(ctx.db, list(seen), ctx.run.id) if seen else []
    for s in stats:
        ctx.db.add(Video(run_id=ctx.run.id, topic_id=seen.get(s.yt_id), yt_id=s.yt_id, title=s.title,
                         channel_title=s.channel_title, description=s.description, tags=s.tags,
                         published_at=s.published_at, views=s.views, likes=s.likes,
                         comments=s.comments, duration_s=s.duration_s))
    ctx.db.flush()
    ctx.log("scout", f"Stored {len(stats)} videos with stats.")
    return len(stats)
