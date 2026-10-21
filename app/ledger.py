"""Cost control. Every YouTube call and every Claude call is booked here
before results are used, and checked against caps before it is made."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from .config import Settings, settings
from .db import Reservation, Usage

PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaExceeded(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def youtube_day_start(now: datetime | None = None) -> datetime:
    """YouTube quota resets at midnight Pacific time."""
    now = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def youtube_units_today(db: Session) -> int:
    q = select(func.coalesce(func.sum(Usage.units), 0)).where(
        Usage.kind == "youtube", Usage.ts >= youtube_day_start())
    return int(db.execute(q).scalar_one())


def llm_spend_month(db: Session) -> float:
    q = select(func.coalesce(func.sum(Usage.cost_usd), 0.0)).where(
        Usage.kind == "llm", Usage.ts >= month_start())
    return float(db.execute(q).scalar_one())


def llm_spend_run(db: Session, run_id: int) -> float:
    q = select(func.coalesce(func.sum(Usage.cost_usd), 0.0)).where(
        Usage.kind == "llm", Usage.run_id == run_id)
    return float(db.execute(q).scalar_one())


def youtube_headroom(db: Session, cfg: Settings = settings) -> int:
    return cfg.youtube_daily_quota - cfg.youtube_quota_reserve - youtube_units_today(db)


# One process (one Render worker) runs every agent, so a process-wide lock makes
# "check headroom, then book" atomic across concurrent runs on different projects.
_ledger_lock = threading.RLock()


def _db_lock(db: Session) -> None:
    """On Postgres, also serialise across instances (e.g. overlapping deploys).
    The lock is released when the transaction commits."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(731100)"))


def charge_youtube(db: Session, units: int, detail: str, run_id: int | None, cfg: Settings = settings) -> None:
    with _ledger_lock:
        _db_lock(db)
        left = youtube_headroom(db, cfg)
        if units > left:
            raise QuotaExceeded(
                f"YouTube quota: need {units} units, {max(left, 0)} left today "
                f"(cap {cfg.youtube_daily_quota}, reserve {cfg.youtube_quota_reserve}). Resets midnight Pacific.")
        db.add(Usage(kind="youtube", units=units, detail=detail[:200], run_id=run_id))
        db.commit()  # spend is real even if a later step fails


def llm_cost(model: str, in_tok: int, out_tok: int, cfg: Settings = settings) -> float:
    if model == cfg.model_smart:
        return in_tok / 1e6 * cfg.price_smart_in + out_tok / 1e6 * cfg.price_smart_out
    return in_tok / 1e6 * cfg.price_fast_in + out_tok / 1e6 * cfg.price_fast_out


RESERVATION_TTL = timedelta(minutes=15)   # a crashed call's hold expires on its own


@contextmanager
def llm_reservation(db: Session, run_id: int | None, worst_usd: float, cfg: Settings = settings):
    """Hold the worst-case cost of a call against both caps before making it.
    Holds live in the database, so concurrent calls on any instance cannot
    jointly overshoot. Released once the real cost is booked."""
    with _ledger_lock:
        _db_lock(db)
        live = Reservation.ts >= datetime.now(timezone.utc) - RESERVATION_TTL
        held_all = float(db.scalar(select(func.coalesce(func.sum(Reservation.usd), 0.0)).where(live)))
        month = llm_spend_month(db)
        if month + held_all + worst_usd > cfg.monthly_llm_budget_usd:
            db.rollback()
            raise BudgetExceeded(f"Monthly Claude budget would be exceeded: ${month:.2f} spent, "
                                 f"${held_all + worst_usd:.2f} needed, cap ${cfg.monthly_llm_budget_usd:.2f}.")
        if run_id is not None:
            held_run = float(db.scalar(select(func.coalesce(func.sum(Reservation.usd), 0.0))
                                       .where(live, Reservation.run_id == run_id)))
            spent = llm_spend_run(db, run_id)
            if spent + held_run + worst_usd > cfg.per_run_llm_budget_usd:
                db.rollback()
                raise BudgetExceeded(f"Per-run Claude budget would be exceeded: ${spent:.2f} spent, "
                                     f"next call up to ${worst_usd:.3f}, cap ${cfg.per_run_llm_budget_usd:.2f}.")
        hold = Reservation(run_id=run_id, usd=worst_usd)
        db.add(hold)
        db.commit()   # publishes the hold and releases the Postgres advisory lock before the slow call
        hold_id = hold.id
    try:
        yield
    finally:
        db.execute(delete(Reservation).where(Reservation.id == hold_id))
        db.commit()


def charge_llm(db: Session, model: str, in_tok: int, out_tok: int, task: str,
               run_id: int | None, cfg: Settings = settings) -> float:
    cost = llm_cost(model, in_tok, out_tok, cfg)
    db.add(Usage(kind="llm", units=in_tok + out_tok, cost_usd=round(cost, 6),
                 model=model, detail=task, run_id=run_id))
    db.commit()  # spend is real even if a later step fails
    return cost


def usage_summary(db: Session, cfg: Settings = settings) -> dict:
    used = youtube_units_today(db)
    spend = llm_spend_month(db)
    reset = youtube_day_start() + timedelta(days=1)
    return {
        "youtube_units_today": used,
        "youtube_daily_quota": cfg.youtube_daily_quota,
        "youtube_reserve": cfg.youtube_quota_reserve,
        "youtube_resets_at": reset.isoformat(),
        "llm_spend_month_usd": round(spend, 4),
        "llm_monthly_budget_usd": cfg.monthly_llm_budget_usd,
        "per_run_llm_budget_usd": cfg.per_run_llm_budget_usd,
        "model_fast": cfg.model_fast,
        "model_smart": cfg.model_smart,
        "youtube_mode": "offline fixtures" if cfg.youtube_offline else "live API",
        "llm_mode": "offline fake model" if cfg.llm_offline else "live Claude API",
    }
