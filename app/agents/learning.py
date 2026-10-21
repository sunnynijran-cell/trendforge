"""Learning and influence mode.

voices   - finds the channels worth learning from, by views, and splits them into
           established and rising. Drops heavily political channels.
distil   - turns their ideas into lens cards: principle, why, when to use, when
           not to, a mantra of 8 words or fewer, and the situations it fits.
gaps     - for the subjects YOU cover, compares demand (views per hour) with
           supply (strong recent videos) to show where you could add something.
comments - drafts comments for you to post by hand on rising videos, with a
           book link only when it fits and the spam rules allow it.
situation (on demand, not in the pipeline) - you describe a situation, it hands
           back one card per voice, the tension between them, and a first move.

Nothing here posts anywhere. Comment drafts are for you to copy and post.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..db import Book, CommentDraft, Gap, LensCard, Trend, Video, Voice, as_utc
from ..youtube import SHORT_MAX_S
from . import guards
from .base import Ctx
from .scout import is_relevant

SCENARIOS = [
    "cold call opening", "price objection", "stalled decision", "negotiation",
    "tense meeting", "pushback on a plan", "pitching an idea", "first meeting",
    "giving hard feedback", "personal stress", "family conflict", "low motivation",
]
RISING_MAX_SUBS = 250_000
RISING_MIN_RATIO = 0.3          # views on this run's videos / subscribers
MAX_VOICES = 15
MAX_CARD_VOICES = 8
MAX_GAP_SUBJECTS = 5
MAX_COMMENTS = 5
LINK_COOLDOWN = timedelta(days=30)
# Anything that reads as a link: http(s)://, www., or a bare domain like bit.ly/x or amazon.co.uk
_TLD = r"(?:com|co\.uk|org\.uk|uk|ly|io|net|org|me|app|link|gl|co|ai|info|biz|shop|store|xyz|page|site|in|au|de|tv)"
URL_RE = re.compile(
    r"(?i:(?:https?://|www\.)\S+)"                                     # any case
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\." + _TLD + r"\b(?:/\S*)?"          # bare lowercase domain: bit.ly/x
    r"|(?i:\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\." + _TLD + r"/\S+)")          # any case WITH a path: Amazon.com/dp/1


def find_links(text: str) -> list[str]:
    return [m.group(0).rstrip(".,);:!?") for m in URL_RE.finditer(text)]


def classify_tier(subscribers: int, recent_views: int) -> str:
    """Rising = smaller channel whose recent videos pull views well above its size."""
    if subscribers and subscribers < RISING_MAX_SUBS and recent_views / subscribers >= RISING_MIN_RATIO:
        return "rising"
    return "established"


# ---------------------------------------------------------------- voices
def run_voices(ctx: Ctx) -> int:
    videos = list(ctx.db.scalars(select(Video).where(Video.run_id == ctx.run.id)))
    by_channel: dict[str, list[Video]] = {}
    for v in videos:
        if v.channel_id:
            by_channel.setdefault(v.channel_id, []).append(v)
    top = sorted(by_channel, key=lambda c: -sum(v.views for v in by_channel[c]))[:MAX_VOICES]
    if not top:
        ctx.log("voices", "No channels to assess.", "warn")
        return 0
    stats = {c.channel_id: c for c in ctx.yt.channels(ctx.db, top, ctx.run.id)}
    data = {"interest": ctx.project.description or ctx.project.name,
            "channels": [{"channel_id": c, "title": stats[c].title if c in stats else by_channel[c][0].channel_title,
                          "description": (stats[c].description if c in stats else "")[:300],
                          "sample_titles": [v.title for v in by_channel[c][:3]]} for c in top]}
    verdicts: dict[str, dict] = {}
    try:
        res = ctx.llm.json(ctx.db, "vet",
                           "You assess YouTube channels for a learner. Mark political=true only if the channel is "
                           "mainly about party politics, elections or culture-war commentary. Mark on_topic=false if "
                           "it does not genuinely teach the interest, if it is mainly not in English, or if it is "
                           "clickbait (manipulation tricks, get-rich claims) rather than real teaching.",
                           'Return {"channels":[{"channel_id":str,"on_topic":bool,"political":bool,"focus":str}]}.'
                           "\nDATA:" + json.dumps(data), ctx.run.id)
        for r in (res.get("channels") if isinstance(res, dict) else None) or []:
            if isinstance(r, dict) and r.get("channel_id"):
                verdicts[str(r["channel_id"])] = r
    except Exception as e:
        if type(e).__name__ == "BudgetExceeded":
            raise
        ctx.log("voices", f"Channel vetting skipped: {e}", "warn")

    kept = 0
    for cid in top:
        vids = by_channel[cid]
        st = stats.get(cid)
        verdict = verdicts.get(cid, {})
        recent = sum(v.views for v in vids)
        subs = st.subscribers if st else 0
        voice = ctx.db.scalar(select(Voice).where(Voice.project_id == ctx.project.id, Voice.channel_id == cid))
        if not voice:
            voice = Voice(project_id=ctx.project.id, channel_id=cid, title=(st.title if st else vids[0].channel_title))
            ctx.db.add(voice)
        voice.subscribers, voice.total_views = subs, (st.total_views if st else 0)
        voice.recent_views = recent
        voice.tier = classify_tier(subs, recent)
        voice.last_seen = datetime.now(timezone.utc)
        if verdict:            # no verdict this run: keep what we knew, never loosen
            voice.vetted = True
            voice.political = voice.political or verdict.get("political") is True
            voice.focus = str(verdict.get("focus", ""))[:300] or voice.focus
            if verdict.get("on_topic") is False:
                voice.hidden = True
        kept += bool(voice.vetted) and not (voice.hidden or voice.political)
    ctx.db.flush()
    unvetted = sum(1 for c in top if c not in verdicts)
    if unvetted:
        ctx.log("voices", f"{unvetted} channel(s) could not be vetted this run and are held back.", "warn")
    ctx.log("voices", f"Assessed {len(top)} channel(s): {kept} usable, "
                      f"{sum(1 for c in top if verdicts.get(c, {}).get('political') is True)} political dropped.")
    return kept


def usable_voices(ctx: Ctx) -> list[Voice]:
    return usable_voices_for(ctx.db, ctx.project.id)


def usable_voices_for(db, project_id: int) -> list[Voice]:
    q = select(Voice).where(Voice.project_id == project_id, Voice.hidden.is_(False),
                            Voice.political.is_(False), Voice.vetted.is_(True))
    return sorted(db.scalars(q), key=lambda v: -v.recent_views)


def usable_card_filter(db, project_id: int):
    """Cards are shown only while their voice is still usable (not hidden, not political)."""
    return LensCard.channel_id.in_([v.channel_id for v in usable_voices_for(db, project_id)])


# ---------------------------------------------------------------- distil
def run_distil(ctx: Ctx) -> int:
    voices = usable_voices(ctx)[:MAX_CARD_VOICES]
    vids = list(ctx.db.scalars(select(Video).where(Video.run_id == ctx.run.id)))
    by_channel: dict[str, list[Video]] = {}
    for v in vids:
        by_channel.setdefault(v.channel_id or "", []).append(v)
    by_name = {vo.title.strip().lower(): vo.channel_id for vo in voices}
    by_id = {vo.channel_id for vo in voices}
    payload_voices = [{"channel_id": vo.channel_id, "name": vo.title, "focus": vo.focus, "tier": vo.tier,
                       "videos": [{"yt_id": v.yt_id, "title": v.title, "summary": v.description[:400]}
                                  for v in sorted(by_channel.get(vo.channel_id, []), key=lambda v: -v.views)[:4]]}
                      for vo in voices if by_channel.get(vo.channel_id)]
    if not payload_voices:
        ctx.log("distil", "No usable voices with videos this run.", "warn")
        return 0
    data = {"learner": ctx.project.audience or "a senior consultant", "interest": ctx.project.description,
            "scenarios": SCENARIOS, "voices": payload_voices}
    res = ctx.llm.json(
        ctx.db, "distil",
        "You turn what respected voices teach into practical lens cards for one learner. Write every field in "
        "your own plain UK English. Never quote the creator's wording. You may draw on what each voice is widely "
        "known to teach, but tie each card to one of the listed videos. Every card must say when NOT to use it.",
        "Make 1-3 cards per voice. mantra: 8 words or fewer, sayable in the moment. scenarios: pick from the "
        'scenarios list. Return {"cards":[{"voice":str,"principle":str,"why":str,"use_when":str,'
        '"avoid_when":str,"mantra":str,"scenarios":[str],"source_yt_id":str,"channel_id":str}]}. '
        "Copy channel_id exactly from the voice the card came from.\nDATA:" + json.dumps(data),
        ctx.run.id)
    valid_ids = {v.yt_id for v in vids}
    descriptions = [f"{v.title}\n{v.description}" for v in vids]
    made = dropped = 0
    for c in (res.get("cards") if isinstance(res, dict) else None) or []:
        if not isinstance(c, dict) or not c.get("principle") or not c.get("mantra"):
            dropped += 1
            continue
        mantra = str(c["mantra"]).strip().strip('"')
        if len(mantra.split()) > 8:
            dropped += 1
            continue
        text = " ".join(str(c.get(k, "")) for k in ("principle", "why", "use_when", "avoid_when", "mantra"))
        mine = guards.shingles(text)
        if any(mine & guards.shingles(d) for d in descriptions):
            dropped += 1          # lifted 8+ words from a video description
            continue
        cid = str(c.get("channel_id", "")) if str(c.get("channel_id", "")) in by_id \
            else by_name.get(str(c.get("voice", "")).strip().lower())
        if not cid:               # card not tied to a usable voice we sent
            dropped += 1
            continue
        yt = str(c.get("source_yt_id", ""))
        scen = [s for s in c.get("scenarios", []) if isinstance(s, str)][:5] if isinstance(c.get("scenarios"), list) else []
        ctx.db.add(LensCard(project_id=ctx.project.id, run_id=ctx.run.id, voice=str(c.get("voice", ""))[:200],
                            channel_id=cid,
                            principle=str(c["principle"]), why=str(c.get("why", "")),
                            use_when=str(c.get("use_when", "")), avoid_when=str(c.get("avoid_when", "")),
                            mantra=mantra[:120], scenarios=scen,
                            source_url=f"https://www.youtube.com/watch?v={yt}" if yt in valid_ids else ""))
        made += 1
    ctx.db.flush()
    ctx.log("distil", f"{made} lens card(s) from {len(payload_voices)} voice(s)"
                      + (f", {dropped} dropped by the rules." if dropped else "."))
    return made


# ---------------------------------------------------------------- gaps
def clean_coverage(coverage: list[str] | None) -> list[str]:
    return [c.strip() for c in (coverage or []) if c and c.strip()][:MAX_GAP_SUBJECTS]


def gap_units(coverage: list[str] | None) -> int:
    """Upper bound: a search plus a stats call per subject."""
    return len(clean_coverage(coverage)) * 101


def run_gaps(ctx: Ctx) -> int:
    subjects = clean_coverage(ctx.project.coverage)
    if not subjects:
        ctx.log("gaps", "No subjects listed under 'What I cover', so no gap check this run.")
        return 0
    rows = []
    after = datetime.now(timezone.utc) - timedelta(days=90)
    now = datetime.now(timezone.utc)
    for subj in subjects:
        ids = ctx.yt.search(ctx.db, subj, "GB", 25, after, ctx.run.id)
        stats = [s for s in ctx.yt.videos(ctx.db, ids, ctx.run.id) if is_relevant(subj, s)] if ids else []
        vph = sorted((s.views / max((now - as_utc(s.published_at)).total_seconds() / 3600, 1) for s in stats),
                     reverse=True)
        demand = round(sum(vph[:10]), 1)
        supply = sum(1 for s in stats if s.views >= 1000 and s.duration_s > SHORT_MAX_S)
        rows.append({"subject": subj, "demand": demand, "supply": supply,
                     "score": round(demand / (1 + supply), 2),
                     "top_titles": [s.title for s in sorted(stats, key=lambda s: -s.views)[:5]]})
    angles: dict[str, dict] = {}
    try:
        res = ctx.llm.json(ctx.db, "angle",
                           "You spot content gaps. Suggest an angle the existing top videos do NOT already take.",
                           'Return {"angles":[{"subject":str,"angle":str,"format":str}]}.\nDATA:'
                           + json.dumps({"gaps": rows, "who": ctx.project.audience}), ctx.run.id)
        for a in (res.get("angles") if isinstance(res, dict) else None) or []:
            if isinstance(a, dict) and a.get("subject"):
                angles[str(a["subject"])] = a
    except Exception as e:
        if type(e).__name__ == "BudgetExceeded":
            raise
        ctx.log("gaps", f"Angle suggestions skipped: {e}", "warn")
    for r in rows:
        a = angles.get(r["subject"], {})
        ctx.db.add(Gap(project_id=ctx.project.id, run_id=ctx.run.id, subject=r["subject"][:200],
                       demand=r["demand"], supply=r["supply"], score=r["score"],
                       angle=str(a.get("angle", "")), format=str(a.get("format", ""))[:60]))
    ctx.db.flush()
    best = max(rows, key=lambda r: r["score"])
    ctx.log("gaps", f"Checked {len(rows)} subject(s). Biggest gap: '{best['subject']}' "
                    f"({best['demand']:,.0f} views/hour of demand, {best['supply']} strong recent videos).")
    return len(rows)


# ---------------------------------------------------------------- comments
def match_book(books: list[Book], text: str) -> Book | None:
    low = text.lower()
    best, hits = None, 0
    for b in books:
        n = sum(1 for t in (b.themes or []) if t and re.search(rf"\b{re.escape(t.lower())}\b", low))
        if n > hits:
            best, hits = b, n
    return best


def strip_links(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", URL_RE.sub("", text)).strip()


def book_url(book: Book | None) -> str:
    return book.url if book else ""


def link_allowed(ctx: Ctx, channel_id: str, is_short: bool) -> bool:
    """One book link per channel per 30 days, never on Shorts. A linked draft counts
    from when it was created; once you mark it posted, it counts from the posting date."""
    if is_short:
        return False
    since = datetime.now(timezone.utc) - LINK_COOLDOWN
    n = ctx.db.scalar(select(func.count()).select_from(CommentDraft).where(
        CommentDraft.channel_id == channel_id, CommentDraft.has_link.is_(True),
        CommentDraft.status != "dismissed",
        func.coalesce(CommentDraft.posted_at, CommentDraft.created_at) >= since))
    return n == 0


def run_comments(ctx: Ctx) -> int:
    voices = {v.channel_id: v for v in usable_voices(ctx)}
    trends = list(ctx.db.scalars(select(Trend).where(Trend.run_id == ctx.run.id).order_by(Trend.score.desc())))
    done = set(ctx.db.scalars(select(CommentDraft.yt_id).where(CommentDraft.project_id == ctx.project.id)))
    picks = []
    for t in trends:   # rising voices first, then the rest, best score first
        v = t.video
        if v.channel_id in voices and v.yt_id not in done:
            picks.append((0 if voices[v.channel_id].tier == "rising" else 1, -t.score, v))
    picks = [p[2] for p in sorted(picks, key=lambda p: (p[0], p[1]))[:MAX_COMMENTS]]
    if not picks:
        ctx.log("comments", "No new videos worth commenting on this run.")
        return 0
    books = list(ctx.db.scalars(select(Book).where(Book.active.is_(True))))
    made = links = 0
    for v in picks:
        is_short = v.duration_s <= SHORT_MAX_S     # unknown length (0) is treated as a Short: no link
        book = match_book(books, f"{v.title} {' '.join(v.tags)}")
        include_link = bool(book and book.url and link_allowed(ctx, v.channel_id or "", is_short))
        data = {"video": {"title": v.title, "channel": v.channel_title, "summary": v.description[:400]},
                "me": ctx.project.audience or "a senior consultant", "brand_rules": ctx.project.brand_rules,
                "include_link": include_link, "mention_book_no_link": bool(book and not include_link and is_short),
                "book": {"title": book.title, "url": book.url} if book else None}
        try:
            res = ctx.llm.json(
                ctx.db, "comment",
                "You draft a YouTube comment the user will post themselves. Under 80 words. Add one specific, "
                "useful point; agree or build on the video, never flatter. No hype, no emojis, no hashtags. "
                "If include_link is true, end with one short line naming the book and its URL. If false, include "
                "no URL at all.",
                'Return {"body":str}.\nDATA:' + json.dumps(data), ctx.run.id)
        except Exception as e:
            if type(e).__name__ == "BudgetExceeded":
                raise
            ctx.log("comments", f"Draft for '{v.title[:50]}' failed: {e}", "warn")
            continue
        body = str(res.get("body", "")).strip() if isinstance(res, dict) else ""
        if not body:
            continue
        others = [u for u in find_links(body) if u != book_url(book)]
        if include_link and book.url in body and not others:
            pass                                   # exactly the one link we allow
        elif include_link:
            body = f"{strip_links(body)}\n\nMore in {book.title}: {book.url}"
        else:
            body = strip_links(body)               # never a stray link
        if not guards.check_item("email", "", body, list(ctx.project.banned_terms or []), [])["passed"]:
            ctx.log("comments", f"Draft for '{v.title[:50]}' broke a rule and was dropped.", "warn")
            continue
        ctx.db.add(CommentDraft(project_id=ctx.project.id, run_id=ctx.run.id, yt_id=v.yt_id,
                                video_title=v.title, channel_id=v.channel_id or "",
                                channel_title=v.channel_title, is_short=is_short, body=body,
                                book_id=book.id if book else None, has_link=include_link))
        ctx.db.commit()          # keep each draft even if a later one fails
        made += 1
        links += include_link
    ctx.log("comments", f"{made} comment draft(s) for you to post by hand, {links} with a book link.")
    return made


# ---------------------------------------------------------------- situation (on demand)
def pick_for_situation(db, llm, project_id: int, situation: str) -> dict:
    cards = list(db.scalars(select(LensCard).where(LensCard.project_id == project_id,
                                                   LensCard.archived.is_(False),
                                                   usable_card_filter(db, project_id))))
    if not cards:
        return {"cards": [], "tension": "", "first_move": "No lens cards yet. Run the pipeline first."}
    # cards that helped you before rank higher in what the model sees
    cards.sort(key=lambda c: -(c.helped * 2 - (c.uses - c.helped)))
    data = {"situation": situation[:1000],
            "cards": [{"id": c.id, "voice": c.voice, "principle": c.principle, "mantra": c.mantra,
                       "use_when": c.use_when, "avoid_when": c.avoid_when, "scenarios": c.scenarios}
                      for c in cards[:60]]}
    res = llm.json(db, "situation",
                   "You coach someone in the moment. Pick up to 4 cards that fit, ideally from different voices, "
                   "skip any whose avoid_when matches. Name the tension between them and one first move.",
                   'Return {"card_ids":[int],"tension":str,"first_move":str}.\nDATA:' + json.dumps(data), None)
    by_id = {c.id: c for c in cards}
    ids = [i for i in (res.get("card_ids") or []) if isinstance(i, int) and i in by_id][:4] \
        if isinstance(res, dict) else []
    for i in ids:
        by_id[i].uses += 1
    db.commit()
    return {"cards": [by_id[i] for i in ids], "tension": str(res.get("tension", "")) if isinstance(res, dict) else "",
            "first_move": str(res.get("first_move", "")) if isinstance(res, dict) else ""}
