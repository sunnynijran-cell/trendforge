"""Learning and influence mode: voices, lens cards, gaps, comment drafts, situations."""
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, orchestrator
from app.agents import learning, publisher
from app.config import settings
from app.db import Book, CommentDraft, Gap, LensCard, Project, SessionLocal, Topic, Voice
from app.llm import FakeLLM
from app.youtube import FixtureYouTube


@pytest.fixture
def learner(db):
    p = Project(name="My learning", description="Sales, influence and leadership for my own growth",
                audience="Sunny, a consultant who sells and leads", mode="learning",
                output_types=["linkedin_post"], banned_terms=["guaranteed"],
                coverage=["decision latency", "primavera p6"])
    p.topics = [Topic(query="decision making leadership", max_results=25, lookback_days=14),
                Topic(query="ai agents automation", max_results=25, lookback_days=14)]
    db.add(p)
    db.add(Book(title="The Decision Gap", url="https://example.com/decision-gap",
                themes=["decision", "decisions", "leaders"]))
    db.commit()
    return p


def _run(db, project, llm=None, cfg=settings):
    run = orchestrator.create_run(db, project.id)
    llm = llm or FakeLLM(cfg)
    return orchestrator.execute(run.id, yt=FixtureYouTube(cfg), llm=llm, cfg=cfg), llm


def test_full_learning_run(db, learner):
    run, llm = _run(db, learner)
    assert run.status == "done", run.error            # nothing to approve: learning runs close themselves
    assert llm.calls[:4] == ["vet", "cluster", "distil", "angle"]
    assert set(llm.calls[4:]) <= {"comment"}
    with SessionLocal() as s:
        voices = s.scalars(select(Voice).where(Voice.project_id == learner.id)).all()
        cards = s.scalars(select(LensCard).where(LensCard.project_id == learner.id)).all()
        gaps = s.scalars(select(Gap).where(Gap.project_id == learner.id)).all()
        drafts = s.scalars(select(CommentDraft).where(CommentDraft.project_id == learner.id)).all()
    assert voices and all(v.tier in ("rising", "established") for v in voices)
    assert cards and all(len(c.mantra.split()) <= 8 for c in cards)
    assert all(c.source_url.startswith("https://www.youtube.com/watch?v=") for c in cards)
    assert {g.subject for g in gaps} == {"decision latency", "primavera p6"}
    assert all(g.score == round(g.demand / (1 + g.supply), 2) for g in gaps)
    assert 0 < len(drafts) <= learning.MAX_COMMENTS
    for d in drafts:
        has_url = "https://" in d.body
        assert has_url == d.has_link                      # a link only where one is allowed
        if d.has_link:
            assert not d.is_short and "https://example.com/decision-gap" in d.body
            assert d.body.count("https://") == 1
    # quota: 2 searches + 1 stats + 1 channels + 2 gap searches + 2 gap stats
    assert run.quota_used == 2 * 100 + 1 + 1 + 2 * 101


def test_link_cap_one_per_channel_per_30_days(db, learner):
    _run(db, learner)
    _run(db, learner)
    with SessionLocal() as s:
        linked = s.scalars(select(CommentDraft).where(CommentDraft.has_link.is_(True))).all()
        all_drafts = s.scalars(select(CommentDraft)).all()
    chans = [d.channel_id for d in linked]
    assert len(chans) == len(set(chans))                   # never two links on one channel
    assert len({d.yt_id for d in all_drafts}) == len(all_drafts)   # never two drafts for one video


def test_shorts_never_get_a_link(db, learner):
    from types import SimpleNamespace
    from app.agents.base import Ctx
    ctx = Ctx(db=db, run=SimpleNamespace(id=None), project=learner, yt=None, llm=None)
    assert learning.link_allowed(ctx, "UCx", is_short=True) is False
    assert learning.link_allowed(ctx, "UCx", is_short=False) is True


