from datetime import datetime, timedelta, timezone

import pytest

from app.agents import guards
from app.agents.analyst import pct_rank, score_videos
from app.config import settings
from app.db import Video
from app.llm import extract_json, model_for
from app.youtube import estimate_units, parse_duration


@pytest.mark.parametrize("iso,secs", [("PT1H2M3S", 3723), ("PT45S", 45), ("PT10M", 600),
                                      ("P1DT1S", 86401), ("", 0), ("garbage", 0)])
def test_parse_duration(iso, secs):
    assert parse_duration(iso) == secs


def test_estimate_units():
    assert estimate_units(0, []) == 0
    assert estimate_units(1, [25]) == 101
    assert estimate_units(3, [50, 50, 50]) == 303      # 150 ids -> 3 stats calls
    assert estimate_units(2, [99, 10]) == 202          # max_results capped at 50


def test_pct_rank_handles_ties_and_single():
    assert pct_rank([5.0]) == [1.0]
    assert pct_rank([1, 2, 3]) == [0.0, 0.5, 1.0]
    r = pct_rank([1, 1, 3])
    assert r[0] == r[1] == 0.25 and r[2] == 1.0


def _v(views, likes, comments, hours_ago, now):
    return Video(yt_id="x", title="t", views=views, likes=likes, comments=comments,
                 published_at=now - timedelta(hours=hours_ago))


def test_score_prefers_velocity_over_raw_views():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    old_big = _v(1_000_000, 10_000, 500, 24 * 13, now)   # ~3.2k views/hour
    new_hot = _v(200_000, 8_000, 900, 6, now)            # ~33k views/hour
    ranked = score_videos([old_big, new_hot], now=now)
    assert ranked[0][0] is new_hot
    assert 0 <= ranked[1][1] <= ranked[0][1] <= 1


def test_score_zero_views_no_crash():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    out = score_videos([_v(0, 0, 0, 2, now), _v(10, 1, 0, 2, now)], now=now)
    assert len(out) == 2 and out[1][3] == 0.0


def test_guards_pass_clean_item():
    r = guards.check_item("linkedin_post", "Title", "A clean, original body.", ["guaranteed"], [])
    assert r["passed"] and r["issues"] == []


def test_guards_banned_term_whole_word_only():
    assert not guards.check_item("linkedin_post", "t", "Results guaranteed.", ["guaranteed"], [])["passed"]
    assert guards.check_item("linkedin_post", "t", "Unguaranteedness is not a word.", ["guaranteed"], [])["passed"]


def test_guards_length_limit():
    r = guards.check_item("x_post", "t", "x" * 281, [], [])
    assert not r["passed"] and "Too long" in r["issues"][0]


def test_guards_copyright_overlap():
    src = "The hidden truth about project controls careers nobody tells you about in interviews."
    body = "Here is the hidden truth about project controls careers nobody tells you about. Then more."
    r = guards.check_item("linkedin_post", "t", body, [], [src])
    assert not r["passed"] and "Copies 8+" in r["issues"][0]
    ok = guards.check_item("linkedin_post", "t", "Project controls careers have a hidden side.", [], [src])
    assert ok["passed"]


def test_guards_empty_and_impersonation():
    assert "Empty body." in guards.check_item("email", "t", "  ", [], [])["issues"]
    assert not guards.check_item("email", "t", "Hi, I'm MrBeast and I approve.", [], [])["passed"]


@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! {"a": 1} hope that helps'])
def test_extract_json(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_rejects_prose():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_model_routing_never_uses_opus():
    assert model_for("cluster") == settings.model_fast
    assert model_for("check") == settings.model_fast
    assert model_for("design") == settings.model_smart
    assert model_for("make") == settings.model_smart
    assert "opus" not in settings.model_fast + settings.model_smart


def test_relevance_filter_drops_off_topic():
    from types import SimpleNamespace as NS
    from app.agents.scout import is_relevant
    p6 = NS(title="Cost loading in Primavera P6", tags=[], description="")
    board = NS(title="HDHMR board for interior work", tags=["interior"], description="Tips")
    stuffed = NS(title="HDHMR board for interior work", tags=["primavera", "p6"], description="#p6 #primavera")
    arabic = NS(title="كورس التخطيط والبريمافيرا P6", tags=[], description="")
    assert is_relevant("primavera p6", p6) and is_relevant("primavera p6", arabic)
    assert not is_relevant("primavera p6", board)
    assert not is_relevant("primavera p6", stuffed)   # tag stuffing does not count
    assert is_relevant("", board)
    assert is_relevant("decision making", NS(title="Why big decisions take too long", tags=[], description=""))
    assert not is_relevant("p6", NS(title="P60 tax form explained", tags=[], description=""))
    assert not is_relevant("sales psychology", NS(title="egg selling Mindset #youtubeshorts #psychology",
                                                  tags=[], description=""))
    assert is_relevant("sales psychology", NS(title="The psychology of a yes #sales", tags=[], description=""))


def test_fake_model_books_no_spend_on_a_keyless_deploy(db):
    from dataclasses import replace
    from app.llm import FakeLLM
    from app.ledger import llm_spend_month
    FakeLLM(replace(settings, offline_override="")).json(db, "check", "s", "p", None)
    assert llm_spend_month(db) == 0
