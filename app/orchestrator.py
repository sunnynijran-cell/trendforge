"""Orchestrator: runs the team in order and stops at your approval gate.

scout -> analyst -> designer -> maker -> [YOU APPROVE] -> publisher
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .agents import analyst, designer, learning, maker, scout
from .agents.base import Ctx
from .config import Settings, settings
from .db import EventLog, Item, LensCard, Project, Run, SessionLocal, Usage, as_utc
from .ledger import llm_spend_run
from .llm import LLM, get_llm
from .youtube import YouTubeSource, get_source

# (name, function, required). A required stage that produces nothing stops the run.
STAGES = [("scout", scout.run, True), ("analyst", analyst.run, True),
          ("designer", designer.run, True), ("maker", maker.run, True)]
LEARNING_STAGES = [("scout", scout.run, True), ("voices", learning.run_voices, True),
                   ("analyst", analyst.run, True), ("distil", learning.run_distil, True),
                   ("gaps", learning.run_gaps, False), ("comments", learning.run_comments, False)]


def stages_for(project: Project):
    return LEARNING_STAGES if (project.mode or "content") == "learning" else STAGES
STALE_AFTER = timedelta(minutes=10)   # no heartbeat for this long = the run is dead
HEARTBEAT_EVERY = 20                  # seconds, from a side thread, so slow model calls never look dead
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


class AlreadyRunning(RuntimeError):
    pass


def _lock_for(project_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def recover_interrupted(db: Session) -> int:
    """On startup, fail runs whose heartbeat has stopped (a restart or deploy
    killed them). Runs with a fresh heartbeat may belong to another instance
    during a zero-downtime deploy, so they are left alone; create_run times them
    out later if they really are dead. Items caught mid-publish go back to approved."""
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_AFTER
    n = 0
    for run in db.scalars(select(Run).where(Run.status.in_(("queued", "running")))):
        if as_utc(run.heartbeat_at) < cutoff:
            run.status, run.finished_at = "failed", now
            run.error = "Interrupted by a restart or deploy. Start a new run."
            n += 1
    db.execute(update(Item).where(Item.status == "publishing").values(status="approved"))
    db.commit()
    return n


def create_run(db: Session, project_id: int) -> Run:
    active = db.scalar(select(Run).where(Run.project_id == project_id, Run.status.in_(("queued", "running"))))
    if active and datetime.now(timezone.utc) - as_utc(active.heartbeat_at) > STALE_AFTER:
        active.status, active.error = "failed", "Stopped: no progress for 3 minutes."
        active.finished_at = datetime.now(timezone.utc)
        db.commit()
        active = None
    if active:
        raise AlreadyRunning(f"Run {active.id} is already in progress for this project.")
    run = Run(project_id=project_id, status="queued", stage="scout")
    db.add(run)
    db.commit()
    return run


def close_if_reviewed(db: Session, run_id: int) -> None:
    """A run is done once nothing in it is waiting for you or waiting to publish."""
    run = db.get(Run, run_id)
    if not run or run.status != "awaiting_approval":
        return
    open_items = db.scalar(select(func.count()).select_from(Item).where(
        Item.run_id == run_id, Item.status.in_(("pending_review", "approved", "publishing"))))
    if open_items == 0:
        run.status, run.stage = "done", "done"
        db.add(EventLog(run_id=run_id, agent="orchestrator", message="Every item reviewed. Run closed."))


def _heartbeat(run_id: int, session_factory, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_EVERY):
        try:
            with session_factory() as hb:
                hb.execute(update(Run).where(Run.id == run_id).values(heartbeat_at=datetime.now(timezone.utc)))
                hb.commit()
        except Exception:
            pass   # a missed beat is harmless; the cut-off allows many


def execute(run_id: int, session_factory=SessionLocal, yt: YouTubeSource | None = None,
            llm: LLM | None = None, cfg: Settings = settings) -> Run:
    with session_factory() as db:
        run = db.get(Run, run_id)
        project = db.get(Project, run.project_id)
        lock = _lock_for(project.id)
        if not lock.acquire(blocking=False):
            run.status, run.error = "failed", "Another run holds the project lock."
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            return run
        stop = threading.Event()
        beat = threading.Thread(target=_heartbeat, args=(run_id, session_factory, stop), daemon=True)
        beat.start()
        try:
            try:
                ctx = Ctx(db=db, run=run, project=project, yt=yt or get_source(cfg),
                          llm=llm or get_llm(cfg), cfg=cfg)
                run.status = "running"
                db.commit()
                for name, fn, required in stages_for(project):
                    run.stage = name
                    db.commit()
                    if required:
                        n = fn(ctx)
                    else:
                        try:                 # optional stage: a failure here never sinks the run
                            n = fn(ctx)
                        except Exception as e:
                            db.rollback()
                            ctx.log(name, f"Skipped: {type(e).__name__}: {e}", "warn")
                            n = 0
                    db.commit()
                    if n == 0 and required:
                        raise RuntimeError(f"{name} produced nothing, so the pipeline stopped. See the run log.")
                run.status = "awaiting_approval"
                run.stage = "review"
                close_if_reviewed(db, run.id)   # learning runs, or every item blocked: straight to done
                # ctx.log commits, so the final status is written once; no in-between state is visible
                if (project.mode or "content") == "learning":
                    ctx.log("orchestrator", "Lens cards, gaps and comment drafts are ready. Nothing is posted.")
                else:
                    ctx.log("orchestrator", "Items are in your review queue. Nothing publishes until you approve.")
            except Exception as e:
                db.rollback()
                run = db.get(Run, run_id)
                msg = f"{type(e).__name__}: {e}"[:2000]
                made = db.scalar(select(func.count()).select_from(Item).where(Item.run_id == run.id)) + \
                    db.scalar(select(func.count()).select_from(LensCard).where(LensCard.run_id == run.id))
                if made:   # e.g. budget hit mid-Maker: keep what was made reviewable
                    run.status, run.stage = "awaiting_approval", "review"
                    run.error = f"Partial run, {made} item(s) made before it stopped. {msg}"
                else:
                    run.status, run.error = "failed", msg
                db.add(EventLog(run_id=run.id, agent="orchestrator", level="error", message=run.error))
            run.quota_used = int(db.scalar(select(func.coalesce(func.sum(Usage.units), 0))
                                           .where(Usage.kind == "youtube", Usage.run_id == run.id)))
            run.llm_cost_usd = round(llm_spend_run(db, run.id), 6)
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            stop.set()
            lock.release()   # always, even if the bookkeeping above fails
        return run


def start_background(run_id: int, **kw) -> threading.Thread:
    t = threading.Thread(target=execute, args=(run_id,), kwargs=kw, daemon=True)
    t.start()
    return t
