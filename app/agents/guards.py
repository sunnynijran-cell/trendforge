"""Checks every made item must pass before it reaches your review queue.

1. Content present and within the length limit for its format.
2. Banned terms from the project (brand fit).
3. Copyright: no run of 8+ words lifted from a source video title or description.
4. Disclosure: no first-person claims to be a named real person or brand.
Items that fail are marked 'blocked' with the reasons. They never publish.
"""
from __future__ import annotations

import re

LIMITS = {  # characters
    "linkedin_post": 3000, "x_post": 280, "youtube_short_script": 1500,
    "youtube_script": 12000, "blog_post": 15000, "email": 5000, "carousel": 4000,
    "talk_outline": 8000, "course_module": 15000,
}
DEFAULT_LIMIT = 10000
SHINGLE = 8


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def shingles(text: str, n: int = SHINGLE) -> set[tuple[str, ...]]:
    w = _words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def check_item(kind: str, title: str, body: str, banned_terms: list[str], sources: list[str]) -> dict:
    issues: list[str] = []
    text = f"{title}\n{body}"
    if not body.strip():
        issues.append("Empty body.")
    limit = LIMITS.get(kind, DEFAULT_LIMIT)
    if len(body) > limit:
        issues.append(f"Too long for {kind}: {len(body)} chars, limit {limit}.")
    low = text.lower()
    hits = [t for t in banned_terms if t and re.search(rf"\b{re.escape(t.lower())}\b", low)]
    if hits:
        issues.append(f"Banned terms: {', '.join(hits)}.")
    mine = shingles(text)
    for src in sources:
        overlap = mine & shingles(src)
        if overlap:
            issues.append("Copies 8+ consecutive words from a source video: '"
                          + " ".join(next(iter(overlap))) + "'.")
            break
    if re.search(r"\b(i am|i'm)\s+(elon musk|mrbeast|an official|the official)\b", low):
        issues.append("Claims to be a real person or official body.")
    return {"passed": not issues, "issues": issues, "limit": limit, "length": len(body)}
