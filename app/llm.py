"""Claude access with model routing and cost booking.

Routing (cheapest model that does the job):
  cluster  -> fast  (Haiku)  : group trending titles into themes
  check    -> fast  (Haiku)  : second-opinion brand / claims check
  design   -> smart (Sonnet) : turn themes into briefs that fit a project
  make     -> smart (Sonnet) : write the actual item
Learning mode:
  vet       -> fast  (Haiku)  : is a channel on-topic, is it heavily political
  distil    -> smart (Sonnet) : ideas into lens cards and mantras
  angle     -> fast  (Haiku)  : suggest an angle for each content gap
  comment   -> smart (Sonnet) : draft a comment for you to post by hand
  situation -> fast  (Haiku)  : pick the right cards for a live situation
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol

from sqlalchemy.orm import Session

from .config import Settings, settings
from .ledger import charge_llm, llm_cost, llm_reservation

ROUTES = {"cluster": "fast", "check": "fast", "design": "smart", "make": "smart",
          "vet": "fast", "distil": "smart", "angle": "fast", "comment": "smart", "situation": "fast"}
MAX_TOKENS = {"cluster": 1200, "check": 600, "design": 2500, "make": 3000,
              "vet": 1500, "distil": 4000, "angle": 1200, "comment": 1500, "situation": 800}


def model_for(task: str, cfg: Settings = settings) -> str:
    return cfg.model_smart if ROUTES[task] == "smart" else cfg.model_fast


def worst_case_usd(task: str, prompt: str, system: str = "", cfg: Settings = settings) -> float:
    """Upper bound for one call: 1 token per 2 chars in (safe even for non-Latin
    text), plus the full max_tokens out."""
    return llm_cost(model_for(task, cfg), (len(prompt) + len(system)) // 2 + 100, MAX_TOKENS[task], cfg)


def extract_json(text: str) -> Any:
    """Pull the first JSON object or array out of a model reply."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("No valid JSON in model reply")


class LLM(Protocol):
    def json(self, db: Session, task: str, system: str, prompt: str, run_id: int | None) -> Any: ...


class ClaudeLLM:
    label = ""   # empty: logs show the real model name

    def __init__(self, cfg: Settings = settings):
        import anthropic
        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=3, timeout=120)

    def json(self, db, task, system, prompt, run_id):
        model = model_for(task, self.cfg)
        last_err: Exception | None = None
        for attempt in range(2):
            content = prompt if attempt == 0 else prompt + "\n\nYour last reply was not valid JSON. Return JSON only."
            with llm_reservation(db, run_id, worst_case_usd(task, content, system, self.cfg), self.cfg):
                msg = self.client.messages.create(
                    model=model, max_tokens=MAX_TOKENS[task],
                    system=system + "\nReply with valid JSON only. No prose, no markdown fences.",
                    messages=[{"role": "user", "content": content}],
                )
                charge_llm(db, model, msg.usage.input_tokens, msg.usage.output_tokens, task, run_id, self.cfg)
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            try:
                return extract_json(text)
            except ValueError as e:
                last_err = e
        raise RuntimeError(f"{task}: model did not return JSON after 2 attempts ({last_err})")


class FakeLLM:
    """Deterministic stand-in for tests and offline demos. Books a token cost
    so the budget logic is exercised exactly as it would be live."""

    label = "offline fake model"

    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.calls: list[str] = []
        # Tests (OFFLINE=1) book a simulated cost so the budget logic is exercised.
        # A live deploy that simply has no Claude key yet books $0, so the meter stays honest.
        self.simulate_cost = cfg.offline_override == "1"

    def json(self, db, task, system, prompt, run_id):
        with llm_reservation(db, run_id, worst_case_usd(task, prompt, system, self.cfg), self.cfg):
            self.calls.append(task)
            if self.simulate_cost:
                charge_llm(db, model_for(task, self.cfg), len(prompt) // 4, 400, task, run_id, self.cfg)
        payload = json.loads(prompt.split("DATA:", 1)[1]) if "DATA:" in prompt else {}
        if task == "cluster":
            themes: dict[str, list[int]] = {}
            for v in payload.get("videos", []):
                words = [w for w in re.findall(r"[A-Za-z]{4,}", v["title"]) if w.lower() not in
                         {"this", "that", "with", "your", "what", "from", "will", "they", "have", "just"}]
                key = (words[0] if words else "General").title()
                themes.setdefault(key, []).append(v["trend_id"])
            return {"themes": [{"name": k, "trend_ids": ids, "why": f"{len(ids)} videos share '{k}'"}
                               for k, ids in themes.items()]}
        if task == "design":
            fmts = payload["project"]["output_types"]
            ts = payload["themes"]
            n = payload["count"]
            return {"briefs": [{
                "title": f"{t['name']}: what the numbers say",
                "angle": f"Take the '{t['name']}' trend and answer it for {payload['project']['audience'] or 'the audience'}.",
                "format": fmts[i % len(fmts)],
                "rationale": f"Theme scores {t['score']:.2f}; {len(t['trend_ids'])} videos.",
                "source_trend_ids": t["trend_ids"][:3],
            } for i, t in enumerate(ts[:n])]}
        if task == "make":
            b = payload["brief"]
            return {"title": b["title"], "body":
                    f"{b['angle']}\n\nThree points:\n1. Lead with the outcome.\n"
                    f"2. Show one number that matters.\n3. Close with a clear next step.\n\n"
                    f"Format: {b['format']}."}
        if task == "check":
            return {"ok": True, "issues": []}
        if task == "vet":
            return {"channels": [{"channel_id": c["channel_id"], "on_topic": True,
                                  "political": "politic" in (c["title"] + c["description"]).lower(),
                                  "focus": f"Talks about {', '.join(c['sample_titles'][:1]) or 'the topic'}"}
                                 for c in payload["channels"]]}
        if task == "distil":
            cards = []
            for v in payload["voices"]:
                vid = v["videos"][0]
                word = (re.findall(r"[A-Za-z]{5,}", vid["title"]) or ["Focus"])[0].title()
                cards.append({"voice": v["name"], "principle": f"Lead with {word.lower()} before detail.",
                              "why": "People decide on the first clear signal.",
                              "use_when": "Opening a conversation.", "avoid_when": "Someone is upset.",
                              "mantra": f"{word} first, detail second", "scenarios": payload["scenarios"][:2],
                              "source_yt_id": vid["yt_id"]})
            return {"cards": cards}
        if task == "angle":
            return {"angles": [{"subject": g["subject"], "angle": f"A practical guide to {g['subject']}",
                                "format": "youtube_script"} for g in payload["gaps"]]}
        if task == "comment":
            c = payload["video"]
            body = f"Useful point on {c['title'][:40]}. The part people miss is acting on it early."
            if payload.get("include_link"):
                body += f" I go deeper on this in {payload['book']['title']}: {payload['book']['url']}"
            return {"body": body}
        if task == "situation":
            words = set(re.findall(r"[a-z]{4,}", payload["situation"].lower()))
            ranked = sorted(payload["cards"], key=lambda c: -len(words & set(re.findall(
                r"[a-z]{4,}", (c["principle"] + " " + " ".join(c["scenarios"]) + " " + c["use_when"]).lower()))))
            return {"card_ids": [c["id"] for c in ranked[:4]], "tension": "Speed versus patience.",
                    "first_move": "Pause, then name what they actually want."}
        raise ValueError(task)


def get_llm(cfg: Settings = settings) -> LLM:
    return FakeLLM(cfg) if cfg.llm_offline else ClaudeLLM(cfg)
