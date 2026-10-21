import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="tf-test-"))
os.environ["OFFLINE"] = "1"
# Set TEST_DATABASE_URL to a throwaway Postgres to run the suite against Postgres.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'test.db'}"
os.environ["EXPORTS_DIR"] = str(_TMP / "exports")
os.environ.pop("ADMIN_TOKEN", None)
os.environ["PUBLISHING_ENABLED"] = "1"   # existing publish tests; the off switch has its own test

import pytest  # noqa: E402

from app.db import Base, Project, SessionLocal, Topic, engine  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def project(db):
    p = Project(name="Meridian LinkedIn", description="Thought leadership for Meridian Advisory",
                audience="Owners and PMO heads on nuclear and mining megaprojects",
                brand_rules="UK spelling. Short sentences. No hype.",
                output_types=["linkedin_post", "youtube_short_script"], banned_terms=["guaranteed"])
    p.topics = [Topic(query="primavera p6 project controls", max_results=25, lookback_days=14),
                Topic(query="decision making leadership", max_results=25, lookback_days=14)]
    db.add(p)
    db.commit()
    return p
