"""Whole-team runs against recorded YouTube data and the fake model."""
from dataclasses import replace

from sqlalchemy import func, select

from app import orchestrator
from app.config import settings
from app.db import Brief, EventLog, Item, Run, SessionLocal, Trend, Usage, Video
from app.ledger import charge_youtube
from app.llm import FakeLLM
from app.youtube import FixtureYouTube


def _run(db, project, cfg=settings, llm=None, yt=None):
    run = orchestrator.create_run(db, project.id)
    llm = llm or FakeLLM(cfg)
    return orchestrator.execute(run.id, yt=yt or FixtureYouTube(cfg), llm=llm, cfg=cfg), llm


def test_full_run_reaches_approval_gate(db, project):
    run, llm = _run(db, project)
    assert run.status == "awaiting_approval", run.error
    assert run.stage == "review"
    with SessionLocal() as s:
        videos = s.scalar(select(func.count()).select_from(Video).where(Video.run_id == run.id))
        trends = s.scalars(select(Trend).where(Trend.run_id == run.id).order_by(Trend.id)).all()
        briefs = s.scalars(select(Brief).where(Brief.run_id == run.id)).all()
        items = s.scalars(select(Item).where(Item.run_id == run.id)).all()
        # Search matches 17 unique fixture videos on title + tags, but only 9 carry a
        # search word in their TITLE. The relevance filter keeps those 9.
        assert videos == 9
        assert len(trends) == min(settings.top_trends, videos)
        assert all(0 <= t.score <= 1 for t in trends)
        scores = [t.score for t in trends]
        assert scores == sorted(scores, reverse=True)   # stored best-first
        assert len(briefs) == settings.briefs_per_run
        assert all(b.format in project.output_types for b in briefs)
        assert len(items) == len(briefs)
        assert all(i.status == "pending_review" for i in items), [i.checks for i in items]
        assert not any(i.status == "published" for i in items)   # nothing ships without approval


def test_run_books_exact_quota_and_routes_models(db, project):
    run, llm = _run(db, project)
    assert run.quota_used == 2 * 100 + 1       # 2 searches + 1 stats batch
    assert llm.calls == ["cluster", "design", "make", "check", "make", "check", "make", "check"]
    with SessionLocal() as s:
        models = dict(s.execute(select(Usage.detail, Usage.model).where(Usage.kind == "llm")).all())
        assert models["cluster"] == settings.model_fast
        assert models["design"] == settings.model_smart
        assert models["make"] == settings.model_smart
        assert models["check"] == settings.model_fast
    assert run.llm_cost_usd > 0


def test_events_give_bottom_up_trail(db, project):
    run, _ = _run(db, project)
    with SessionLocal() as s:
        agents = [e.agent for e in s.scalars(select(EventLog).where(EventLog.run_id == run.id).order_by(EventLog.id))]
    for a in ("scout", "analyst", "designer", "maker", "orchestrator"):
        assert a in agents
    assert agents.index("scout") < agents.index("analyst") < agents.index("designer") < agents.index("maker")


def test_no_topics_fails_cleanly(db, project):
    project.topics.clear()
    db.commit()
    run, llm = _run(db, project)
    assert run.status == "failed" and "No active topics" in run.error
    assert llm.calls == []


def test_quota_exhausted_stops_before_spending(db, project):
    charge_youtube(db, settings.youtube_daily_quota - settings.youtube_quota_reserve - 50, "earlier", None)
    run, llm = _run(db, project)
    assert run.status == "failed" and "QuotaExceeded" in run.error
    assert run.quota_used == 0 and llm.calls == []


def test_budget_cap_stops_run_but_keeps_ledger(db, project):
    # The cheap Haiku cluster call fits under $0.02 worst case; the Sonnet design
    # call's worst case does not, so it is refused BEFORE it is made.
    cfg = replace(settings, per_run_llm_budget_usd=0.02)
    run, llm = _run(db, project, cfg=cfg)
    assert run.status == "failed" and "BudgetExceeded" in run.error
    assert llm.calls == ["cluster"]
    assert 0 < run.llm_cost_usd <= 0.02       # spend survived the rollback and stayed under cap
    assert run.quota_used == 201


def test_cap_refuses_first_call_if_worst_case_too_big(db, project):
    cfg = replace(settings, per_run_llm_budget_usd=0.0001)
    run, llm = _run(db, project, cfg=cfg)
    assert run.status == "failed" and llm.calls == [] and run.llm_cost_usd == 0


