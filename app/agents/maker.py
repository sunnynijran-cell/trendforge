"""Maker: writes each brief into a finished item, then runs the guards.

Guards run in two layers:
  1. Code checks (free): length, banned terms, copying from ANY video in the run,
     impersonation. Copying is checked against every source video, not just the
     ones the Designer cited, so a missing citation cannot hide lifted wording.
  2. Haiku second opinion (cheap): brand rules and unsupported claims. Advisory:
     its notes appear on the item in your review queue; you decide.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from ..db import Brief, Item, Trend, run_source_texts
from . import guards
from .base import Ctx

FORMAT_GUIDE = {
    "linkedin_post": "150-300 words. Hook in line one. Short paragraphs. One clear takeaway. No hashtag spam.",
    "x_post": "Under 260 characters.",
    "youtube_short_script": "45-60 seconds spoken. Hook in first 2 seconds. Scene notes in [brackets].",
    "youtube_script": "6-10 minutes. Hook, three sections, call to action. Scene notes in [brackets].",
    "blog_post": "800-1200 words with subheadings.",
    "email": "Under 200 words. Subject line as the title.",
    "carousel": "6-8 slides. Format each slide as 'Slide N: headline / one line'.",
    "talk_outline": "Section headings with timings and one key point each.",
    "course_module": "Learning objective, 3 lessons, one exercise, one quiz question.",
}


def second_opinion(ctx: Ctx, title: str, body: str) -> list[str]:
    """Haiku reads the item against the project's brand rules. Returns issues."""
    p = ctx.project
    data = {"brand_rules": p.brand_rules, "audience": p.audience, "title": title, "body": body}
    try:
        res = ctx.llm.json(
            ctx.db, "check",
            "You are a strict editor. Flag only real problems: breaks a stated brand rule, makes a factual "
            "claim with no basis, promises results, or could embarrass the brand. Do not flag style preferences.",
            'Check this item. Return {"ok":bool,"issues":[str]}.\nDATA:' + json.dumps(data), ctx.run.id)
    except Exception as e:
        ctx.log("maker", f"Second-opinion check unavailable ({e}). Item still goes to you for review.", "warn")
        return []
    if not isinstance(res, dict) or res.get("ok") is not False:
        return []
    return [f"Editor: {str(i)[:300]}" for i in (res.get("issues") or []) if i][:5] or ["Editor flagged an issue."]


def run(ctx: Ctx) -> int:
    p = ctx.project
    briefs = list(ctx.db.scalars(select(Brief).where(Brief.run_id == ctx.run.id)))
    all_sources = run_source_texts(ctx.db, ctx.run.id)
    made = blocked = 0
    for b in briefs:
        srcs = list(ctx.db.scalars(select(Trend).where(Trend.id.in_(b.source_trend_ids or [-1]))))
        data = {"brief": {"title": b.title, "angle": b.angle, "format": b.format, "rationale": b.rationale},
                "project": {"name": p.name, "audience": p.audience, "brand_rules": p.brand_rules},
                "format_guide": FORMAT_GUIDE.get(b.format, "Fit the format."),
                "source_titles": [t.video.title for t in srcs]}
        try:
            res = ctx.llm.json(
                ctx.db, "make",
                "You write original, specific content in plain UK English. Short sentences. No filler, "
                "no hype words, no em-dashes. Never copy wording from the source videos.",
                'Write the item. Return {"title":str,"body":str}.\nDATA:' + json.dumps(data), ctx.run.id)
            if not isinstance(res, dict) or not isinstance(res.get("body"), str):
                raise ValueError("model reply had no text body")
        except Exception as e:
            ctx.log("maker", f"Brief {b.id} failed: {e}", "error")
            if type(e).__name__ == "BudgetExceeded":
                raise
            continue
        title, body = str(res.get("title") or b.title)[:300], res["body"]
        checks = guards.check_item(b.format, title, body, list(p.banned_terms or []), all_sources)
        if checks["passed"]:
            # Advisory: shown to you on the item, but you make the call.
            checks = {**checks, "editor_notes": second_opinion(ctx, title, body)}
        status = "pending_review" if checks["passed"] else "blocked"
        ctx.db.add(Item(brief_id=b.id, project_id=p.id, run_id=ctx.run.id, kind=b.format,
                        title=title, body=body, status=status, checks=checks))
        made += 1
        blocked += status == "blocked"
        if status == "blocked":
            ctx.log("maker", f"'{title[:60]}' blocked: {'; '.join(checks['issues'])}", "warn")
    ctx.db.flush()
    ctx.log("maker", f"{made} item(s) made, {made - blocked} sent for your review, {blocked} blocked.")
    return made
