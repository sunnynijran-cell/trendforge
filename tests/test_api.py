"""HTTP API, end to end, including the approval gate and publishing."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _project(client, **kw):
    body = {"name": "Kev Bangkok", "description": "Comedy expat training", "audience": "New expats in Thailand",
            "output_types": ["youtube_short_script", "x_post"], "banned_terms": ["guaranteed"], **kw}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _wait(client, rid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{rid}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_health_and_meta(client):
    assert client.get("/api/health").json()["ok"]
    m = client.get("/api/meta").json()
    assert "linkedin_post" in m["output_types"] and m["publish_targets"] == ["export"]


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "TrendForge" in r.text


def test_project_validation(client):
    assert client.post("/api/projects", json={"name": ""}).status_code == 422
    assert client.post("/api/projects", json={"name": "x", "output_types": ["tiktok_dance"]}).status_code == 422
    _project(client)
    assert client.post("/api/projects", json={"name": "Kev Bangkok"}).status_code == 409
    p = client.get("/api/projects").json()[0]
    assert client.post(f"/api/projects/{p['id']}/topics", json={"query": "x"}).status_code == 422
    assert client.post(f"/api/projects/{p['id']}/topics", json={"query": "ok q", "region": "gb"}).status_code == 422
    assert client.post(f"/api/projects/{p['id']}/topics", json={"query": "ok q", "max_results": 51}).status_code == 422
    assert client.get("/api/projects/999").status_code in (404, 405)
    assert client.get("/api/runs/999").status_code == 404


def test_quote_before_and_after_topics(client):
    p = _project(client)
    q = client.get(f"/api/projects/{p['id']}/quote").json()
    assert q["topics"] == 0 and q["fits"] is False
    client.post(f"/api/projects/{p['id']}/topics", json={"query": "bangkok expat life"})
    q = client.get(f"/api/projects/{p['id']}/quote").json()
    assert q["youtube_units"] == 101 and q["fits"] is True


def test_full_flow_run_review_publish_feedback(client):
    p = _project(client)
    client.post(f"/api/projects/{p['id']}/topics", json={"query": "bangkok expat life", "region": "TH"})
    r = client.post(f"/api/projects/{p['id']}/runs")
    assert r.status_code == 202
    run = _wait(client, r.json()["id"])
    assert run["status"] == "awaiting_approval", run["error"]
    assert run["trends"] and run["briefs"] and run["items"]
    assert all(t["url"].startswith("https://www.youtube.com/watch?v=") for t in run["trends"])

    usage = client.get("/api/usage").json()
    assert usage["youtube_units_today"] == 101 and usage["llm_mode"] == "offline fake model"

    pending = client.get(f"/api/items?status=pending_review&project_id={p['id']}").json()
    assert len(pending) == len(run["items"])
    a, b = pending[0], pending[1]

    # publishing before approval is refused
    assert client.post(f"/api/items/{a['id']}/publish", json={"target": "export"}).status_code == 409
    # an edit that breaks the rules is refused
    bad = client.post(f"/api/items/{a['id']}/approve", json={"body": "Results guaranteed."})
    assert bad.status_code == 422 and "Banned terms" in bad.json()["detail"]
    # a clean edit is accepted
    ok = client.post(f"/api/items/{a['id']}/approve", json={"body": "Day one in Bangkok. Buy a Rabbit card."})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.post(f"/api/items/{a['id']}/approve", json={}).status_code == 409   # no double approve
    # webhook not configured -> refused
    assert client.post(f"/api/items/{a['id']}/publish", json={"target": "webhook"}).status_code == 409
    pub = client.post(f"/api/items/{a['id']}/publish", json={"target": "export"}).json()
    assert pub["status"] == "published"
    f = Path(pub["publish_ref"])
    assert f.exists() and "Rabbit card" in f.read_text()

    rej = client.post(f"/api/items/{b['id']}/reject", json={"note": "off brand"}).json()
    assert rej["status"] == "rejected"
    assert client.post(f"/api/items/{b['id']}/performance", json={"views": 10}).status_code == 409

    perf = client.post(f"/api/items/{a['id']}/performance", json={"views": 1000, "clicks": 4, "leads": 1}).json()
    assert perf["performance"]["score"] == pytest.approx(1000 * 0.01 + 4 * 0.5 + 10)

    # every item now reviewed (1 published, 1 rejected, rest rejected below) -> run closes
    for it in client.get(f"/api/items?status=pending_review&project_id={p['id']}").json():
        client.post(f"/api/items/{it['id']}/reject", json={})
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "done"

    log = client.get(f"/api/runs/{run['id']}").json()["events"]
    assert any(e["agent"] == "you" and "Approved" in e["message"] for e in log)
    assert any(e["agent"] == "publisher" for e in log)


def test_second_run_rejected_while_running(client, monkeypatch):
    p = _project(client)
    client.post(f"/api/projects/{p['id']}/topics", json={"query": "bangkok expat"})
    monkeypatch.setattr(main.orchestrator, "start_background", lambda rid, **kw: None)  # leave it queued
    assert client.post(f"/api/projects/{p['id']}/runs").status_code == 202
    r = client.post(f"/api/projects/{p['id']}/runs")
    assert r.status_code == 409 and "already in progress" in r.json()["detail"]


def test_admin_token_enforced(client, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(main, "settings", replace(settings, admin_token="s3cret"))
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/api/projects", headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert client.get("/api/health").status_code == 200        # health stays open for Render


def test_delete_project_cascades(client):
    p = _project(client)
    client.post(f"/api/projects/{p['id']}/topics", json={"query": "bangkok expat"})
    rid = client.post(f"/api/projects/{p['id']}/runs").json()["id"]
    _wait(client, rid)
    assert client.delete(f"/api/projects/{p['id']}").status_code == 204
    assert client.get(f"/api/runs/{rid}").status_code == 404
    assert client.get("/api/items").json() == []


def test_approve_edit_with_copied_text_refused(client):
    """Review finding: a reviewer edit must still pass the copyright check."""
    p = _project(client, output_types=["linkedin_post"])
    client.post(f"/api/projects/{p['id']}/topics", json={"query": "project controls career", "max_results": 50})
    run = _wait(client, client.post(f"/api/projects/{p['id']}/runs").json()["id"])
    item = next(i for i in run["items"] if i["status"] == "pending_review")
    copied = "The hidden truth about project controls careers nobody tells you about in interviews."
    r = client.post(f"/api/items/{item['id']}/approve", json={"body": copied})
    assert r.status_code == 422 and "Copies 8+" in r.json()["detail"]