def test_monthly_cap_counts_concurrent_reservations(db):
    from app.ledger import BudgetExceeded, llm_reservation
    cfg = replace(settings, monthly_llm_budget_usd=1.0)
    with llm_reservation(db, None, 0.6, cfg):
        try:
            with llm_reservation(db, None, 0.6, cfg):
                assert False, "second reservation should be refused"
        except BudgetExceeded:
            pass
    with llm_reservation(db, None, 0.6, cfg):   # released, so fits again
        pass


def test_copied_wording_is_blocked(db, project):
    """Fixture video fx000000003 has this sentence in its description. If the Maker
    lifts it, the guard must block the item so it never reaches review."""
    cfg = replace(settings, top_trends=100)

    class CopyCat(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "cluster":
                ids = [t["trend_id"] for th in out["themes"] for t in [{"trend_id": i} for i in th["trend_ids"]]]
                out = {"themes": [{"name": "All", "trend_ids": ids, "why": "test"}]}
            if task == "design":
                out["briefs"] = [{**b, "source_trend_ids": payload_ids(prompt)} for b in out["briefs"]]
            if task == "make":
                out["body"] = ("The hidden truth about project controls careers nobody tells you about "
                               "in interviews. That is all.")
            return out

    def payload_ids(prompt):
        import json
        return json.loads(prompt.split("DATA:", 1)[1])["themes"][0]["trend_ids"]

    run, _ = _run(db, project, cfg=cfg, llm=CopyCat(cfg))
    assert run.status == "done", run.error        # every item blocked, nothing left to review
    with SessionLocal() as s:
        yt_ids = set(s.scalars(select(Video.yt_id).join(Trend, Trend.video_id == Video.id)
                               .where(Trend.run_id == run.id)))
        items = s.scalars(select(Item).where(Item.run_id == run.id)).all()
    assert "fx000000003" in yt_ids
    assert items and all(i.status == "blocked" for i in items)
    assert all(any("Copies 8+" in m for m in i.checks["issues"]) for i in items)


def test_bad_format_from_model_falls_back(db, project):
    class Wrong(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "design":
                for b in out["briefs"]:
                    b["format"] = "tiktok_dance"
                    b["source_trend_ids"] = [999999]
            return out
    run, _ = _run(db, project, llm=Wrong(settings))
    assert run.status == "awaiting_approval"
    with SessionLocal() as s:
        for b in s.scalars(select(Brief).where(Brief.run_id == run.id)):
            assert b.format == project.output_types[0]
            assert b.source_trend_ids == []          # invented ids are dropped


def test_second_run_blocked_while_first_active(db, project):
    first = orchestrator.create_run(db, project.id)
    try:
        orchestrator.create_run(db, project.id)
        assert False, "expected AlreadyRunning"
    except orchestrator.AlreadyRunning:
        pass
    orchestrator.execute(first.id, yt=FixtureYouTube(), llm=FakeLLM())
    again = orchestrator.create_run(db, project.id)      # allowed once the first finishes
    assert again.id != first.id


def test_all_items_blocked_closes_run(db, project):
    class Blocky(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "make":
                out["body"] = "Results guaranteed."
            return out
    run, _ = _run(db, project, llm=Blocky(settings))
    assert run.status == "done"


def test_haiku_editor_notes_are_advisory(db, project):
    """Review finding: one false flag from the cheap model must not kill a good item."""
    class Strict(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            return {"ok": False, "issues": ["Promises an outcome."]} if task == "check" else out
    run, _ = _run(db, project, llm=Strict(settings))
    with SessionLocal() as s:
        items = s.scalars(select(Item).where(Item.run_id == run.id)).all()
    assert items and all(i.status == "pending_review" for i in items)
    assert items[0].checks["editor_notes"] == ["Editor: Promises an outcome."]


def test_editor_string_false_is_not_a_flag(db, project):
    class Odd(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            return {"ok": "false", "issues": ["x"]} if task == "check" else out
    run, _ = _run(db, project, llm=Odd(settings))
    with SessionLocal() as s:
        assert all(i.checks["editor_notes"] == [] for i in s.scalars(select(Item).where(Item.run_id == run.id)))


def test_budget_hit_mid_maker_keeps_items_reviewable(db, project):
    class Spendy(FakeLLM):
        n = 0
        def json(self, db, task, system, prompt, run_id):
            if task == "make":
                Spendy.n += 1
                if Spendy.n == 2:
                    from app.ledger import BudgetExceeded
                    raise BudgetExceeded("Per-run Claude budget would be exceeded")
            return super().json(db, task, system, prompt, run_id)
    run, _ = _run(db, project, llm=Spendy(settings))
    assert run.status == "awaiting_approval" and run.error.startswith("Partial run, 1 item")


def test_copy_caught_even_when_designer_cites_nothing(db, project):
    """Review finding: guards must check every video in the run, not only cited ones."""
    cfg = replace(settings, top_trends=100)

    class NoCite(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "design":
                for b in out["briefs"]:
                    b["source_trend_ids"] = []
            if task == "make":
                out["body"] = "The hidden truth about project controls careers nobody tells you about in interviews."
            return out
    project.topics[0].query = "project controls career"
    db.commit()
    run, _ = _run(db, project, cfg=cfg, llm=NoCite(cfg))
    with SessionLocal() as s:
        items = s.scalars(select(Item).where(Item.run_id == run.id)).all()
    assert items and all(i.status == "blocked" for i in items)


def test_malformed_model_output_does_not_crash(db, project):
    class Junk(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "cluster":
                return ["not", "a", "dict"]
            if task == "design":
                return {"briefs": ["junk", {"title": "Good", "angle": "A", "format": "linkedin_post",
                                            "source_trend_ids": "abc"}]}
            return out
    run, _ = _run(db, project, llm=Junk(settings))
    assert run.status == "awaiting_approval", run.error
    with SessionLocal() as s:
        b = s.scalars(select(Brief).where(Brief.run_id == run.id)).all()
    assert len(b) == 1 and b[0].source_trend_ids == []


def test_restart_recovery_unlocks_project(db, project):
    from datetime import datetime, timedelta, timezone
    stuck = orchestrator.create_run(db, project.id)
    stuck.status = "running"
    db.commit()
    assert orchestrator.recover_interrupted(db) == 0      # fresh heartbeat: may be another live instance
    stuck.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    db.commit()
    assert orchestrator.recover_interrupted(db) == 1
    db.refresh(stuck)
    assert stuck.status == "failed" and "Interrupted" in stuck.error
    assert orchestrator.create_run(db, project.id).id != stuck.id


def test_stale_run_times_out(db, project):
    from datetime import datetime, timedelta, timezone
    stuck = orchestrator.create_run(db, project.id)
    stuck.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    db.commit()
    assert orchestrator.create_run(db, project.id).id != stuck.id


def test_lock_released_even_if_bookkeeping_fails(db, project, monkeypatch):
    run = orchestrator.create_run(db, project.id)
    monkeypatch.setattr(orchestrator, "llm_spend_run", lambda *a: (_ for _ in ()).throw(RuntimeError("db gone")))
    try:
        orchestrator.execute(run.id, yt=FixtureYouTube(), llm=FakeLLM())
    except RuntimeError:
        pass
    assert not orchestrator._lock_for(project.id).locked()


def test_stuck_publishing_reset_on_startup(db, project):
    run, _ = _run(db, project)
    with SessionLocal() as s:
        it = s.scalars(select(Item).where(Item.run_id == run.id)).first()
        it.status = "publishing"
        s.commit()
        orchestrator.recover_interrupted(s)
        s.refresh(it)
        assert it.status == "approved"


def test_heartbeat_keeps_slow_run_alive(db, project, monkeypatch):
    """Review finding: a healthy run with slow model calls must not look dead."""
    import time
    monkeypatch.setattr(orchestrator, "HEARTBEAT_EVERY", 0.05)
    seen = []

    class Slow(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)   # books + commits, like the real client
            if task == "make":
                with SessionLocal() as s:
                    seen.append(s.get(Run, run_id).heartbeat_at)
                time.sleep(0.3)        # a slow call, with no transaction held
            return out
    _run(db, project, llm=Slow(settings))
    assert len(seen) == 3 and seen[0] < seen[1] < seen[2]   # beat advanced during silent stretches


def test_reservation_visible_to_other_sessions(db):
    """Holds live in the database, so another instance (session) sees them."""
    from app.ledger import BudgetExceeded, llm_reservation
    cfg = replace(settings, monthly_llm_budget_usd=1.0)
    with llm_reservation(db, None, 0.7, cfg):
        with SessionLocal() as other:
            try:
                with llm_reservation(other, None, 0.7, cfg):
                    assert False, "other session should see the hold"
            except BudgetExceeded:
                pass
    with SessionLocal() as other:
        with llm_reservation(other, None, 0.7, cfg):   # released
            pass
