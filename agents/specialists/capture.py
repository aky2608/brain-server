"""
CaptureAgent — slash check → classify (1min.ai) → embed (Google) → write.

SHADOW_MODE = True  : classifies and embeds, logs results, does NOT write to items table.
                      classify_single remains authoritative. Flip to False for cutover.
SHADOW_MODE = False : writes classification + embedding to items, then runs wikilink
                      and embedding-similarity linking against thought_links.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, Optional

from dateutil import parser as _dateutil_parser

import httpx
import psycopg
from psycopg.types.json import Jsonb

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel
from shortcuts import parse_slash

logger = logging.getLogger("brain")

# Claude models are not supported for UNIFY_CHAT_WITH_AI on this 1min.ai account/plan —
# confirmed via direct API testing. Both models below are verified working.
# ⚠ gpt-4.1-nano deprecationDate: 2026-10-21 — replace fallback before that date.
_ONEMIN_MODEL = "gpt-4o-mini"
_FALLBACK_MODEL = "gpt-4.1-nano"
_EMBED_MODEL = "gemini-embedding-001"
_EMBED_DIMS = 1536  # matches vector(1536) column; truncated from model's native 3072 via outputDimensionality

_SIM_THRESHOLD = 0.82
_MAX_SIM_LINKS = 5

SHADOW_MODE = False

_EXTRACT_SUBCATEGORIES   = frozenset({"finance", "quotes", "wayclear", "accrediq"})
# 'finance' included because the LLM sometimes outputs category=finance instead of life/finance
_EXTRACT_NULL_CATEGORIES = frozenset({"thoughts", "life", "finance", "work"})


def _should_extract_people(category: Optional[str], subcategory: Optional[str]) -> bool:
    # Normalize the string literal 'null' the LLM emits to Python None
    if subcategory == "null":
        subcategory = None
    if subcategory in _EXTRACT_SUBCATEGORIES:
        return True
    if subcategory is None and category in _EXTRACT_NULL_CATEGORIES:
        return True
    return False


def _enqueue_people_extraction(item_id: str, raw_content: str, source: str) -> None:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        return
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO job_queue (job_type, payload) VALUES ('people_extraction', %s)",
            (Jsonb({"item_id": item_id, "raw_content": raw_content, "source": source}),),
        )
        conn.commit()


def _record_project_activity_capture(item_id: str, subcategory: Optional[str],
                                      action_class: Optional[str], raw: str) -> None:
    if not subcategory or subcategory == "null":
        return
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        return
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT id FROM projects WHERE alias = %s AND status = 'active'",
            (subcategory,),
        ).fetchone()
        if row is None:
            return
        summary = f"[{action_class or 'note'}] {raw[:120]}"
        conn.execute(
            """INSERT INTO project_activity (project_id, event_type, item_id, summary)
               VALUES (%s, 'capture', %s, %s)""",
            (str(row[0]), item_id, summary),
        )
        conn.commit()


_WIKILINK_RE = re.compile(r'\[\[([^\[\]]+)\]\]')

_PROMPT_TMPL: Optional[str] = None


def _load_prompt() -> str:
    global _PROMPT_TMPL
    if _PROMPT_TMPL is None:
        p = Path(__file__).parent.parent.parent / "prompts" / "classify_single.txt"
        _PROMPT_TMPL = p.read_text()
    return _PROMPT_TMPL


class CaptureInput(NarrowModel):
    raw: str
    source: str
    capture_uuid: str  # items.id (PK) — used for UPDATE WHERE id = %s
    capture_type: str = "text"


class CaptureOutput(NarrowModel):
    item_id: str
    category: str
    subcategory: Optional[str] = None
    tags: list[str] = []
    summary: str = ""
    action_class: str = "record"
    shadow: bool
    embedding_stored: bool = False
    links_created: int = 0  # wikilink + embedding links combined
    decisions: list[dict] = []


class CaptureAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.flash
    requires_context: ClassVar[list[str]] = []

    InputSchema = CaptureInput
    OutputSchema = CaptureOutput

    def handle(self, input: CaptureInput) -> CaptureOutput:
        # Slash commands should be short-circuited by personal_agent before reaching here.
        # Guard defensively — return a neutral result so the graph can close cleanly.
        if parse_slash(input.raw):
            logger.warning(
                "capture_agent received slash input — should have been routed upstream",
                extra={"ctx": {"item_id": input.capture_uuid}},
            )
            return CaptureOutput(
                item_id=input.capture_uuid,
                category="shortcut",
                shadow=True,
            )

        cls = _classify(input.raw, input.source, input.capture_type)

        # Regex pre-pass: deterministic extraction wins; LLM date_phrase is the fallback.
        _regex_phrase = _extract_date_phrase(input.raw)
        if _regex_phrase:
            cls["date_phrase"] = _regex_phrase
            _date_source = f"regex:{_regex_phrase}"
        elif cls.get("date_phrase"):
            _date_source = f"model:{cls['date_phrase']}"
        else:
            _date_source = "none"

        # Keyword override: LLM sometimes returns is_event=false for calls and meetings.
        # If a date phrase was found and the text contains event-language, force true.
        if cls.get("date_phrase") and not cls.get("is_event"):
            if any(kw in input.raw.lower() for kw in _EVENT_KEYWORDS):
                cls["is_event"] = True

        vector = _embed(input.raw)
        decisions: list[dict] = []

        cat = cls.get("category", "thoughts")
        sub = cls.get("subcategory")
        decisions.append({
            "agent_name": "capture_agent",
            "action_taken": f"classified:{cat}/{sub}" if sub else f"classified:{cat}",
            "reason": cls.get("summary", ""),
            "interrupt_tier": "log_only",
        })

        if SHADOW_MODE:
            logger.info(
                "capture_agent shadow",
                extra={
                    "ctx": {
                        "item_id": input.capture_uuid,
                        "classification": cls,
                        "embedded": vector is not None,
                    }
                },
            )
            return CaptureOutput(
                item_id=input.capture_uuid,
                category=cat,
                subcategory=sub,
                tags=cls.get("tags", []),
                summary=cls.get("summary", ""),
                action_class=cls.get("action_class", "record"),
                shadow=True,
                decisions=decisions,
            )

        # Non-shadow: one connection, one commit for all writes
        embedding_stored = False
        wikilink_count = 0
        embedding_link_count = 0
        url = os.environ.get("BRAIN_DB_URL", "")
        if url:
            try:
                captured_at = datetime.now(_IST)
                with psycopg.connect(url) as conn:
                    starts_at_dt, time_inferred = _write_classification(
                        input.capture_uuid, cls, conn, captured_at
                    )
                    if vector:
                        _store_embedding(input.capture_uuid, vector, conn)
                        embedding_stored = True
                    wikilink_count = _link_wikilinks(input.capture_uuid, input.raw, conn)
                    if vector:
                        embedding_link_count = _link_embeddings(input.capture_uuid, vector, conn)
                    conn.commit()

                if starts_at_dt:
                    decisions.append({
                        "agent_name":    "capture_agent",
                        "action_taken":  f"scheduled:{starts_at_dt.strftime('%Y-%m-%dT%H:%M')}",
                        "reason":        f"{'time guessed (09:00 default)' if time_inferred else 'explicit time'} — source:{_date_source}",
                        "interrupt_tier": "log_only",
                    })

                decisions.append({
                    "agent_name": "capture_agent",
                    "action_taken": "embedded" if embedding_stored else "embed:skipped",
                    "reason": f"model={_EMBED_MODEL}" if embedding_stored else "no vector returned",
                    "interrupt_tier": "log_only",
                })
                decisions.append({
                    "agent_name": "capture_agent",
                    "action_taken": f"linked:wikilink={wikilink_count},embedding={embedding_link_count}",
                    "reason": f"{wikilink_count} wikilink(s), {embedding_link_count} embedding link(s) created",
                    "interrupt_tier": "log_only",
                })

                people_enqueued = False
                if _should_extract_people(cls.get("category"), cls.get("subcategory")):
                    try:
                        _enqueue_people_extraction(input.capture_uuid, input.raw, input.source)
                        people_enqueued = True
                    except Exception:
                        logger.warning(
                            "people_extraction enqueue failed",
                            extra={"ctx": {"item_id": input.capture_uuid}},
                            exc_info=True,
                        )
                decisions.append({
                    "agent_name": "capture_agent",
                    "action_taken": "people:enqueued" if people_enqueued else "people:skipped",
                    "reason": f"category={cat} sub={sub}" if not people_enqueued else "queued for name extraction",
                    "interrupt_tier": "log_only",
                })

                try:
                    _record_project_activity_capture(
                        input.capture_uuid,
                        cls.get("subcategory"),
                        cls.get("action_class"),
                        input.raw,
                    )
                except Exception:
                    logger.warning(
                        "project_activity capture record failed",
                        extra={"ctx": {"item_id": input.capture_uuid}},
                        exc_info=True,
                    )
            except Exception:
                logger.error(
                    "capture_agent write phase failed",
                    extra={"ctx": {"item_id": input.capture_uuid}},
                    exc_info=True,
                )

        return CaptureOutput(
            item_id=input.capture_uuid,
            category=cat,
            subcategory=sub,
            tags=cls.get("tags", []),
            summary=cls.get("summary", ""),
            action_class=cls.get("action_class", "record"),
            shadow=False,
            embedding_stored=embedding_stored,
            links_created=wikilink_count + embedding_link_count,
            decisions=decisions,
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_IST = timezone(timedelta(hours=5, minutes=30))

_WEEKDAY_MAP: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2,
    "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}
_WEEKDAY_RE = re.compile(
    r'^(?:next\s+|this\s+)?(' +
    '|'.join(sorted(_WEEKDAY_MAP, key=len, reverse=True)) +
    r')(?:\s+at\s+(.+))?$',
    re.IGNORECASE,
)
_TIME_PRESENT_RE = re.compile(r'\d{1,2}[:h]\d{2}|\d{1,2}\s*(?:am|pm)', re.IGNORECASE)

_EVENT_KEYWORDS = frozenset({"call", "meeting", "appointment", "lunch", "dinner", "meet"})

# ---------------------------------------------------------------------------
# Regex date pre-pass — runs before classify, regex result wins over LLM
# ---------------------------------------------------------------------------

_WEEKDAYS_PAT = '|'.join(sorted(_WEEKDAY_MAP, key=len, reverse=True))

# Optional time suffix matched inline: "at 6pm", "at 18:00", "at 6:30am"
_TS = r'(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)|\s+at\s+\d{1,2}:\d{2})'

_DATE_PREPASS_RE: list[re.Pattern] = [
    # longest/most specific first to avoid partial matches
    re.compile(r'\bday\s+after\s+tomorrow' + _TS + r'?', re.IGNORECASE),
    re.compile(r'\btonight\b', re.IGNORECASE),
    re.compile(r'\btoday' + _TS + r'?', re.IGNORECASE),
    re.compile(r'\b(?:tomorrow|tmrw?|tom)\b' + _TS + r'?', re.IGNORECASE),
    re.compile(r'\b(?:next\s+|this\s+)?(?:' + _WEEKDAYS_PAT + r')\b' + _TS + r'?', re.IGNORECASE),
    re.compile(r'\bin\s+\d+\s+(?:days?|weeks?)\b', re.IGNORECASE),
    re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),
    re.compile(r'\b\d{1,2}/\d{1,2}/\d{4}\b'),
    re.compile(r'\bon\s+the\s+\d{1,2}(?:st|nd|rd|th)\b' + _TS + r'?', re.IGNORECASE),
    re.compile(
        r'\b\d{1,2}(?:st|nd|rd|th)\s+'
        r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?'
        r'|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b'
        + _TS + r'?',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?'
        r'|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'\s+\d{1,2}(?:st|nd|rd|th)?\b' + _TS + r'?',
        re.IGNORECASE,
    ),
]


def _extract_date_phrase(raw: str) -> str | None:
    """Deterministic pre-pass: find a date/time phrase in raw before the LLM call.
    First match wins (patterns ordered most-specific first).
    """
    for pat in _DATE_PREPASS_RE:
        m = pat.search(raw)
        if m:
            return m.group(0).strip()
    return None


def _make_ist_dt(d: object, time_str: str | None) -> tuple[datetime, bool]:
    """Build an IST datetime from a date object and optional time string.
    Returns (dt, time_inferred). time_str=None → 09:00, time_inferred=True.
    """
    if time_str:
        try:
            t = _dateutil_parser.parse(time_str, default=datetime(2000, 1, 1, 0, 0, 0))
            return datetime(d.year, d.month, d.day, t.hour, t.minute, 0, tzinfo=_IST), False
        except Exception:
            pass
    return datetime(d.year, d.month, d.day, 9, 0, 0, tzinfo=_IST), True


def _resolve_date_phrase(phrase: str | None, captured_at: datetime) -> tuple[datetime | None, bool]:
    """Resolve a literal date phrase from the LLM to an IST datetime.

    The model returns the verbatim phrase ("next Friday", "tomorrow at 3pm",
    "25th September") — this function does all arithmetic, so the model never
    computes dates.

    Returns (starts_at_dt, time_inferred):
      time_inferred=True when no time of day was stated (defaulted to 09:00 IST).
      Returns (None, False) on empty input or parse failure.
    Logs a warning when result is more than 24 h before captured_at.
    """
    if not phrase:
        return None, False
    p   = phrase.strip()
    p_l = p.lower()
    cap = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=_IST)

    # today [at <time>]
    if p_l.startswith("today"):
        time_part = re.sub(r'^today\s*(?:at\s*)?', '', p_l).strip() or None
        return _make_ist_dt(cap.date(), time_part)

    # tonight
    if p_l == "tonight":
        return datetime(cap.year, cap.month, cap.day, 20, 0, 0, tzinfo=_IST), False

    # day after tomorrow [at <time>]
    if re.match(r'^day\s+after\s+tomorrow', p_l):
        time_part = re.sub(r'^day\s+after\s+tomorrow\s*(?:at\s*)?', '', p_l).strip() or None
        return _make_ist_dt((cap + timedelta(days=2)).date(), time_part)

    # tomorrow [at <time>]
    if re.match(r'^tomorrow|^tmr(?:w)?$|^tom$', p_l):
        time_part = re.sub(r'^tomorrow\s*(?:at\s*)?', '', p_l).strip() or None
        return _make_ist_dt((cap + timedelta(days=1)).date(), time_part)

    # in N days / in N weeks
    m = re.match(r'^in\s+(\d+)\s+(days?|weeks?)$', p_l)
    if m:
        n = int(m.group(1))
        delta = timedelta(weeks=n) if m.group(2).startswith('week') else timedelta(days=n)
        return _make_ist_dt((cap + delta).date(), None)

    # (next|this) <weekday> [at <time>]
    wd_m = _WEEKDAY_RE.match(p_l)
    if wd_m:
        target_dow  = _WEEKDAY_MAP[wd_m.group(1).lower()]
        cur_dow     = cap.weekday()                         # 0=Monday
        days_ahead  = (target_dow - cur_dow) % 7 or 7      # 0 → next week
        return _make_ist_dt((cap + timedelta(days=days_ahead)).date(), (wd_m.group(2) or "").strip() or None)

    # dateutil fallback — absolute dates ("25th September", "Sep 25", "25/9/2026")
    # Strip leading prepositions that confuse dateutil ("on the 25th" → "25th")
    for _pfx in ("on the ", "on ", "by the ", "by ", "at "):
        if p_l.startswith(_pfx):
            p = p[len(_pfx):]
            break
    try:
        default = datetime(cap.year, cap.month, cap.day, 9, 0, 0)
        dt = _dateutil_parser.parse(p, default=default, dayfirst=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        time_inferred = not bool(_TIME_PRESENT_RE.search(p))
        if time_inferred:
            dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
    except Exception:
        logger.warning("date_phrase parse failed: %r", phrase)
        return None, False

    if dt < cap - timedelta(hours=24):
        logger.warning(
            "starts_at %s is more than 24h before capture time %s — possible misparse",
            dt.isoformat(), cap.isoformat(),
        )
    return dt, time_inferred


def _build_prompt(raw: str, source: str, capture_type: str, captured_at: datetime) -> str:
    type_context = {
        "url": "This is extracted text from a web page. Classify by the page content, not the URL string.",
        "youtube": "This is a YouTube video transcript. Classify by the video content/topic.",
        "pdf": "This is text extracted from a PDF document. Classify by document content.",
    }.get(capture_type, "")

    quote_hint = ""
    stripped = raw.strip()
    attribution = re.search(r'[—~\-]\s*([A-Z][^\n\-~—]{1,50}?)\s*$', stripped)
    if attribution:
        has_quotes = any(c in stripped for c in ('"', '"', '"', '\u201c', '\u201d'))
        if has_quotes or len(stripped) < 300:
            quote_hint = "If this looks like a quote or saying, set category=learning, subcategory=quotes, and include the author in tags if identifiable."
    elif (stripped.startswith(('"', '\u201c')) and stripped.endswith(('"', '\u201d'))) or \
         (stripped.startswith("'") and stripped.endswith("'") and len(stripped) > 10):
        quote_hint = "If this looks like a quote or saying, set category=learning, subcategory=quotes, and include the author in tags if identifiable."

    captured_at_str = captured_at.strftime("%A %Y-%m-%d %H:%M")
    return _load_prompt().format(
        content=raw[:1000],
        source=source,
        capture_type=capture_type,
        captured_at=captured_at_str,
        type_context=type_context,
        quote_hint=quote_hint,
    )


def _call_1minai_sync(prompt: str, model: str) -> str:
    r = httpx.post(
        f"{os.environ['ONEMIN_API_URL']}/api/chat-with-ai",
        headers={"API-KEY": os.environ["ONEMIN_API_KEY"], "Content-Type": "application/json"},
        json={"type": "UNIFY_CHAT_WITH_AI", "model": model, "promptObject": {"prompt": prompt}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["aiRecord"]["aiRecordDetail"]["resultObject"][0]


def _classify(raw: str, source: str, capture_type: str) -> dict:
    prompt = _build_prompt(raw, source, capture_type, datetime.now(_IST))
    try:
        resp = _call_1minai_sync(prompt, _ONEMIN_MODEL)
    except Exception:
        logger.warning("1min.ai classify failed, trying fallback", exc_info=True)
        try:
            resp = _call_1minai_sync(prompt, _FALLBACK_MODEL)
        except Exception:
            logger.error("classify fallback also failed", exc_info=True)
            return {"category": "thoughts", "action_class": "record", "tags": [], "summary": ""}

    clean = re.sub(r"```(?:json)?\n?", "", resp).strip().strip("`")
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.error("classify JSON parse failed: %r", clean[:200])
        return {"category": "thoughts", "action_class": "record", "tags": [], "summary": ""}


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed(text: str) -> Optional[list[float]]:
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        logger.debug("GOOGLE_API_KEY not set — skipping embed")
        return None
    try:
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_EMBED_MODEL}:embedContent",
            params={"key": api_key},
            json={
                "model": f"models/{_EMBED_MODEL}",
                "content": {"parts": [{"text": text[:8000]}]},
                "outputDimensionality": _EMBED_DIMS,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["embedding"]["values"]
    except Exception:
        logger.warning("embed failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Write helpers — all accept an open connection; caller owns commit
# ---------------------------------------------------------------------------

def _write_classification(
    item_id: str, cls: dict, conn: psycopg.Connection, captured_at: datetime
) -> tuple[datetime | None, bool]:
    """Write classification + temporal fields in a single UPDATE.

    Returns (starts_at_dt, time_inferred) so the caller can log a decision.
    """
    tags = cls.get("tags", [])
    starts_at_dt, time_inferred = _resolve_date_phrase(cls.get("date_phrase"), captured_at)
    is_event = bool(cls.get("is_event", False))
    conn.execute(
        """UPDATE items SET
               category              = %s,
               subcategory           = %s,
               ai_tags               = %s,
               ai_summary            = %s,
               action_class          = %s,
               classification_status = 'done',
               starts_at             = %s,
               is_event              = %s,
               time_inferred         = %s
           WHERE id = %s""",
        (
            cls.get("category", "thoughts"),
            cls.get("subcategory"),
            Jsonb(tags),
            cls.get("summary", ""),
            cls.get("action_class", "record"),
            starts_at_dt,
            is_event,
            time_inferred,
            item_id,
        ),
    )
    return starts_at_dt, time_inferred


def _store_embedding(item_id: str, vector: list[float], conn: psycopg.Connection) -> None:
    vec_str = "[" + ",".join(str(v) for v in vector) + "]"
    conn.execute(
        "UPDATE items SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
        (vec_str, _EMBED_MODEL, item_id),
    )


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

def _link_wikilinks(item_id: str, raw: str, conn: psycopg.Connection) -> int:
    """
    Extract [[title]] patterns from raw, resolve against items.title (exact,
    case-insensitive). Write one thought_links row per match.

    source = this item (the one containing [[brackets]])
    target = the item whose title matches the bracket text

    Unmatched brackets produce no row — they stay as plain text in raw_content
    and are resolved on-demand via live LIKE scan when fetching backlinks.

    Duplicate titles: if multiple items share a title, LIMIT 1 picks an
    arbitrary winner. Undefined behaviour by design — no fix this weekend.
    """
    titles = _WIKILINK_RE.findall(raw)
    if not titles:
        return 0

    created = 0
    for title_text in {t.strip() for t in titles}:  # deduplicate
        if not title_text:
            continue
        row = conn.execute(
            """SELECT id FROM items
                WHERE lower(title) = lower(%s)
                  AND status = 'active'
                  AND id != %s
                LIMIT 1""",
            (title_text, item_id),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            """INSERT INTO thought_links
                   (source_item_id, target_item_id, link_type, wikilink_text)
               VALUES (%s, %s, 'wikilink', %s)
               ON CONFLICT (source_item_id, target_item_id, link_type) DO NOTHING""",
            (item_id, str(row[0]), title_text),
        )
        created += 1

    return created


def _link_embeddings(item_id: str, vector: list[float], conn: psycopg.Connection) -> int:
    """
    Find existing items with cosine similarity above _SIM_THRESHOLD, cap at
    _MAX_SIM_LINKS. Reuses the vector already computed — no new embed call.

    Fetches _MAX_SIM_LINKS * 3 candidates so the threshold filter has headroom
    without a second query.
    """
    vec_str = "[" + ",".join(str(v) for v in vector) + "]"
    rows = conn.execute(
        """SELECT id, 1 - (embedding <=> %s::vector) AS score
             FROM items
            WHERE id != %s
              AND embedding IS NOT NULL
              AND status = 'active'
            ORDER BY embedding <=> %s::vector
            LIMIT %s""",
        (vec_str, item_id, vec_str, _MAX_SIM_LINKS * 3),
    ).fetchall()

    created = 0
    for target_id, score in rows:
        if score < _SIM_THRESHOLD:
            continue
        if created >= _MAX_SIM_LINKS:
            break
        conn.execute(
            """INSERT INTO thought_links
                   (source_item_id, target_item_id, link_type, similarity_score)
               VALUES (%s, %s, 'embedding', %s)
               ON CONFLICT (source_item_id, target_item_id, link_type) DO NOTHING""",
            (item_id, str(target_id), round(score, 4)),
        )
        created += 1

    return created


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

_agent = CaptureAgent()


def capture_agent_node(state: GraphState) -> dict:
    capture_uuid = state.get("capture_uuid")
    if not capture_uuid:
        logger.error("capture_agent_node: no capture_uuid in state — skipping")
        return {"specialist_result": {"error": "no capture_uuid"}}

    result = _agent.handle(
        CaptureInput(
            raw=state["raw_input"],
            source=state["source"],
            capture_uuid=capture_uuid,
        )
    )
    return {"specialist_result": result.model_dump()}
