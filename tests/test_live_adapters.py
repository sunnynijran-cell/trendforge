"""The live YouTube, Claude and webhook adapters, tested against mocked HTTP
so the real request shapes, parsing, retries and cost booking are proven
without spending quota or credits."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.agents import publisher
from app.config import settings
from app.db import Item, Usage
from app.ledger import QuotaExceeded
from app.llm import ClaudeLLM
from app.youtube import LiveYouTube

CFG = replace(settings, youtube_api_key="test-key", anthropic_api_key="sk-test")


def _yt(handler):
    return LiveYouTube(CFG, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_live_youtube_search_and_videos(db):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "abc"}}, {"id": {"kind": "channel"}}]})
        return httpx.Response(200, json={"items": [{
            "id": "abc", "snippet": {"publishedAt": "2026-10-20T10:00:00Z", "title": "T", "channelTitle": "C",
                                     "description": "D", "tags": ["x"]},
            "statistics": {"viewCount": "1000", "commentCount": "5"},   # likes hidden by creator
            "contentDetails": {"duration": "PT4M2S"}}]})

    yt = _yt(handler)
    after = datetime.now(timezone.utc) - timedelta(days=7)
    ids = yt.search(db, "p6 scheduling", "GB", 80, after, None)
    assert ids == ["abc"]
    q = seen[0].url.params
    assert q["type"] == "video" and q["order"] == "viewCount" and q["regionCode"] == "GB"
    assert q["maxResults"] == "50" and q["publishedAfter"].endswith("Z")
    assert "key" not in q and seen[0].headers["X-Goog-Api-Key"] == "test-key"   # never in the URL
    stats = yt.videos(db, ids, None)
    assert stats[0].views == 1000 and stats[0].likes == 0 and stats[0].duration_s == 242
    assert seen[1].url.params["part"] == "snippet,statistics,contentDetails"
    units = [u.units for u in db.scalars(select(Usage).where(Usage.kind == "youtube"))]
    assert units == [100, 1]


def test_live_youtube_retries_5xx_then_succeeds(db):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"items": []})

    assert _yt(handler).search(db, "x y", "GB", 5, datetime.now(timezone.utc), None) == []
    assert calls["n"] == 3
    # Google bills failed calls too, so the ledger books all three attempts
    assert [u.units for u in db.scalars(select(Usage).where(Usage.kind == "youtube"))] == [100, 100, 100]


def test_youtube_error_never_leaks_key(db):
    body = {"error": {"message": "API key not valid. Please pass a valid API key.",
                      "errors": [{"reason": "badRequest"}]}}
    yt = _yt(lambda req: httpx.Response(400, json=body))
    with pytest.raises(RuntimeError) as e:
        yt.search(db, "x y", "GB", 5, datetime.now(timezone.utc), None)
    assert "test-key" not in str(e.value) and "googleapis" not in str(e.value)
    assert "API key not valid" in str(e.value)


def test_live_youtube_google_quota_error(db):
    yt = _yt(lambda req: httpx.Response(403, text='{"error":{"errors":[{"reason":"quotaExceeded"}]}}'))
    with pytest.raises(QuotaExceeded):
        yt.search(db, "x y", "GB", 5, datetime.now(timezone.utc), None)


def test_live_youtube_batches_50(db):
    sizes = []

    def handler(req):
        sizes.append(len(req.url.params["id"].split(",")))
        return httpx.Response(200, json={"items": []})

    _yt(handler).videos(db, [f"v{i}" for i in range(120)], None)
    assert sizes == [50, 50, 20]


class _FakeMessages:
    def __init__(self, replies):
        self.replies, self.kwargs = list(replies), []

    def create(self, **kw):
        self.kwargs.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
                               content=[SimpleNamespace(type="text", text=text)])


def _claude(replies):
    llm = ClaudeLLM(CFG)
    llm.client = SimpleNamespace(messages=_FakeMessages(replies))
    return llm


def test_claude_routes_models_and_books_cost(db):
    llm = _claude(['{"themes": []}', '```json\n{"briefs": []}\n```'])
    assert llm.json(db, "cluster", "sys", "p", None) == {"themes": []}
    assert llm.json(db, "design", "sys", "p", None) == {"briefs": []}
    sent = llm.client.messages.kwargs
    assert sent[0]["model"] == settings.model_fast and sent[1]["model"] == settings.model_smart
    costs = [u.cost_usd for u in db.scalars(select(Usage).where(Usage.kind == "llm").order_by(Usage.id))]
    assert costs[0] == pytest.approx(1000 / 1e6 * CFG.price_fast_in + 500 / 1e6 * CFG.price_fast_out)
    assert costs[1] == pytest.approx(1000 / 1e6 * CFG.price_smart_in + 500 / 1e6 * CFG.price_smart_out)


def test_claude_retries_once_on_bad_json_then_fails(db):
    llm = _claude(["not json", '{"ok": true}'])
    assert llm.json(db, "check", "s", "p", None) == {"ok": True}
    assert "not valid JSON" in llm.client.messages.kwargs[1]["messages"][0]["content"]
    llm = _claude(["nope", "still nope"])
    with pytest.raises(RuntimeError, match="did not return JSON"):
        llm.json(db, "check", "s", "p", None)


def _approved_item(db, project):
    from app.db import Brief, Run
    run = Run(project_id=project.id)
    db.add(run)
    db.flush()
    b = Brief(run_id=run.id, project_id=project.id, title="b", angle="a", format="linkedin_post")
    db.add(b)
    db.flush()
    i = Item(brief_id=b.id, project_id=project.id, run_id=run.id, kind="linkedin_post",
             title="Hello", body="World", status="approved")
    db.add(i)
    db.commit()
    return i


def test_webhook_publish(db, project):
    got = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: got.append(r) or httpx.Response(200)))
    cfg = replace(settings, webhook_url="https://hooks.example.com/x")
    item = publisher.publish(db, _approved_item(db, project), "webhook", cfg, http=http)
    assert item.status == "published" and item.publish_ref == "webhook 200"
    assert b'"title":"Hello"' in got[0].content.replace(b" ", b"")


def test_webhook_failure_does_not_mark_published(db, project):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down")))
    cfg = replace(settings, webhook_url="https://hooks.example.com/x")
    item = _approved_item(db, project)
    with pytest.raises(publisher.PublishError, match="500"):
        publisher.publish(db, item, "webhook", cfg, http=http)
    assert item.status == "approved"


def test_double_publish_sends_once(db, project):
    from app.db import SessionLocal
    got = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: got.append(r) or httpx.Response(200)))
    cfg = replace(settings, webhook_url="https://hooks.example.com/x")
    item = _approved_item(db, project)
    with SessionLocal() as other:                 # a second tab holding a stale copy
        stale = other.get(Item, item.id)
        publisher.publish(db, item, "webhook", cfg, http=http)
        db.commit()
        with pytest.raises(publisher.PublishError):
            publisher.publish(other, stale, "webhook", cfg, http=http)
    assert len(got) == 1


def test_config_blank_env_and_postgres_urls(monkeypatch):
    from app.config import _s, normalise_db_url
    monkeypatch.setenv("SOME_BLANK", "")
    assert _s("SOME_BLANK", "fallback") == "fallback"
    assert normalise_db_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert normalise_db_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert normalise_db_url("sqlite:///x.db") == "sqlite:///x.db"


def test_require_postgres_refuses_sqlite(monkeypatch):
    import app.db as dbmod
    if dbmod.engine.url.get_backend_name() == "postgresql":
        pytest.skip("suite is running on Postgres")
    monkeypatch.setattr(dbmod, "settings", replace(settings, require_postgres=True))
    with pytest.raises(RuntimeError, match="REQUIRE_POSTGRES"):
        dbmod.init_db()


def test_routing_follows_env_overrides():
    from app.llm import model_for
    cfg = replace(settings, model_fast="claude-haiku-x", model_smart="claude-sonnet-y")
    assert model_for("cluster", cfg) == "claude-haiku-x" and model_for("make", cfg) == "claude-sonnet-y"
