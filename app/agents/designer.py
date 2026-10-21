"""Designer: turns the strongest themes into briefs that fit the project."""
from __future__ import annotations

import json

from sqlalchemy import select

from ..db import Brief, Item, Trend
from .base import Ctx


def themes_for_run(ctx: Ctx) -> list[dict]:
    trends = list(ctx.db.scalars(select(Trend).where(Trend.run_id == ctx.run.id)))
    groups: dict[str, list[Trend]] = {}
    for t in trends:
        groups.setdefault(t.theme, []).append(t)
    out = []
    for name, ts in groups.items():
        ts.sort(key=lambda t: t.score, reverse=True)
        out.append({
            "name": name,
            "score": round(sum(t.score for t in ts) / len(ts), 4),
            "trend_ids": [t.id for t in ts],
            "example_titles": [t.video.title for t in ts[:3]],
            "views_per_hour_top": ts[0].views_per_hour,
        })
    out.sort(key=lambda th: th["score"], reverse=True)
    return out


def past_winners(ctx: Ctx, limit: int = 3) -> list[dict]:
    """Feedback loop: what worked before for this project, by recorded results."""
    items = ctx.db.scalars(select(Item).where(Item.project_id == ctx.project.id, Item.status == "published"))
    scored = [(float(i.performance.get("score", 0)), i) for i in items if i.performance]
    scored.sort(key=lambda r: r[0], reverse=True)
    return [{"title": i.title, "kind": i.kind, "performance": i.performance} for _, i in scored[:limit]]


def run(ctx: Ctx) -> int:
    themes = themes_for_run(ctx)
    if not themes:
        ctx.log("designer", "No themes to design from.", "warn")
        return 0
    p = ctx.project
    allowed = list(p.output_types) or ["linkedin_post"]
    data = {
        "project": {"name": p.name, "description": p.description, "audience": p.audience,
                    "brand_rules": p.brand_rules, "output_types": allowed},
        "themes": themes, "count": ctx.cfg.briefs_per_run, "past_winners": past_winners(ctx),
    }
    res = ctx.llm.json(
        ctx.db, "design",
        "You are a content strategist. You turn what is popular on YouTube into original work that serves "
        "a specific project. Never copy a video. Find the angle the audience needs that the trend has not covered.",
        f"Write {ctx.cfg.briefs_per_run} briefs. Each must use one format from output_types and cite the "
        "trend_ids it draws on. Return {\"briefs\":[{\"title\":str,\"angle\":str,\"format\":str,"
        "\"rationale\":str,\"source_trend_ids\":[int]}]}.\nDATA:" + json.dumps(data),
        ctx.run.id)

    valid_ids = {tid for th in themes for tid in th["trend_ids"]}
    raw = res.get("briefs") if isinstance(res, dict) else None
    if not isinstance(raw, list):
        raise ValueError("Designer reply had no list of briefs.")
    made = 0
    for b in raw[: ctx.cfg.briefs_per_run]:
        if not isinstance(b, dict) or not b.get("title") or not b.get("angle"):
            ctx.log("designer", "Dropped a malformed brief.", "warn")
            continue
        fmt = b.get("format") if b.get("format") in allowed else allowed[0]
        ids = b.get("source_trend_ids") if isinstance(b.get("source_trend_ids"), list) else []
        srcs = [i for i in ids if isinstance(i, int) and i in valid_ids]
        ctx.db.add(Brief(run_id=ctx.run.id, project_id=p.id, title=str(b["title"])[:300],
                         angle=str(b["angle"]), format=fmt, rationale=str(b.get("rationale", "")),
                         source_trend_ids=srcs))
        made += 1
    ctx.db.flush()
    ctx.log("designer", f"{made} brief(s) from {len(themes)} theme(s) with {ctx.cfg.model_smart}.")
    return made
