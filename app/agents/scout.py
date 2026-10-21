"""Scout: pulls video data for every active topic on the project."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..db import Video
from ..youtube import estimate_units
from ..ledger import youtube_headroom, QuotaExceeded
from .base import Ctx


STOP = {"the", "and", "for", "with", "how", "what", "your", "you", "from", "into", "best"}


def is_relevant(query: str, v) -> bool:
    """YouTube search pads results with loosely related videos, and some uploaders
    stuff popular words into tags and descriptions. Keep a video only if at least
    one meaningful search word appears in its TITLE."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1 and w not in STOP]
    if not words:
        return True
    hay = v.title.lower()
    # Words of 4+ letters match as a word start, so "decision" also finds "decisions"
    # and "leader" finds "leadership". Short tokens like "p6" must match whole.
    return any(re.search(rf"\b{re.escape(w)}" + ("" if len(w) >= 4 else r"\b"), hay) for w in words)


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
    queries = {t.id: t.query for t in topics}
    kept = [s for s in stats if is_relevant(queries.get(seen.get(s.yt_id), ""), s)]
    if len(kept) < len(stats):
        ctx.log("scout", f"Dropped {len(stats) - len(kept)} off-topic video(s) that never mention the search words.")
    stats = kept
    for s in stats:
        ctx.db.add(Video(run_id=ctx.run.id, topic_id=seen.get(s.yt_id), yt_id=s.yt_id, title=s.title,
                         channel_title=s.channel_title, channel_id=s.channel_id, description=s.description, tags=s.tags,
                         published_at=s.published_at, views=s.views, likes=s.likes,
                         comments=s.comments, duration_s=s.duration_s))
    ctx.db.flush()
    ctx.log("scout", f"Stored {len(stats)} videos with stats.")
    return len(stats)
