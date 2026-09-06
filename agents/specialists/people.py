"""
PeopleAgent — entity resolution for person names extracted from captures.

Two entry points:
  run_people_extraction(item_id, raw_content, source)
      Called from job_queue worker for 'people_extraction' jobs.
      One gpt-4o-mini call -> per-name three-band trigram logic -> DB writes.

  people_agent_node(state)
      Graph node. Dispatches on state['people_action']:
        resolve_conflict — YES/NO/SKIP a pending people_conflict row
        list_pending    — /people pending: list open + snoozed conflicts
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import ClassVar, Optional

import httpx
import psycopg
import psycopg.errors

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

logger = logging.getLogger("brain")

_ONEMIN_MODEL = "gpt-4o-mini"
_FALLBACK_MODEL = "gpt-4.1-nano"

_HIGH_SIM = 0.85
_LOW_SIM  = 0.50

_EXTRACT_SUBCATEGORIES   = frozenset({"finance", "quotes", "wayclear", "accrediq"})
_EXTRACT_NULL_CATEGORIES = frozenset({"thoughts", "life", "finance"})

_PROMPT_TMPL: Optional[str] = None


def _load_prompt() -> str:
    global _PROMPT_TMPL
    if _PROMPT_TMPL is None:
        p = Path(__file__).parent.parent.parent / "prompts" / "people_extract.txt"
        _PROMPT_TMPL = p.read_text()
    return _PROMPT_TMPL


def should_extract_people(category: Optional[str], subcategory: Optional[str]) -> bool:
    if subcategory == "null":
        subcategory = None
    if subcategory in _EXTRACT_SUBCATEGORIES:
        return True
    if subcategory is None and category in _EXTRACT_NULL_CATEGORIES:
        return True
    return False


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PeopleInput(NarrowModel):
    people_action: str              # resolve_conflict | list_pending
    source: str                     # Telegram chat ID — outbox recipient
    conflict_id: Optional[str] = None
    conflict_answer: Optional[str] = None   # yes | no | skip


class PeopleOutput(NarrowModel):
    action: str
    outcome: str                    # merged | rejected | snoozed | listed | error
    resolved_conflict_id: Optional[str] = None
    pending_count: Optional[int] = None
    decisions: list[dict] = []


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class PeopleAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.flash
    requires_context: ClassVar[list[str]] = []

    InputSchema = PeopleInput
    OutputSchema = PeopleOutput

    def handle(self, input: PeopleInput) -> PeopleOutput:
        url = os.environ.get("BRAIN_DB_URL", "")
        if not url:
            logger.error("people_agent: BRAIN_DB_URL not set")
            return PeopleOutput(action=input.people_action, outcome="error")

        if input.people_action == "resolve_conflict":
            return self._resolve_conflict(input, url)
        if input.people_action == "list_pending":
            return self._list_pending(input, url)

        logger.error("people_agent: unknown action %r", input.people_action)
        return PeopleOutput(action=input.people_action, outcome="error")

    def _resolve_conflict(self, input: PeopleInput, url: str) -> PeopleOutput:
        if not input.conflict_id or not input.conflict_answer:
            logger.error("people_agent: resolve_conflict missing conflict_id or answer")
            return PeopleOutput(action="resolve_conflict", outcome="error")

        answer = input.conflict_answer.lower()
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT candidate_person_id, mention_text FROM people_conflicts WHERE id = %s",
                (input.conflict_id,),
            ).fetchone()

            if row is None:
                logger.error("people_agent: conflict %s not found", input.conflict_id)
                return PeopleOutput(action="resolve_conflict", outcome="error")

            candidate_id, mention_text = str(row[0]), row[1]

            if answer == "yes":
                conn.execute(
                    "UPDATE people_conflicts SET status='merged', resolved_at=now() WHERE id=%s",
                    (input.conflict_id,),
                )
                # Scope sweep to mentions from items where the conflict was specifically
                # against this candidate — prevents incorrectly resolving a same-text
                # conflict against a different candidate_person_id.
                conn.execute(
                    """UPDATE people_mentions
                       SET person_id = %s, match_type = 'fuzzy'
                       WHERE match_type = 'pending_conflict'
                         AND person_id IS NULL
                         AND lower(matched_text) = lower(%s)
                         AND item_id IN (
                             SELECT item_id FROM people_conflicts
                             WHERE candidate_person_id = %s
                               AND lower(mention_text) = lower(%s)
                         )""",
                    (candidate_id, mention_text, candidate_id, mention_text),
                )
                conn.execute(
                    "UPDATE people SET status='confirmed' WHERE id=%s AND status='provisional'",
                    (candidate_id,),
                )
                reply = f'Merged \u2014 all \u201c{mention_text}\u201d mentions linked to existing record.'
                outcome = "merged"

            elif answer == "no":
                new_id = str(conn.execute(
                    """INSERT INTO people (name, name_normalized, status)
                       VALUES (%s, %s, 'provisional') RETURNING id""",
                    (mention_text, mention_text.lower().strip()),
                ).fetchone()[0])
                conn.execute(
                    "UPDATE people_conflicts SET status='rejected', resolved_at=now() WHERE id=%s",
                    (input.conflict_id,),
                )
                conn.execute(
                    """UPDATE people_mentions
                       SET person_id = %s, match_type = 'fuzzy'
                       WHERE match_type = 'pending_conflict'
                         AND person_id IS NULL
                         AND lower(matched_text) = lower(%s)
                         AND item_id IN (
                             SELECT item_id FROM people_conflicts
                             WHERE candidate_person_id = %s
                               AND lower(mention_text) = lower(%s)
                         )""",
                    (new_id, mention_text, candidate_id, mention_text),
                )
                reply = f'Kept separate \u2014 new provisional record created for \u201c{mention_text}\u201d.'
                outcome = "rejected"

            else:  # skip
                conn.execute(
                    "UPDATE people_conflicts SET status='snoozed', resolved_at=now() WHERE id=%s",
                    (input.conflict_id,),
                )
                reply = "Snoozed \u2014 use /people pending to revisit."
                outcome = "snoozed"

            conn.execute(
                "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                (input.source, reply),
            )
            conn.commit()

        return PeopleOutput(
            action="resolve_conflict",
            outcome=outcome,
            resolved_conflict_id=input.conflict_id,
            decisions=[{
                "agent_name": "people_agent",
                "action_taken": f"resolve_conflict:{outcome}",
                "reason": f"user answered {answer!r} for conflict {input.conflict_id}",
                "interrupt_tier": "log_only",
            }],
        )

    def _list_pending(self, input: PeopleInput, url: str) -> PeopleOutput:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                """SELECT pc.id, pc.mention_text, pc.similarity_score, pc.status,
                          p.name AS candidate_name, p.status AS person_status,
                          pc.created_at
                   FROM people_conflicts pc
                   JOIN people p ON p.id = pc.candidate_person_id
                   WHERE pc.status IN ('pending', 'snoozed')
                   ORDER BY pc.status DESC, pc.created_at ASC""",
            ).fetchall()

            if not rows:
                msg = "No open people conflicts."
            else:
                lines = ["Open people conflicts:"]
                for row in rows:
                    conflict_id, name, sim, status, candidate, cand_status, created_at = row
                    code = str(conflict_id).replace("-", "")[-6:]
                    lines.append(
                        f"\u2022 {code}  {name}  ({status}, {sim:.2f})"
                        f" \u2014 vs. {candidate} [{cand_status}] on {created_at:%b %-d}"
                    )
                lines.append("Reply YES/NO/SKIP <code> to resolve.")
                msg = "\n".join(lines)

            conn.execute(
                "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                (input.source, msg),
            )
            conn.commit()

        return PeopleOutput(
            action="list_pending",
            outcome="listed",
            pending_count=len(rows),
            decisions=[{
                "agent_name": "people_agent",
                "action_taken": "list_pending",
                "reason": "/people pending command",
                "interrupt_tier": "log_only",
            }],
        )


# ---------------------------------------------------------------------------
# Extraction job — called from job_queue worker via run_in_executor
# ---------------------------------------------------------------------------

def run_people_extraction(item_id: str, raw_content: str, source: str) -> None:
    names = _extract_names(raw_content)
    if not names:
        logger.info("people_extraction: no names found", extra={"ctx": {"item_id": item_id}})
        return

    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        logger.error("people_extraction: BRAIN_DB_URL not set")
        return

    # Deduplicate by normalized name, keeping longest form ("Athira Menon" over "Athira").
    seen: dict[str, dict] = {}
    for entry in names:
        key = entry["name"].lower().strip()
        if key not in seen or len(entry["name"]) > len(seen[key]["name"]):
            seen[key] = entry
    names = list(seen.values())

    try:
        with psycopg.connect(url) as conn:
            for entry in names:
                # Per-name savepoint: people + people_mentions are atomic together.
                # If either INSERT fails, sp_name rollback undoes both — no orphan people rows.
                conn.execute("SAVEPOINT sp_name")
                try:
                    _process_name(conn, item_id, entry["name"], entry["context"], source)
                    conn.execute("RELEASE SAVEPOINT sp_name")
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT sp_name")
                    conn.execute("RELEASE SAVEPOINT sp_name")
                    logger.error(
                        "people_extraction: skipping name %r",
                        entry["name"],
                        extra={"ctx": {"item_id": item_id}},
                        exc_info=True,
                    )
            conn.commit()
    except Exception:
        logger.error(
            "people_extraction: write phase failed",
            extra={"ctx": {"item_id": item_id}},
            exc_info=True,
        )


def _extract_names(raw_content: str) -> list[dict]:
    prompt = _load_prompt().format(raw_content=raw_content[:1000])
    try:
        resp = _call_1minai(prompt, _ONEMIN_MODEL)
    except Exception:
        logger.warning("people_extraction: 1min.ai call failed, trying fallback", exc_info=True)
        try:
            resp = _call_1minai(prompt, _FALLBACK_MODEL)
        except Exception:
            logger.error("people_extraction: fallback also failed", exc_info=True)
            return []

    clean = re.sub(r"```(?:json)?\n?", "", resp).strip().strip("`")
    try:
        data = json.loads(clean)
        return data.get("people", [])
    except json.JSONDecodeError:
        logger.error("people_extraction: JSON parse failed: %r", clean[:200])
        return []


def _call_1minai(prompt: str, model: str) -> str:
    r = httpx.post(
        f"{os.environ['ONEMIN_API_URL']}/api/chat-with-ai",
        headers={"API-KEY": os.environ["ONEMIN_API_KEY"], "Content-Type": "application/json"},
        json={"type": "UNIFY_CHAT_WITH_AI", "model": model, "promptObject": {"prompt": prompt}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["aiRecord"]["aiRecordDetail"]["resultObject"][0]


def _process_name(
    conn: psycopg.Connection,
    item_id: str,
    name: str,
    context: str,
    source: str,
) -> None:
    rows = conn.execute(
        """SELECT id, name, created_at, similarity(name_normalized, lower(%s)) AS sim
           FROM people
           WHERE similarity(name_normalized, lower(%s)) > 0.45
           ORDER BY sim DESC LIMIT 3""",
        (name, name),
    ).fetchall()

    if rows and rows[0][3] >= _HIGH_SIM:
        best_id = str(rows[0][0])
        conn.execute(
            """INSERT INTO people_mentions (item_id, person_id, matched_text, match_type)
               VALUES (%s, %s, %s, 'exact')""",
            (item_id, best_id, name),
        )
        conn.execute(
            "UPDATE people SET status='confirmed' WHERE id=%s AND status='provisional'",
            (best_id,),
        )
        logger.info("people: auto-linked", extra={"ctx": {"name": name, "person_id": best_id}})

    elif rows and rows[0][3] >= _LOW_SIM:
        best_id       = str(rows[0][0])
        candidate_name   = rows[0][1]
        candidate_created = rows[0][2]
        best_sim      = rows[0][3]

        # Try to create conflict row — uq_pc_open enforces ask-once per candidate+text.
        conn.execute("SAVEPOINT sp_conflict")
        conflict_id = None
        try:
            conflict_id = str(conn.execute(
                """INSERT INTO people_conflicts
                   (candidate_person_id, mention_text, item_id, similarity_score)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (best_id, name, item_id, float(best_sim)),
            ).fetchone()[0])
            conn.execute("RELEASE SAVEPOINT sp_conflict")
        except psycopg.errors.UniqueViolation:
            # Already an open conflict for this candidate+name — write mention, skip outbox.
            conn.execute("ROLLBACK TO SAVEPOINT sp_conflict")
            conn.execute("RELEASE SAVEPOINT sp_conflict")

        conn.execute(
            """INSERT INTO people_mentions (item_id, person_id, matched_text, match_type)
               VALUES (%s, NULL, %s, 'pending_conflict')""",
            (item_id, name),
        )

        if conflict_id:
            code = conflict_id.replace("-", "")[-6:]
            msg = (
                f'People: \u201c{context}\u201d \u2014 is this the same {candidate_name}\n'
                f"you mentioned on {candidate_created:%b %-d}?\n\n"
                f"Reply YES {code}, NO {code}, or SKIP {code}."
            )
            conn.execute(
                "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                (source, msg),
            )
            conn.execute(
                "UPDATE people_conflicts SET asked_at=now() WHERE id=%s",
                (conflict_id,),
            )
        logger.info(
            "people: conflict",
            extra={"ctx": {"name": name, "candidate": candidate_name, "asked": conflict_id is not None}},
        )

    else:
        new_id = str(conn.execute(
            """INSERT INTO people (name, name_normalized, status)
               VALUES (%s, %s, 'provisional') RETURNING id""",
            (name, name.lower().strip()),
        ).fetchone()[0])
        conn.execute(
            """INSERT INTO people_mentions (item_id, person_id, matched_text, match_type)
               VALUES (%s, %s, %s, 'fuzzy')""",
            (item_id, new_id, name),
        )
        logger.info("people: new provisional", extra={"ctx": {"name": name, "person_id": new_id}})


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

_agent = PeopleAgent()


def people_agent_node(state: GraphState) -> dict:
    result = _agent.handle(PeopleInput(
        people_action=state.get("people_action") or "",
        source=state.get("source") or "",
        conflict_id=state.get("conflict_id"),
        conflict_answer=state.get("conflict_answer"),
    ))
    return {"specialist_result": result.model_dump()}
