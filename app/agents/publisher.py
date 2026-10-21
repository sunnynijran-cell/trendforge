"""Publisher: ships approved items only. Nothing goes out without your approval.

Targets:
  export   - writes a Markdown file to EXPORTS_DIR (always available)
  webhook  - POSTs the item as JSON to PUBLISH_WEBHOOK_URL (Zapier, Make, n8n,
             your own service). Use this to reach Metricool, LinkedIn, CMS etc.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..config import Settings, settings
from ..db import EventLog, Item

TARGETS = ("export", "webhook")


class PublishError(RuntimeError):
    pass


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "item"


def available_targets(cfg: Settings = settings) -> list[str]:
    return ["export"] + (["webhook"] if cfg.webhook_url else [])


def publish(db: Session, item: Item, target: str, cfg: Settings = settings,
            http: httpx.Client | None = None) -> Item:
    if target not in available_targets(cfg):
        raise PublishError(f"Target '{target}' is not configured. Available: {', '.join(available_targets(cfg))}.")
    # Atomic claim: only one request can move approved -> publishing, so two
    # clicks (or two tabs) can never send the same item twice.
    claimed = db.execute(update(Item).where(Item.id == item.id, Item.status == "approved")
                         .values(status="publishing")).rowcount
    db.commit()
    if not claimed:
        db.refresh(item)
        raise PublishError(f"Item {item.id} is '{item.status}'. Only approved items can be published.")
    try:
        ref = _send(item, target, cfg, http)
    except Exception:
        db.execute(update(Item).where(Item.id == item.id).values(status="approved"))
        db.commit()
        db.refresh(item)
        raise
    db.refresh(item)

    item.status = "published"
    item.publish_target = target
    item.publish_ref = ref
    item.published_at = datetime.now(timezone.utc)
    db.add(EventLog(run_id=item.run_id, agent="publisher", message=f"Published item {item.id} via {target}: {ref}"))
    db.flush()
    return item


def _send(item: Item, target: str, cfg: Settings, http: httpx.Client | None) -> str:
    if target == "export":
        cfg.exports_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.exports_dir / f"{item.id:05d}-{_slug(item.title)}.md"
        path.write_text(f"# {item.title}\n\n_Format: {item.kind}_\n\n{item.body}\n", encoding="utf-8")
        ref = str(path)
    else:
        client = http or httpx.Client(timeout=20)
        try:
            r = client.post(cfg.webhook_url, json={
            "id": item.id, "project_id": item.project_id, "kind": item.kind,
            "title": item.title, "body": item.body})
        except httpx.HTTPError as e:
            raise PublishError(f"Webhook unreachable: {type(e).__name__}") from e
        if r.status_code >= 300:
            raise PublishError(f"Webhook returned {r.status_code}: {r.text[:200]}")
        ref = f"webhook {r.status_code}"
    return ref
