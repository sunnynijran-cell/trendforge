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
        quiet = ["--disable-background-networking", "--disable-component-update", "--disable-sync",
                 "--no-first-run", "--disable-default-apps", "--disable-features=AutofillServerCommunication"]
        try:
            browser = p.chromium.launch(args=quiet)
        except Exception:  # bundled browser missing: use a system Chromium (CHROMIUM_PATH)
            browser = p.chromium.launch(args=quiet,
                                        executable_path=os.getenv("CHROMIUM_PATH", "/opt/pw-browsers/chromium"))
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        # Google Fonts is blocked in the build sandbox; stub it so only app errors count.
        ctx.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, body="", content_type="text/css"))
        ctx.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=200, body=""))
        # Only the app under test may be reached; anything else is answered locally.
        ctx.route(lambda url: not url.startswith("http://127.0.0.1") and "fonts.g" not in url,
                  lambda r: r.fulfill(status=204, body=""))
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
    assert pg.inner_text("[data-testid=stage-scout] .n") == "11"
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


def test_learning_journey(server, page):
    base, tmp = server
    pg, errors = page
    EVIDENCE.mkdir(exist_ok=True)
    pg.goto(base)
    pg.get_by_role("tab", name="Books").click()
    pg.fill("#bk-title", "The Decision Gap")
    pg.fill("#bk-url", "https://example.com/decision-gap")
    pg.fill("#bk-themes", "decision, decisions, leaders")
    pg.click("#bookForm button[type=submit]")
    pg.wait_for_selector("[data-testid=book-row] >> text=The Decision Gap")

    pg.get_by_role("tab", name="Projects & topics").click()
    pg.fill("#np-name", "My learning")
    pg.fill("#np-desc", "Sales, influence and leadership")
    pg.fill("#np-aud", "Me, a consultant who sells and leads")
    pg.select_option("[data-testid=mode-select]", "learning")
    pg.fill("#np-cov", "decision latency\nprimavera p6")
    pg.click("#newProject button[type=submit]")
    pg.wait_for_selector("text=Created My learning")
    pg.fill("[data-testid=topic-input]", "decision making leadership")
    pg.click("#topicForm button[type=submit]")
    pg.wait_for_selector("[data-testid=topics] >> text=decision making leadership")
    # learning tabs appear, content-only review tab is hidden
    assert pg.get_by_role("tab", name="Lenses").is_visible()
    assert not pg.locator('nav button[data-tab="review"]').is_visible()

    pg.get_by_role("tab", name="Overview").click()
    pg.wait_for_selector("#quote >> text=304 YouTube units")    # 101 topic + 1 channels + 2 x 101 gaps
    pg.click("[data-testid=run-btn]")
    pg.wait_for_selector("[data-testid=run-status] >> text=done", timeout=20000)
    for k in ("scout", "voices", "analyst", "distil", "gaps", "comments"):
        assert int(pg.inner_text(f"[data-testid=stage-{k}] .n")) > 0, k
    pg.screenshot(path=EVIDENCE / "8-learning-overview.png", full_page=True)

    pg.get_by_role("tab", name="Lenses").click()
    pg.wait_for_selector("[data-testid=lens-card]")
    pg.fill("[data-testid=situation-input]", "the board keeps delaying a decision")
    pg.click("#sitForm button[type=submit]")
    pg.wait_for_selector("[data-testid=situation-out] >> text=First move")
    pg.locator("[data-testid=situation-out] [data-fb='1']").first.click()
    pg.wait_for_selector("text=Noted.")
    pg.screenshot(path=EVIDENCE / "9-lenses.png", full_page=True)

    pg.get_by_role("tab", name="Voices").click()
    pg.wait_for_selector("[data-testid=voice-row]")
    pg.get_by_role("tab", name="Gaps").click()
    pg.wait_for_selector("[data-testid=gap-row]")
    assert pg.locator("[data-testid=gap-row]").count() == 2
    pg.screenshot(path=EVIDENCE / "10-gaps.png", full_page=True)

    pg.get_by_role("tab", name="Comments").click()
    first = pg.locator("[data-testid^=comment-]").first
    first.wait_for()
    first.get_by_role("button", name="I posted it").click()
    pg.wait_for_selector("text=Done.")
    pg.screenshot(path=EVIDENCE / "11-comments.png", full_page=True)

    pg.set_viewport_size({"width": 390, "height": 844})
    pg.get_by_role("tab", name="Lenses").click()
    pg.wait_for_selector("[data-testid=lens-card]")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    pg.screenshot(path=EVIDENCE / "12-lenses-mobile.png", full_page=True)
    assert errors == [], errors
