from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import Settings, settings
from ..db import EventLog, Project, Run, utcnow
from ..llm import LLM
from ..youtube import YouTubeSource


@dataclass
class Ctx:
    db: Session
    run: Run
    project: Project
    yt: YouTubeSource
    llm: LLM
    cfg: Settings = settings

    def log(self, agent: str, message: str, level: str = "info") -> None:
        self.db.add(EventLog(run_id=self.run.id, agent=agent, level=level, message=message[:2000]))
        self.run.heartbeat_at = utcnow()   # proves this run is alive to other instances
        self.db.commit()  # dashboard sees progress live
