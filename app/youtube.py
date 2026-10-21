"""YouTube access through the official YouTube Data API v3.

Why the API and not scraping: scraping breaks YouTube's Terms of Service and
breaks whenever the page changes. The API is free within quota and stable.

Quota (default 10,000 units/day per Google Cloud project):
  search.list  = 100 units per call  (this is the expensive one)
  videos.list  = 1 unit per call, up to 50 ids per call
Docs: https://developers.google.com/youtube/v3/determine_quota_cost
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from .config import Settings, settings
from .ledger import charge_youtube

API = "https://www.googleapis.com/youtube/v3"
SEARCH_COST = 100
VIDEOS_COST = 1
VIDEOS_BATCH = 50

_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(iso: str) -> int:
    """ISO 8601 duration (PT1H2M3S) to seconds."""
    m = _DUR.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class VideoStats:
    yt_id: str
    title: str
    channel_title: str
    description: str
    tags: list[str]
    published_at: datetime
    views: int
    likes: int
    comments: int
    duration_s: int


def estimate_units(n_topics: int, max_results_each: list[int]) -> int:
    """Units a scout pass will cost: one search per topic plus batched stats calls."""
    ids = sum(min(m, 50) for m in max_results_each)
    return n_topics * SEARCH_COST + math.ceil(ids / VIDEOS_BATCH) * VIDEOS_COST if n_topics else 0


class YouTubeSource(Protocol):
    def search(self, db: Session, query: str, region: str, max_results: int,
               published_after: datetime, run_id: int | None) -> list[str]: ...

    def videos(self, db: Session, ids: list[str], run_id: int | None) -> list[VideoStats]: ...


def _to_stats(item: dict) -> VideoStats:
    sn, st, cd = item.get("snippet", {}), item.get("statistics", {}), item.get("contentDetails", {})
    return VideoStats(
        yt_id=item["id"],
        title=sn.get("title", ""),
        channel_title=sn.get("channelTitle", ""),
        description=sn.get("description", "")[:5000],
        tags=sn.get("tags", [])[:30],
        published_at=parse_ts(sn["publishedAt"]),
        views=int(st.get("viewCount", 0)),
        likes=int(st.get("likeCount", 0)),   # can be hidden by the creator
        comments=int(st.get("commentCount", 0)),
        duration_s=parse_duration(cd.get("duration", "")),
    )


class LiveYouTube:
    def __init__(self, cfg: Settings = settings, client: httpx.Client | None = None):
        self.cfg = cfg
        self.http = client or httpx.Client(timeout=20)

    def _get(self, db, path: str, params: dict, cost: int, detail: str, run_id) -> dict:
        """One API call. Quota is booked per attempt, because Google bills failed
        requests too. The key travels in a header so it never appears in URLs,
        logs or error messages."""
        from .ledger import QuotaExceeded
        last = ""
        for attempt in range(3):
            charge_youtube(db, cost, f"{detail} (try {attempt + 1})" if attempt else detail, run_id, self.cfg)
            try:
                r = self.http.get(f"{API}/{path}", params=params,
                                  headers={"X-Goog-Api-Key": self.cfg.youtube_api_key})
            except httpx.TransportError as e:
                last = f"network error: {type(e).__name__}"
                continue
            if r.status_code < 300:
                return r.json()
            reason = _error_reason(r)
            if r.status_code == 403 and "quota" in reason.lower():
                raise QuotaExceeded("Google reports the YouTube quota is exhausted for today.")
            if r.status_code >= 500:
                last = f"YouTube {r.status_code}: {reason}"
                continue
            raise RuntimeError(f"YouTube API {r.status_code}: {reason}")
        raise RuntimeError(f"YouTube API failed after 3 tries ({last}).")

    def search(self, db, query, region, max_results, published_after, run_id):
        data = self._get(db, "search", {
            "part": "id", "q": query, "type": "video", "order": "viewCount",
            "regionCode": region, "maxResults": min(max_results, 50),
            "publishedAfter": published_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "relevanceLanguage": "en", "safeSearch": "moderate",
        }, SEARCH_COST, f"search:{query}", run_id)
        return [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]

    def videos(self, db, ids, run_id):
        out: list[VideoStats] = []
        for i in range(0, len(ids), VIDEOS_BATCH):
            batch = ids[i:i + VIDEOS_BATCH]
            data = self._get(db, "videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)},
                             VIDEOS_COST, f"videos:{len(batch)}", run_id)
            out += [_to_stats(it) for it in data.get("items", [])]
        return out


def _error_reason(r: httpx.Response) -> str:
    """Google's own error message only. Never the URL, which could hold secrets."""
    try:
        err = r.json().get("error", {})
        reasons = ",".join(e.get("reason", "") for e in err.get("errors", []))
        return (f"{err.get('message', '')} [{reasons}]").strip()[:300]
    except Exception:
        return r.reason_phrase or "error"


class FixtureYouTube:
    """Recorded API responses. Same interface, same quota accounting, no network.
    Fixture dates are shifted so the newest video is 'now minus 6 hours'."""

    def __init__(self, cfg: Settings = settings, now: datetime | None = None):
        self.cfg = cfg
        raw = json.loads((cfg.fixtures_dir / "youtube_videos.json").read_text())
        items = raw["items"]
        newest = max(parse_ts(i["snippet"]["publishedAt"]) for i in items)
        shift = (now or datetime.now(timezone.utc)) - timedelta(hours=6) - newest
        for it in items:
            it["snippet"]["publishedAt"] = (parse_ts(it["snippet"]["publishedAt"]) + shift).isoformat()
        self.items = {i["id"]: i for i in items}

    def search(self, db, query, region, max_results, published_after, run_id):
        charge_youtube(db, SEARCH_COST, f"search:{query}", run_id, self.cfg)
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        hits = []
        for vid, it in self.items.items():
            hay = (it["snippet"]["title"] + " " + " ".join(it["snippet"].get("tags", []))).lower()
            if parse_ts(it["snippet"]["publishedAt"]) >= published_after and any(w in hay for w in words):
                hits.append((int(it["statistics"].get("viewCount", 0)), vid))
        hits.sort(reverse=True)
        return [v for _, v in hits[:min(max_results, 50)]]

    def videos(self, db, ids, run_id):
        out = []
        for i in range(0, len(ids), VIDEOS_BATCH):
            batch = ids[i:i + VIDEOS_BATCH]
            charge_youtube(db, VIDEOS_COST, f"videos:{len(batch)}", run_id, self.cfg)
            out += [_to_stats(self.items[v]) for v in batch if v in self.items]
        return out


def get_source(cfg: Settings = settings) -> YouTubeSource:
    return FixtureYouTube(cfg) if cfg.youtube_offline else LiveYouTube(cfg)
