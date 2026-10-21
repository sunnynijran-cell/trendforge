"""Real browser test of the dashboard: a user creates a project, runs the team,
approves and publishes. Screenshots land in evidence/ as proof."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server():
    tmp = Path(tempfile.mkdtemp(prefix="tf-e2e-"))
    port = _free_port()
    env = {**os.environ, "OFFLINE": "1", "DATABASE_URL": f"sqlite:///{tmp/'e2e.db'}",
           "EXPORTS_DIR": str(tmp / "exports"), "ADMIN_TOKEN": ""}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(proc.stdout.read().decode())
    yield base, tmp
    proc.terminate()
    proc.wait(10)


@pytest.fixture
def page(server):
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:  # bundled browser missing: use a system Chromium (CHROMIUM_PATH)
            browser = p.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH", "/opt/pw-browsers/chromium"))
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        # Google Fonts is blocked in the build sandbox; stub it so only app errors count.
        ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, body="", content_type="text/css"))
        ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=200, body=""))
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.on("dialog", lambda d: d.accept("looks off brand"))
        yield pg, errors
        browser.close()


def test_user_journey(server, page):
    base, tmp = server
    pg, errors = page
    EVIDENCE.mkdir(exist_ok=True)
    pg.goto(base)
    pg.get_by_role("tab", name="Projects & topics").click()

    pg.fill("#np-name", "Meridian LinkedIn")
    pg.fill("#np-desc", "Thought leadership for Meridian Advisory")
    pg.fill("#np-aud", "PMO heads on nuclear and mining megaprojects")
    pg.fill("#np-banned", "guaranteed, game-changer")
    pg.click("#newProject button[type=submit]")
    pg.wait_for_selector("text=Created Meridian LinkedIn")

    pg.fill("[data-testid=topic-input]", "primavera p6 project controls")
    pg.click("#topicForm button[type=submit]")
    pg.wait_for_selector("[data-testid=topics] >> text=primavera p6 project controls")
    pg.fill("[data-testid=topic-input]", "decision making leadership")
    pg.click("#topicForm button[type=submit]")
    pg.wait_for_selector("[data-testid=topics] >> text=decision making leadership")
    pg.screenshot(path=EVIDENCE / "1-project-setup.png", full_page=True)

    pg.get_by_role("tab", name="Overview").click()
    pg.wait_for_selector("#quote >> text=201 YouTube units")
    pg.click("[data-testid=run-btn]")
    pg.wait_for_selector("[data-testid=run-status] >> text=awaiting approval", timeout=20000)
    assert pg.inner_text("[data-testid=stage-scout] .n") == "17"
    assert pg.inner_text("[data-testid=stage-analyst] .n") == "10"
    assert pg.inner_text("[data-testid=stage-designer] .n") == "3"
    assert pg.inner_text("[data-testid=stage-review] .n") == "3"
    assert pg.inner_text("[data-testid=stage-publisher] .n") == "0"
    assert pg.inner_text("[data-testid=yt-used]") == "201"
    pg.screenshot(path=EVIDENCE / "2-overview-top-down.png", full_page=True)

    pg.get_by_role("tab", name="Trends").click()
    pg.wait_for_selector("[data-testid=trend-row]")
    assert pg.locator("[data-testid=trend-row]").count() == 10
    pg.screenshot(path=EVIDENCE / "3-trends.png", full_page=True)

    pg.get_by_role("tab", name="Review queue").click()
    cards = pg.locator("[data-status=pending_review]")
    cards.first.wait_for()
    assert cards.count() == 3
    first = cards.first
    first.locator("textarea").fill("Your schedule is not late. Your decisions are. One fix: decide weekly.")
    first.get_by_role("button", name="Approve").click()
    pg.wait_for_selector("text=Done.")
    pg.locator("[data-status=pending_review]").first.get_by_role("button", name="Reject").click()
    pg.wait_for_function("document.querySelectorAll('[data-status=pending_review]').length === 1")
    pg.wait_for_function("document.querySelector('#pendingCount').textContent === '1'")
    pg.screenshot(path=EVIDENCE / "4-review-queue.png", full_page=True)

    pg.select_option("[data-testid=item-filter]", "approved")
    pg.locator("[data-status=approved]").first.get_by_role("button", name="Publish").click()
    pg.select_option("[data-testid=item-filter]", "published")
    pg.wait_for_selector("[data-status=published] >> text=Published via export")
    exported = list((tmp / "exports").glob("*.md"))
    assert len(exported) == 1 and "decide weekly" in exported[0].read_text()
    pg.screenshot(path=EVIDENCE / "5-published.png", full_page=True)

    pg.get_by_role("tab", name="Run log").click()
    pg.wait_for_selector("[data-testid=log-list] li")
    log = pg.inner_text("[data-testid=log-list]").lower()
    for agent in ("scout", "analyst", "designer", "maker", "orchestrator", "you", "publisher"):
        assert agent in log
    pg.screenshot(path=EVIDENCE / "6-run-log-bottom-up.png", full_page=True)

    # phone width, no sideways scroll
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.get_by_role("tab", name="Overview").click()
    pg.wait_for_selector("[data-testid=stage-scout]")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    pg.screenshot(path=EVIDENCE / "7-mobile.png", full_page=True)

    assert errors == [], errors
