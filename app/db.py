"""Database models. SQLite locally, Postgres (e.g. Supabase) in production.

Point DATABASE_URL at a Postgres connection string and the same models work.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    """A project need: who it is for, what to make, and the rules it must follow."""
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(Text, default="")
    brand_rules: Mapped[str] = mapped_column(Text, default="")
    output_types: Mapped[list] = mapped_column(JSON, default=lambda: ["linkedin_post"])
    banned_terms: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    topics: Mapped[list["Topic"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(2), default="GB")
    max_results: Mapped[int] = mapped_column(Integer, default=25)
    lookback_days: Mapped[int] = mapped_column(Integer, default=14)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    project: Mapped[Project] = relationship(back_populates="topics")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="queued")  # queued|running|awaiting_approval|done|failed
    stage: Mapped[str] = mapped_column(String(30), default="scout")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str] = mapped_column(Text, default="")
    quota_used: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"), nullable=True)
    yt_id: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(Text)
    channel_title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[int] = mapped_column(Integer, default=0)


class Trend(Base):
    __tablename__ = "trends"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    views_per_hour: Mapped[float] = mapped_column(Float)
    engagement_rate: Mapped[float] = mapped_column(Float)
    theme: Mapped[str] = mapped_column(String(120), default="unclustered")
    video: Mapped[Video] = relationship()


class Brief(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    angle: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(60))
    rationale: Mapped[str] = mapped_column(Text, default="")
    source_trend_ids: Mapped[list] = mapped_column(JSON, default=list)


class Item(Base):
    """A made thing awaiting your decision. Status flow:
    blocked (failed checks) | pending_review -> approved -> published, or rejected."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("briefs.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending_review")
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    review_note: Mapped[str] = mapped_column(Text, default="")
    publish_target: Mapped[str] = mapped_column(String(60), default="")
    publish_ref: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    performance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventLog(Base):
    """Bottom-up view: every agent step, in order."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=True)
    agent: Mapped[str] = mapped_column(String(30))
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Usage(Base):
    """Ledger for both cost lines: YouTube quota units and Claude spend."""
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(20))  # youtube | llm
    units: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    model: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[str] = mapped_column(String(200), default="")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Reservation(Base):
    """Claude spend held for calls in flight. Lives in the database so every
    instance sees every other instance's in-flight calls."""
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usd: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


def make_engine(url: str | None = None):
    url = url or settings.database_url
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}  # Supabase drops idle connections
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # enforce cascades in SQLite
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return eng


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(eng=None) -> None:
    eng = eng or engine
    if settings.require_postgres and eng.url.get_backend_name() != "postgresql":
        raise RuntimeError("REQUIRE_POSTGRES=1 but DATABASE_URL is not Postgres. The cost ledger must "
                           "live in a database that survives deploys. Set DATABASE_URL to Supabase.")
    Base.metadata.create_all(eng)


def run_source_texts(db, run_id: int) -> list[str]:
    """Every source video's words in a run, for the copyright check."""
    from sqlalchemy import select
    return [f"{t}\n{d}" for t, d in db.execute(select(Video.title, Video.description).where(Video.run_id == run_id))]


def as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read. Treat naive datetimes as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
