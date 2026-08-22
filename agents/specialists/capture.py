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
from pathlib import Path
from typing import ClassVar, Optional

import httpx
import psycopg
from psycopg.types.json import Jsonb

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel
from shortcuts import parse_slash

logger = logging.getLogger("brain")

_ONEMIN_MODEL = "claude-haiku-4-5-20251001"
_FALLBACK_MODEL = "gpt-4o-mini"
_EMBED_MODEL = "gemini-embedding-001"
_EMBED_DIMS = 1536  # matches vector(1536) column; truncated from model's native 3072 via outputDimensionality

_SIM_THRESHOLD = 0.82
_MAX_SIM_LINKS = 5

SHADOW_MODE = False

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
        vector = _embed(input.raw)

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
                category=cls.get("category", "thoughts"),
                subcategory=cls.get("subcategory"),
                tags=cls.get("tags", []),
                summary=cls.get("summary", ""),
                action_class=cls.get("action_class", "record"),
                shadow=True,
            )

        # Non-shadow: one connection, one commit for all writes
        embedding_stored = False
        links_created = 0
        url = os.environ.get("BRAIN_DB_URL", "")
        if url:
            try:
                with psycopg.connect(url) as conn:
                    _write_classification(input.capture_uuid, cls, conn)
                    if vector:
                        _store_embedding(input.capture_uuid, vector, conn)
                        embedding_stored = True
                    links_created += _link_wikilinks(input.capture_uuid, input.raw, conn)
                    if vector:
                        links_created += _link_embeddings(input.capture_uuid, vector, conn)
                    conn.commit()
            except Exception:
                logger.error(
                    "capture_agent write phase failed",
                    extra={"ctx": {"item_id": input.capture_uuid}},
                    exc_info=True,
                )

        return CaptureOutput(
            item_id=input.capture_uuid,
            category=cls.get("category", "thoughts"),
            subcategory=cls.get("subcategory"),
            tags=cls.get("tags", []),
            summary=cls.get("summary", ""),
            action_class=cls.get("action_class", "record"),
            shadow=False,
            embedding_stored=embedding_stored,
            links_created=links_created,
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _build_prompt(raw: str, source: str, capture_type: str) -> str:
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

    return _load_prompt().format(
        content=raw[:1000],
        source=source,
        capture_type=capture_type,
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
    prompt = _build_prompt(raw, source, capture_type)
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

def _write_classification(item_id: str, cls: dict, conn: psycopg.Connection) -> None:
    tags = cls.get("tags", [])
    conn.execute(
        """UPDATE items SET
               category              = %s,
               subcategory           = %s,
               ai_tags               = %s,
               ai_summary            = %s,
               action_class          = %s,
               classification_status = 'done'
           WHERE id = %s""",
        (
            cls.get("category", "thoughts"),
            cls.get("subcategory"),
            Jsonb(tags),
            cls.get("summary", ""),
            cls.get("action_class", "record"),
            item_id,
        ),
    )


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
