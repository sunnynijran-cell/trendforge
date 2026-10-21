"""HTTP API and dashboard host. Run: uvicorn app.main:app --port 8000"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import orchestrator
from .agents import guards, learning, publisher
from .agents.maker import FORMAT_GUIDE
from .config import settings
from .db import (Book, Brief, CommentDraft, EventLog, Gap, Item, LensCard, Project, Run, SessionLocal, Topic,
                 Trend, Voice, as_utc, init_db, run_source_texts)
from .ledger import usage_summary, youtube_headroom
from .youtube import estimate_units

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    with SessionLocal() as db:
        orchestrator.recover_interrupted(db)   # a deploy or crash mid-run must not lock a project
    yield


app = FastAPI(title="TrendForge", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def get_db():
    with SessionLocal() as db:
        yield db


def auth(x_admin_token: str = Header(default="")) -> None:
    if settings.admin_token and not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(401, "Missing or wrong X-Admin-Token.")


Guarded = Depends(auth)

# ---------- schemas ----------
OUTPUT_TYPES = sorted(FORMAT_GUIDE)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    audience: str = ""
    brand_rules: str = ""
    output_types: list[str] = Field(default_factory=lambda: ["linkedin_post"])
    banned_terms: list[str] = Field(default_factory=list)
    mode: Literal["content", "learning"] = "content"
    coverage: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("coverage")
    @classmethod
    def _cov(cls, v):
        return [c.strip()[:200] for c in v if c and c.strip()]

    @field_validator("output_types")
    @classmethod
    def _types(cls, v):
        bad = [t for t in v if t not in FORMAT_GUIDE]
        if bad or not v:
            raise ValueError(f"output_types must be a non-empty subset of {OUTPUT_TYPES}; bad: {bad}")
        return v


class ProjectPatch(BaseModel):
    description: str | None = None
    audience: str | None = None
    brand_rules: str | None = None
    output_types: list[str] | None = None
    banned_terms: list[str] | None = None
    mode: Literal["content", "learning"] | None = None
    coverage: list[str] | None = Field(default=None, max_length=20)

    @field_validator("coverage")
    @classmethod
    def _cov(cls, v):
        return ProjectIn._cov(v) if v is not None else v

    @field_validator("output_types")
    @classmethod
    def _types(cls, v):
        return ProjectIn._types(v) if v is not None else v


class TopicIn(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    region: str = Field(default="GB", pattern=r"^[A-Z]{2}$")
    max_results: int = Field(default=25, ge=1, le=50)
    lookback_days: int = Field(default=14, ge=1, le=90)


class ReviewIn(BaseModel):
    note: str = ""
    body: str | None = None   # optional edit before approving
    title: str | None = None


class PublishIn(BaseModel):
    target: Literal["export", "webhook"] = "export"


class PerformanceIn(BaseModel):
    views: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    reactions: int = Field(default=0, ge=0)
    leads: int = Field(default=0, ge=0)
    note: str = ""


# ---------- serialisers ----------
def project_out(p: Project) -> dict:
    return {"id": p.id, "name": p.name, "description": p.description, "audience": p.audience,
            "brand_rules": p.brand_rules, "output_types": p.output_types, "banned_terms": p.banned_terms,
            "mode": p.mode or "content", "coverage": p.coverage or [],
            "topics": [{"id": t.id, "query": t.query, "region": t.region, "max_results": t.max_results,
                        "lookback_days": t.lookback_days, "active": t.active} for t in p.topics]}


def run_out(r: Run) -> dict:
    return {"id": r.id, "project_id": r.project_id, "status": r.status, "stage": r.stage,
            "started_at": as_utc(r.started_at).isoformat(),
            "finished_at": as_utc(r.finished_at).isoformat() if r.finished_at else None,
            "error": r.error, "quota_used": r.quota_used, "llm_cost_usd": r.llm_cost_usd}


def item_out(i: Item) -> dict:
    return {"id": i.id, "project_id": i.project_id, "run_id": i.run_id, "brief_id": i.brief_id,
            "kind": i.kind, "title": i.title, "body": i.body, "status": i.status, "checks": i.checks,
            "review_note": i.review_note, "publish_target": i.publish_target, "publish_ref": i.publish_ref,
            "published_at": as_utc(i.published_at).isoformat() if i.published_at else None,
            "performance": i.performance}


def _get(db: Session, model, id_: int):
    obj = db.get(model, id_)
    if not obj:
        raise HTTPException(404, f"{model.__name__} {id_} not found.")
    return obj


# ---------- routes ----------
@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version}


@app.get("/api/meta", dependencies=[Guarded])
def meta():
    return {"output_types": OUTPUT_TYPES, "publish_targets": publisher.available_targets(),
            "publishing_enabled": settings.publishing_enabled,
            "format_guide": FORMAT_GUIDE, "limits": guards.LIMITS, "scenarios": learning.SCENARIOS}


@app.get("/api/usage", dependencies=[Guarded])
def usage(db: Session = Depends(get_db)):
    return usage_summary(db)


@app.get("/api/projects", dependencies=[Guarded])
def list_projects(db: Session = Depends(get_db)):
    return [project_out(p) for p in db.scalars(select(Project).order_by(Project.name))]


@app.post("/api/projects", status_code=201, dependencies=[Guarded])
def create_project(body: ProjectIn, db: Session = Depends(get_db)):
    p = Project(**body.model_dump())
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A project called '{body.name}' already exists.")
    return project_out(p)


@app.patch("/api/projects/{pid}", dependencies=[Guarded])
def patch_project(pid: int, body: ProjectPatch, db: Session = Depends(get_db)):
    p = _get(db, Project, pid)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    db.commit()
    return project_out(p)


@app.delete("/api/projects/{pid}", status_code=204, dependencies=[Guarded])
def delete_project(pid: int, db: Session = Depends(get_db)):
    db.delete(_get(db, Project, pid))
    db.commit()


@app.post("/api/projects/{pid}/topics", status_code=201, dependencies=[Guarded])
def add_topic(pid: int, body: TopicIn, db: Session = Depends(get_db)):
    p = _get(db, Project, pid)
    db.add(Topic(project_id=p.id, **body.model_dump()))
    db.commit()
    db.refresh(p)
    return project_out(p)


@app.delete("/api/topics/{tid}", status_code=204, dependencies=[Guarded])
def delete_topic(tid: int, db: Session = Depends(get_db)):
    db.delete(_get(db, Topic, tid))
    db.commit()


@app.get("/api/projects/{pid}/quote", dependencies=[Guarded])
def quote(pid: int, db: Session = Depends(get_db)):
    """What a run will cost before you press go."""
    p = _get(db, Project, pid)
    topics = [t for t in p.topics if t.active]
    units = estimate_units(len(topics), [t.max_results for t in topics])
    if (p.mode or "content") == "learning":
        units += 1 + learning.gap_units(p.coverage)              # channel stats + gap searches
        calls = 1 + 1 + 1 + (1 if p.coverage else 0) + learning.MAX_COMMENTS
    else:
        calls = 2 + 2 * settings.briefs_per_run                  # cluster, design, then make + check per brief
    left = youtube_headroom(db)
    return {"topics": len(topics), "youtube_units": units, "youtube_units_left": left,
            "fits": bool(topics) and units <= left, "mode": p.mode or "content",
            "llm_calls": calls, "llm_run_cap_usd": settings.per_run_llm_budget_usd}


@app.post("/api/projects/{pid}/runs", status_code=202, dependencies=[Guarded])
def start_run(pid: int, db: Session = Depends(get_db)):
    _get(db, Project, pid)
    try:
        run = orchestrator.create_run(db, pid)
    except orchestrator.AlreadyRunning as e:
        raise HTTPException(409, str(e))
    orchestrator.start_background(run.id)
    return run_out(run)


@app.get("/api/runs", dependencies=[Guarded])
def list_runs(project_id: int | None = None, limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    q = select(Run).order_by(Run.id.desc()).limit(limit)
    if project_id:
        q = q.where(Run.project_id == project_id)
    return [run_out(r) for r in db.scalars(q)]


@app.get("/api/runs/{rid}", dependencies=[Guarded])
def get_run(rid: int, db: Session = Depends(get_db)):
    r = _get(db, Run, rid)
    trends = db.scalars(select(Trend).where(Trend.run_id == rid).order_by(Trend.score.desc()))
    return {
        **run_out(r),
        "events": [{"id": e.id, "agent": e.agent, "level": e.level, "message": e.message,
                    "ts": as_utc(e.ts).isoformat()}
                   for e in db.scalars(select(EventLog).where(EventLog.run_id == rid).order_by(EventLog.id))],
        "trends": [{"id": t.id, "score": t.score, "views_per_hour": t.views_per_hour,
                    "engagement_rate": t.engagement_rate, "theme": t.theme, "title": t.video.title,
                    "channel": t.video.channel_title, "views": t.video.views,
                    "url": f"https://www.youtube.com/watch?v={t.video.yt_id}"} for t in trends],
        "briefs": [{"id": b.id, "title": b.title, "angle": b.angle, "format": b.format,
                    "rationale": b.rationale, "source_trend_ids": b.source_trend_ids}
                   for b in db.scalars(select(Brief).where(Brief.run_id == rid))],
        "items": [item_out(i) for i in db.scalars(select(Item).where(Item.run_id == rid))],
        "learning": {
            "voices": db.scalar(select(func.count()).select_from(Voice).where(
                Voice.project_id == r.project_id, Voice.hidden.is_(False), Voice.political.is_(False))),
            "cards": db.scalar(select(func.count()).select_from(LensCard).where(LensCard.run_id == rid)),
            "gaps": db.scalar(select(func.count()).select_from(Gap).where(Gap.run_id == rid)),
            "comments": db.scalar(select(func.count()).select_from(CommentDraft).where(CommentDraft.run_id == rid)),
        },
    }


@app.get("/api/items", dependencies=[Guarded])
def list_items(status: str | None = None, project_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Item).order_by(Item.id.desc()).limit(200)
    if status:
        q = q.where(Item.status == status)
    if project_id:
        q = q.where(Item.project_id == project_id)
    return [item_out(i) for i in db.scalars(q)]


@app.post("/api/items/{iid}/approve", dependencies=[Guarded])
def approve(iid: int, body: ReviewIn, db: Session = Depends(get_db)):
    i = _get(db, Item, iid)
    if i.status != "pending_review":
        raise HTTPException(409, f"Item is '{i.status}', only pending_review items can be approved.")
    if body.title is not None or body.body is not None:
        title = body.title if body.title is not None else i.title
        text = body.body if body.body is not None else i.body
        p = db.get(Project, i.project_id)
        checks = guards.check_item(i.kind, title, text, list(p.banned_terms or []),
                                   run_source_texts(db, i.run_id))
        if not checks["passed"]:
            raise HTTPException(422, "Your edit fails checks: " + " ".join(checks["issues"]))
        i.title, i.body, i.checks = title, text, checks
    i.status, i.review_note = "approved", body.note
    db.add(EventLog(run_id=i.run_id, agent="you", message=f"Approved item {i.id}. {body.note}".strip()))
    db.commit()
    return item_out(i)


@app.post("/api/items/{iid}/reject", dependencies=[Guarded])
def reject(iid: int, body: ReviewIn, db: Session = Depends(get_db)):
    i = _get(db, Item, iid)
    if i.status not in ("pending_review", "blocked", "approved", "publishing"):
        raise HTTPException(409, f"Item is '{i.status}' and cannot be rejected.")
    i.status, i.review_note = "rejected", body.note
    db.add(EventLog(run_id=i.run_id, agent="you", message=f"Rejected item {i.id}. {body.note}".strip()))
    orchestrator.close_if_reviewed(db, i.run_id)
    db.commit()
    return item_out(i)


@app.post("/api/items/{iid}/publish", dependencies=[Guarded])
def publish_item(iid: int, body: PublishIn, db: Session = Depends(get_db)):
    i = _get(db, Item, iid)
    try:
        publisher.publish(db, i, body.target)
    except publisher.PublishError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    orchestrator.close_if_reviewed(db, i.run_id)
    db.commit()
    return item_out(i)


@app.post("/api/items/{iid}/performance", dependencies=[Guarded])
def record_performance(iid: int, body: PerformanceIn, db: Session = Depends(get_db)):
    """Feed results back so the Designer learns what works for this project."""
    i = _get(db, Item, iid)
    if i.status != "published":
        raise HTTPException(409, "Record results only on published items.")
    d = body.model_dump()
    d["score"] = d["views"] * 0.01 + d["clicks"] * 0.5 + d["reactions"] * 0.2 + d["leads"] * 10
    i.performance = d
    db.commit()
    return item_out(i)


# ================= learning mode =================
class BookIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(default="", max_length=300, pattern=r"^(https://\S+)?$")
    themes: list[str] = Field(default_factory=list, max_length=20)
    active: bool = True


class BookPatch(BaseModel):
    url: str | None = Field(default=None, max_length=300, pattern=r"^(https://\S+)?$")
    themes: list[str] | None = Field(default=None, max_length=20)
    active: bool | None = None


class SituationIn(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class FeedbackIn(BaseModel):
    helped: bool


class HideIn(BaseModel):
    hidden: bool = True


class CommentEdit(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


def card_out(c: LensCard) -> dict:
    return {"id": c.id, "voice": c.voice, "principle": c.principle, "why": c.why, "use_when": c.use_when,
            "avoid_when": c.avoid_when, "mantra": c.mantra, "scenarios": c.scenarios, "source_url": c.source_url,
            "uses": c.uses, "helped": c.helped, "archived": c.archived}


def voice_out(v: Voice) -> dict:
    return {"id": v.id, "vetted": bool(v.vetted), "title": v.title, "channel_url": f"https://www.youtube.com/channel/{v.channel_id}",
            "subscribers": v.subscribers, "recent_views": v.recent_views, "tier": v.tier,
            "political": v.political, "hidden": v.hidden, "focus": v.focus}


def comment_out(c: CommentDraft) -> dict:
    return {"id": c.id, "video_title": c.video_title, "video_url": f"https://www.youtube.com/watch?v={c.yt_id}",
            "channel_title": c.channel_title, "is_short": c.is_short, "body": c.body, "has_link": c.has_link,
            "book_id": c.book_id, "status": c.status}


def book_out(b: Book) -> dict:
    return {"id": b.id, "title": b.title, "url": b.url, "themes": b.themes, "active": b.active}


@app.get("/api/projects/{pid}/voices", dependencies=[Guarded])
def list_voices(pid: int, db: Session = Depends(get_db)):
    _get(db, Project, pid)
    q = select(Voice).where(Voice.project_id == pid).order_by(Voice.recent_views.desc())
    return [voice_out(v) for v in db.scalars(q)]


@app.post("/api/voices/{vid}/hide", dependencies=[Guarded])
def hide_voice(vid: int, body: HideIn, db: Session = Depends(get_db)):
    v = _get(db, Voice, vid)
    v.hidden = body.hidden
    if body.hidden:   # its cards drop out automatically; its open drafts go too
        for d in db.scalars(select(CommentDraft).where(CommentDraft.project_id == v.project_id,
                                                       CommentDraft.channel_id == v.channel_id,
                                                       CommentDraft.status == "draft")):
            d.status = "dismissed"
    db.commit()
    return voice_out(v)


@app.get("/api/projects/{pid}/cards", dependencies=[Guarded])
def list_cards(pid: int, archived: bool = False, db: Session = Depends(get_db)):
    _get(db, Project, pid)
    q = select(LensCard).where(LensCard.project_id == pid, LensCard.archived.is_(archived),
                               learning.usable_card_filter(db, pid)) \
        .order_by(LensCard.helped.desc(), LensCard.id.desc())
    return [card_out(c) for c in db.scalars(q)]


@app.post("/api/cards/{cid}/archive", dependencies=[Guarded])
def archive_card(cid: int, db: Session = Depends(get_db)):
    c = _get(db, LensCard, cid)
    c.archived = True
    db.commit()
    return card_out(c)


@app.post("/api/cards/{cid}/feedback", dependencies=[Guarded])
def card_feedback(cid: int, body: FeedbackIn, db: Session = Depends(get_db)):
    """After a real situation: did this card help? Cards that help rise to the top."""
    c = _get(db, LensCard, cid)
    c.uses = max(c.uses, c.helped + 1) if body.helped else c.uses + 1
    c.helped += 1 if body.helped else 0
    db.commit()
    return card_out(c)


@app.get("/api/projects/{pid}/daily", dependencies=[Guarded])
def daily_card(pid: int, db: Session = Depends(get_db)):
    """One mantra a day. Same card all day; cards that helped you come round more often."""
    from datetime import date
    _get(db, Project, pid)
    cards = list(db.scalars(select(LensCard).where(LensCard.project_id == pid, LensCard.archived.is_(False),
                                                   learning.usable_card_filter(db, pid))
                            .order_by(LensCard.id)))
    if not cards:
        return None
    pool = [c for c in cards for _ in range(1 + c.helped)]
    return card_out(pool[date.today().toordinal() % len(pool)])


@app.post("/api/projects/{pid}/situation", dependencies=[Guarded])
def situation(pid: int, body: SituationIn, db: Session = Depends(get_db)):
    from .ledger import BudgetExceeded
    from .llm import get_llm
    _get(db, Project, pid)
    try:
        res = learning.pick_for_situation(db, get_llm(), pid, body.text)
    except BudgetExceeded as e:
        raise HTTPException(429, str(e))
    return {"cards": [card_out(c) for c in res["cards"]], "tension": res["tension"],
            "first_move": res["first_move"]}


@app.get("/api/projects/{pid}/gaps", dependencies=[Guarded])
def list_gaps(pid: int, db: Session = Depends(get_db)):
    _get(db, Project, pid)
    last = db.scalar(select(func.max(Gap.run_id)).where(Gap.project_id == pid))
    q = select(Gap).where(Gap.project_id == pid, Gap.run_id == last).order_by(Gap.score.desc())
    return [{"id": g.id, "subject": g.subject, "demand": g.demand, "supply": g.supply, "score": g.score,
             "angle": g.angle, "format": g.format} for g in db.scalars(q)]


@app.get("/api/projects/{pid}/comments", dependencies=[Guarded])
def list_comments(pid: int, status: str = "draft", db: Session = Depends(get_db)):
    _get(db, Project, pid)
    q = select(CommentDraft).where(CommentDraft.project_id == pid, CommentDraft.status == status) \
        .order_by(CommentDraft.id.desc())
    return [comment_out(c) for c in db.scalars(q)]


@app.patch("/api/comments/{cid}", dependencies=[Guarded])
def edit_comment(cid: int, body: CommentEdit, db: Session = Depends(get_db)):
    c = _get(db, CommentDraft, cid)
    if c.status != "draft":
        raise HTTPException(409, "Only drafts can be edited.")
    links = learning.find_links(body.body)
    allowed = db.get(Book, c.book_id).url if (c.has_link and c.book_id and db.get(Book, c.book_id)) else None
    if links and (not allowed or any(u != allowed for u in links)):
        raise HTTPException(422, "Only the matched book link is allowed here, and only on drafts that were given "
                                 "one (not Shorts, and one per channel per 30 days). Remove the other link.")
    c.body = body.body
    db.commit()
    return comment_out(c)


@app.post("/api/comments/{cid}/posted", dependencies=[Guarded])
def mark_posted(cid: int, db: Session = Depends(get_db)):
    """You posted it yourself on YouTube. Recorded so the link cap counts it."""
    from datetime import datetime, timezone
    c = _get(db, CommentDraft, cid)
    if c.status != "draft":
        raise HTTPException(409, f"This comment is '{c.status}'.")
    c.status, c.posted_at = "posted_by_you", datetime.now(timezone.utc)
    db.commit()
    return comment_out(c)


@app.post("/api/comments/{cid}/dismiss", dependencies=[Guarded])
def dismiss_comment(cid: int, db: Session = Depends(get_db)):
    c = _get(db, CommentDraft, cid)
    c.status = "dismissed"
    db.commit()
    return comment_out(c)


@app.get("/api/books", dependencies=[Guarded])
def list_books(db: Session = Depends(get_db)):
    return [book_out(b) for b in db.scalars(select(Book).order_by(Book.title))]


@app.post("/api/books", status_code=201, dependencies=[Guarded])
def add_book(body: BookIn, db: Session = Depends(get_db)):
    b = Book(**body.model_dump())
    db.add(b)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"'{body.title}' is already listed.")
    return book_out(b)


@app.patch("/api/books/{bid}", dependencies=[Guarded])
def patch_book(bid: int, body: BookPatch, db: Session = Depends(get_db)):
    b = _get(db, Book, bid)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(b, k, v)
    db.commit()
    return book_out(b)


@app.delete("/api/books/{bid}", status_code=204, dependencies=[Guarded])
def delete_book(bid: int, db: Session = Depends(get_db)):
    db.delete(_get(db, Book, bid))
    db.commit()