def test_political_channels_are_dropped(db, learner):
    class Partisan(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "vet":
                out["channels"][0]["political"] = True
                self.flagged = out["channels"][0]["channel_id"]
            return out
    llm = Partisan(settings)
    run, _ = _run(db, learner, llm=llm)
    with SessionLocal() as s:
        v = s.scalar(select(Voice).where(Voice.channel_id == llm.flagged))
        assert v.political
        assert not s.scalars(select(LensCard).where(LensCard.voice == v.title)).all()
        assert not s.scalars(select(CommentDraft).where(CommentDraft.channel_id == llm.flagged)).all()


def test_long_mantras_and_copied_wording_are_dropped(db, learner):
    class Sloppy(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "distil":
                good = dict(out["cards"][0])
                long = {**good, "mantra": "one two three four five six seven eight nine"}
                copied = {**good, "principle": "In this video we cover why big decisions take too long "
                                               "and what to do about it"}
                out["cards"] = [good, long, copied]
            return out
    run, _ = _run(db, learner, llm=Sloppy(settings))
    with SessionLocal() as s:
        assert len(s.scalars(select(LensCard)).all()) == 1


def test_no_coverage_skips_gaps_but_run_completes(db, learner):
    learner.coverage = []
    db.commit()
    run, llm = _run(db, learner)
    assert run.status == "done" and "angle" not in llm.calls


def test_comment_without_book_has_no_link(db, learner):
    for b in db.scalars(select(Book)):
        db.delete(b)
    db.commit()
    _run(db, learner)
    with SessionLocal() as s:
        assert all("https://" not in d.body and not d.has_link for d in s.scalars(select(CommentDraft)))


def test_tier_rules():
    assert learning.classify_tier(50_000, 30_000) == "rising"
    assert learning.classify_tier(50_000, 5_000) == "established"
    assert learning.classify_tier(900_000, 900_000) == "established"
    assert learning.classify_tier(0, 10_000) == "established"     # hidden subscriber count


# ---------- API ----------
@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _wait(client, rid):
    for _ in range(150):
        r = client.get(f"/api/runs/{rid}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_learning_api_flow(client):
    r = client.post("/api/projects", json={"name": "Lenses", "mode": "learning", "description": "Influence",
                                           "coverage": ["decision latency"]})
    assert r.status_code == 201 and r.json()["mode"] == "learning"
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/topics", json={"query": "decision making leadership"})
    book = client.post("/api/books", json={"title": "The Decision Gap", "url": "https://example.com/dg",
                                           "themes": ["decision", "decisions"]})
    assert book.status_code == 201
    assert client.post("/api/books", json={"title": "Bad", "url": "javascript:alert(1)"}).status_code == 422
    q = client.get(f"/api/projects/{pid}/quote").json()
    assert q["youtube_units"] == 101 + 1 + 101 and q["mode"] == "learning"
    run = _wait(client, client.post(f"/api/projects/{pid}/runs").json()["id"])
    assert run["status"] == "done", run["error"]

    voices = client.get(f"/api/projects/{pid}/voices").json()
    cards = client.get(f"/api/projects/{pid}/cards").json()
    gaps = client.get(f"/api/projects/{pid}/gaps").json()
    drafts = client.get(f"/api/projects/{pid}/comments").json()
    assert voices and cards and gaps and drafts

    sit = client.post(f"/api/projects/{pid}/situation", json={"text": "client says the decision is stalled"})
    assert sit.status_code == 200 and 0 < len(sit.json()["cards"]) <= 4 and sit.json()["first_move"]
    used = sit.json()["cards"][0]
    fb = client.post(f"/api/cards/{used['id']}/feedback", json={"helped": True}).json()
    assert fb["helped"] == 1 and fb["uses"] >= 1

    d1 = client.get(f"/api/projects/{pid}/daily").json()
    assert d1 and d1["id"] == client.get(f"/api/projects/{pid}/daily").json()["id"]   # stable all day

    d = drafts[0]
    if not d["has_link"]:
        assert client.patch(f"/api/comments/{d['id']}", json={"body": "See https://spam.example"}).status_code == 422
        assert client.patch(f"/api/comments/{d['id']}", json={"body": "Read it at bit.ly/xyz"}).status_code == 422
    else:
        assert client.patch(f"/api/comments/{d['id']}", json={"body": "Also www.other.com"}).status_code == 422
    assert client.patch(f"/api/comments/{d['id']}", json={"body": "Edited by me."}).json()["body"] == "Edited by me."
    assert client.post(f"/api/comments/{d['id']}/posted").json()["status"] == "posted_by_you"
    assert client.patch(f"/api/comments/{d['id']}", json={"body": "x"}).status_code == 409
    assert client.post(f"/api/comments/{d['id']}/posted").status_code == 409            # not twice

    # hiding a voice removes its cards everywhere and dismisses its open drafts
    target = next(v for v in voices if any(c["voice"] == v["title"] for c in cards))
    client.post(f"/api/voices/{target['id']}/hide", json={"hidden": True})
    after = client.get(f"/api/projects/{pid}/cards").json()
    assert all(c["voice"] != target["title"] for c in after)
    daily = client.get(f"/api/projects/{pid}/daily").json()
    assert daily is None or daily["voice"] != target["title"]
    sit2 = client.post(f"/api/projects/{pid}/situation", json={"text": "decision stalled"}).json()
    assert all(c["voice"] != target["title"] for c in sit2["cards"])
    left = client.get(f"/api/projects/{pid}/comments").json()
    assert all(c["channel_title"] != target["title"] for c in left)
    if after:
        assert client.post(f"/api/cards/{after[0]['id']}/archive").json()["archived"] is True


def test_blank_coverage_is_ignored_in_quote(client):
    r = client.post("/api/projects", json={"name": "B", "mode": "learning", "coverage": [" ", "", "  real  "]})
    assert r.json()["coverage"] == ["real"]
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/topics", json={"query": "decision making"})
    assert client.get(f"/api/projects/{pid}/quote").json()["youtube_units"] == 101 + 1 + 101


def test_publishing_off_switch(client, db, monkeypatch):
    off = replace(settings, publishing_enabled=False)
    monkeypatch.setattr(main, "settings", off)
    monkeypatch.setattr(publisher, "settings", off)
    assert publisher.available_targets(off) == []
    from app.db import Brief, Item, Run
    p = Project(name="P")
    db.add(p)
    db.flush()
    r = Run(project_id=p.id)
    db.add(r)
    db.flush()
    b = Brief(run_id=r.id, project_id=p.id, title="t", angle="a", format="linkedin_post")
    db.add(b)
    db.flush()
    i = Item(brief_id=b.id, project_id=p.id, run_id=r.id, kind="linkedin_post", title="t", body="b",
             status="approved")
    db.add(i)
    db.commit()
    with pytest.raises(publisher.PublishError, match="switched off"):
        publisher.publish(db, i, "export", off)
    db.refresh(i)
    assert i.status == "approved"


def test_additive_migration_adds_new_columns(tmp_path):
    """Start from the full schema with the new columns removed (the live database's
    shape before learning mode), then migrate."""
    from sqlalchemy import create_engine, inspect, text
    from app.db import Base, _add_missing_columns
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        for t, col in [("projects", "mode"), ("projects", "coverage"), ("videos", "channel_id"),
                       ("voices", "vetted"), ("lens_cards", "channel_id")]:
            c.execute(text(f"ALTER TABLE {t} DROP COLUMN {col}"))
    _add_missing_columns(eng)
    _add_missing_columns(eng)          # idempotent
    insp = inspect(eng)
    assert {"mode", "coverage"} <= {c["name"] for c in insp.get_columns("projects")}
    assert "channel_id" in {c["name"] for c in insp.get_columns("videos")}
    assert "vetted" in {c["name"] for c in insp.get_columns("voices")}


def test_migration_refuses_missing_not_null_column(tmp_path):
    from sqlalchemy import create_engine, text
    from app.db import _add_missing_columns
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE projects (id INTEGER PRIMARY KEY, name VARCHAR(120))"))
    with pytest.raises(RuntimeError, match="NOT NULL"):
        _add_missing_columns(eng)


# ---------- review findings ----------
@pytest.mark.parametrize("text", ["see https://x.com/a", "www.amazon.co.uk/dp/1", "bit.ly/abc", "amazon.com",
                                  "go to example.co.uk/book now"])
def test_find_links_catches_every_form(text):
    assert learning.find_links(text)


@pytest.mark.parametrize("text", ["e.g. this works", "Decision latency costs 3.5 weeks", "P6 v24.1"])
def test_find_links_ignores_plain_text(text):
    assert learning.find_links(text) == []


def test_model_cannot_sneak_a_bare_link_into_a_no_link_draft(db, learner):
    for b in db.scalars(select(Book)):
        db.delete(b)
    db.commit()

    class Sneaky(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "comment":
                out["body"] += " Grab it at www.mybook.com or bit.ly/mybook"
            return out
    _run(db, learner, llm=Sneaky(settings))
    with SessionLocal() as s:
        drafts = s.scalars(select(CommentDraft)).all()
    assert drafts and all(learning.find_links(d.body) == [] for d in drafts)


def test_shorts_and_unknown_length_never_get_a_link_in_a_run(db, learner, monkeypatch):
    import app.youtube as yt
    orig = yt._to_stats

    def short(item):
        s = orig(item)
        s.duration_s = 0 if hash(s.yt_id) % 2 else 45     # unknown or a Short
        return s
    monkeypatch.setattr(yt, "_to_stats", short)
    _run(db, learner)
    with SessionLocal() as s:
        drafts = s.scalars(select(CommentDraft)).all()
    assert drafts and all(d.is_short and not d.has_link for d in drafts)


def test_link_cap_counts_from_posting_date(db, learner):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.agents.base import Ctx
    old = CommentDraft(project_id=learner.id, yt_id="v1", video_title="t", channel_id="UCz", body="b",
                       has_link=True, status="posted_by_you",
                       created_at=datetime.now(timezone.utc) - timedelta(days=40),
                       posted_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(old)
    db.commit()
    ctx = Ctx(db=db, run=SimpleNamespace(id=None), project=learner, yt=None, llm=None)
    assert learning.link_allowed(ctx, "UCz", False) is False       # posted yesterday: blocked
    old.posted_at = datetime.now(timezone.utc) - timedelta(days=31)
    db.commit()
    assert learning.link_allowed(ctx, "UCz", False) is True        # outside 30 days: allowed


def test_failed_vetting_holds_voices_back(db, learner):
    class Broken(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            if task == "vet":
                raise RuntimeError("model down")
            return super().json(db, task, system, prompt, run_id)
    run, _ = _run(db, learner, llm=Broken(settings))
    assert run.status == "failed" and "voices produced nothing" in run.error   # fails closed
    with SessionLocal() as s:
        assert not s.scalars(select(LensCard)).all()
        assert all(not v.vetted for v in s.scalars(select(Voice)))


def test_previous_political_flag_survives_a_missing_verdict(db, learner):
    class Partisan(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "vet":
                out["channels"][0]["political"] = True
            return out

    class Forgetful(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            out = super().json(db, task, system, prompt, run_id)
            if task == "vet":
                for c in out["channels"]:
                    c["political"] = False
            return out
    _run(db, learner, llm=Partisan(settings))
    _run(db, learner, llm=Forgetful(settings))
    with SessionLocal() as s:
        assert s.scalars(select(Voice).where(Voice.political.is_(True))).all()     # never loosened


def test_optional_stage_failure_does_not_fail_the_run(db, learner):
    class GapsDown(FakeLLM):
        def json(self, db, task, system, prompt, run_id):
            if task == "comment":
                from app.ledger import BudgetExceeded
                raise BudgetExceeded("cap")
            return super().json(db, task, system, prompt, run_id)
    run, _ = _run(db, learner, llm=GapsDown(settings))
    assert run.status == "done", run.error
