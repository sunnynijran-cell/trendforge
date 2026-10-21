"""Analyst: scores what is popular now, then groups the leaders into themes.

Scoring is plain maths, not a model, so it is cheap, repeatable and auditable:
  views_per_hour   = views / hours since upload (min 1 hour)
  engagement_rate  = (likes + 2 x comments) / views   (comments weigh more)
  score = 0.7 x pct(views_per_hour) + 0.2 x pct(engagement) + 0.1 x recency
pct() is the percentile rank inside this run, so scores compare like with like.
Only the theme grouping uses Claude (Haiku).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import Trend, Video, as_utc
from .base import Ctx

W_VPH, W_ENG, W_REC = 0.7, 0.2, 0.1   # velocity dominates: 'popular now'


def pct_rank(values: list[float]) -> list[float]:
    """Percentile rank 0..1, ties share the average rank."""
    n = len(values)
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg / (n - 1)
        i = j + 1
    return ranks


def score_videos(videos: list[Video], now: datetime | None = None,
                 lookback_hours: float = 14 * 24) -> list[tuple[Video, float, float, float]]:
    now = now or datetime.now(timezone.utc)
    if not videos:
        return []
    vph, eng, rec = [], [], []
    for v in videos:
        hours = max((now - as_utc(v.published_at)).total_seconds() / 3600, 1.0)
        vph.append(v.views / hours)
        eng.append((v.likes + 2 * v.comments) / v.views if v.views else 0.0)
        rec.append(max(0.0, 1 - hours / lookback_hours))
    p_vph, p_eng = pct_rank(vph), pct_rank(eng)
    out = []
    for i, v in enumerate(videos):
        score = W_VPH * p_vph[i] + W_ENG * p_eng[i] + W_REC * rec[i]
        out.append((v, round(score, 4), round(vph[i], 2), round(eng[i], 5)))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def run(ctx: Ctx) -> int:
    videos = list(ctx.db.scalars(select(Video).where(Video.run_id == ctx.run.id)))
    if not videos:
        ctx.log("analyst", "No videos to score. Try broader topics or a longer lookback.", "warn")
        return 0
    lookback = max((t.lookback_days for t in ctx.project.topics if t.active), default=14) * 24
    ranked = score_videos(videos, lookback_hours=lookback)[: ctx.cfg.top_trends]
    trends = []
    for v, s, vph, eng in ranked:
        t = Trend(run_id=ctx.run.id, video_id=v.id, score=s, views_per_hour=vph, engagement_rate=eng)
        ctx.db.add(t)
        trends.append(t)
    ctx.db.flush()
    ctx.log("analyst", f"Scored {len(videos)} videos. Top {len(trends)} kept. "
                       f"Leader: '{ranked[0][0].title[:80]}' at {ranked[0][2]:,.0f} views/hour.")

    data = {"videos": [{"trend_id": t.id, "title": t.video.title, "tags": t.video.tags[:8]} for t in trends]}
    try:
        res = ctx.llm.json(ctx.db, "cluster",
                           "You group YouTube videos into 2-6 content themes. Theme names are 1-4 words.",
                           "Group these trending videos into themes. Every trend_id must appear in exactly one "
                           'theme. Return {"themes":[{"name":str,"trend_ids":[int],"why":str}]}.\nDATA:'
                           + json.dumps(data), ctx.run.id)
        by_id = {t.id: t for t in trends}
        themes = res.get("themes") if isinstance(res, dict) else None
        for th in themes if isinstance(themes, list) else []:
            if not isinstance(th, dict) or not isinstance(th.get("trend_ids"), list):
                continue
            for tid in th["trend_ids"]:
                if isinstance(tid, int) and tid in by_id:
                    by_id[tid].theme = str(th.get("name", "unclustered"))[:120]
        ctx.log("analyst", f"Grouped into {len({t.theme for t in trends})} theme(s) with {getattr(ctx.llm, 'label', '') or ctx.cfg.model_fast}.")
    except Exception as e:  # themes are helpful, not essential
        ctx.log("analyst", f"Theme grouping skipped: {e}", "warn")
    ctx.db.flush()
    return len(trends)
