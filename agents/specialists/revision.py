"""
RevisionAgent — spaced-repetition specialist.

Three behaviors dispatched from raw_input:

  /revise generate <subject_hint>
      Fetches unreviewed notebook items → LLM generates questions →
      INSERTs revision_questions + outbox in one transaction.

  /revise           → write next due question to outbox
  /revise answer X  → grade X against due question, update SM state, outbox result
  /revise skip      → defer question by 1 day, outbox confirmation

chat_id is read from items.metadata['chat_id'], seeded by telegram_bot.py.
All replies go through outbox — no synchronous HTTP responses.
"""
import json
import logging
import os
import pathlib
import re
from typing import ClassVar, Optional

import httpx
import psycopg
from dotenv import load_dotenv

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

load_dotenv()
logger = logging.getLogger("brain")

ONEMIN_PRIMARY = "claude-haiku-4-5-20251001"
ONEMIN_FALLBACK = "gpt-4o-mini"

SUBJECT_ALIASES: dict[str, str] = {
    "os": "Operating Systems",
    "operating": "Operating Systems",
    "dbms": "DBMS",
    "db": "DBMS",
    "database": "DBMS",
    "cn": "Computer Networks",
    "net": "Computer Networks",
    "network": "Computer Networks",
    "networks": "Computer Networks",
    "algo": "Algorithms",
    "dsa": "Algorithms",
    "algorithms": "Algorithms",
    "algorithm": "Algorithms",
}

LADDER = [1, 3, 7, 21]

# Fail loud at import time if prompt files are missing.
_GENERATE_TEMPLATE: str = (
    pathlib.Path(__file__).parent.parent.parent / "prompts" / "revise_generate.txt"
).read_text()

_GRADE_TEMPLATE: str = (
    pathlib.Path(__file__).parent.parent.parent / "prompts" / "revise_grade.txt"
).read_text()

_DUE_QUERY = """
    SELECT id, question, expected_answer, interval_days, notebook_id
    FROM revision_questions
    WHERE next_review_date <= CURRENT_DATE
      AND archived_at IS NULL
    ORDER BY next_review_date ASC, id ASC
    LIMIT 1
"""


class RevisionInput(NarrowModel):
    raw_input: str
    item_id: Optional[str] = None


class RevisionOutput(NarrowModel):
    action: str  # "generated" | "showed" | "graded" | "skipped" | "empty" | "error"
    message: str
    decisions: list[dict] = []


def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


def next_interval(current: int, score: int) -> int:
    if score < 7:
        return 1
    idx = LADDER.index(current) if current in LADDER else 0
    return LADDER[min(idx + 1, len(LADDER) - 1)]


class RevisionAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.flash
    requires_context: ClassVar[list[str]] = []

    InputSchema = RevisionInput
    OutputSchema = RevisionOutput

    def handle(self, input: RevisionInput) -> RevisionOutput:
        raw = input.raw_input.strip()
        after_slash = re.sub(r"^/revise\s*", "", raw, flags=re.IGNORECASE).strip()
        chat_id = self._get_chat_id(input.item_id)

        lower = after_slash.lower()
        if lower.startswith("generate"):
            subject_hint = after_slash[len("generate"):].strip()
            return self._handle_generate(subject_hint, chat_id)
        if lower.startswith("answer"):
            user_answer = after_slash[len("answer"):].strip()
            return self._handle_answer(user_answer, chat_id)
        if lower == "skip":
            return self._handle_skip(chat_id)
        return self._handle_show(chat_id)

    # ------------------------------------------------------------------

    def _get_chat_id(self, item_id: Optional[str]) -> Optional[str]:
        if not item_id:
            return None
        url = _db_url()
        if not url:
            return None
        try:
            with psycopg.connect(url) as conn:
                row = conn.execute(
                    "SELECT metadata FROM items WHERE id = %s", (item_id,)
                ).fetchone()
            if row and row[0] and "chat_id" in row[0]:
                return str(row[0]["chat_id"])
        except Exception:
            logger.exception("chat_id lookup failed item_id=%s", item_id)
        return None

    def _write_outbox(self, conn, chat_id: str, message: str) -> None:
        conn.execute(
            """INSERT INTO outbox (channel, recipient, message, status)
               VALUES ('telegram', %s, %s, 'pending')""",
            (chat_id, message),
        )

    def _call_llm(self, prompt: str) -> str:
        for model in (ONEMIN_PRIMARY, ONEMIN_FALLBACK):
            try:
                with httpx.Client(timeout=30) as client:
                    r = client.post(
                        f"{os.environ['ONEMIN_API_URL']}/api/chat-with-ai",
                        headers={
                            "API-KEY": os.environ["ONEMIN_API_KEY"],
                            "Content-Type": "application/json",
                        },
                        json={
                            "type": "UNIFY_CHAT_WITH_AI",
                            "model": model,
                            "promptObject": {"prompt": prompt},
                        },
                    )
                    r.raise_for_status()
                    return r.json()["aiRecord"]["aiRecordDetail"]["resultObject"][0]
            except Exception as exc:
                logger.warning("_call_llm model=%s failed: %s", model, exc)
        raise RuntimeError("all LLM models failed")

    # ------------------------------------------------------------------

    def _handle_generate(self, subject_hint: str, chat_id: Optional[str]) -> RevisionOutput:
        notebook_name = SUBJECT_ALIASES.get(subject_hint.lower(), subject_hint)
        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                nb_row = conn.execute(
                    "SELECT id FROM notebooks WHERE name = %s AND archived_at IS NULL LIMIT 1",
                    (notebook_name,),
                ).fetchone()
                if nb_row is None:
                    return RevisionOutput(action="error", message=f"Notebook {notebook_name!r} not found")
                notebook_id = nb_row[0]

                items = conn.execute(
                    """
                    SELECT id, raw_content
                    FROM items
                    WHERE notebook_id = %s
                      AND status = 'active'
                      AND id != ALL(
                          SELECT unnest(source_item_ids)
                          FROM revision_questions
                          WHERE notebook_id = %s
                      )
                    ORDER BY created_at ASC
                    """,
                    (notebook_id, notebook_id),
                ).fetchall()
        except Exception as exc:
            logger.exception("generate: DB read failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        if not items:
            msg = f"No new items to generate questions from in [{notebook_name}]."
            if chat_id:
                try:
                    with psycopg.connect(url) as conn:
                        self._write_outbox(conn, chat_id, msg)
                        conn.commit()
                except Exception:
                    logger.exception("generate: outbox write failed (empty)")
            return RevisionOutput(action="empty", message=msg)

        n_questions = min(5, max(3, len(items)))
        selected = items[:n_questions]
        notes = "\n\n".join(f"[{i+1}] {row[1]}" for i, row in enumerate(selected))
        source_ids = [str(row[0]) for row in selected]

        # LLM call — outside write transaction
        try:
            prompt = _GENERATE_TEMPLATE.format(
                subject=notebook_name,
                n_questions=n_questions,
                notes=notes,
            )
            raw_response = self._call_llm(prompt)
            clean = raw_response.strip().replace("```json", "").replace("```", "").strip()
            questions = json.loads(clean)
            if not isinstance(questions, list) or not questions:
                raise ValueError("LLM returned empty or non-list")
        except Exception as exc:
            logger.exception("generate: LLM call or parse failed")
            return RevisionOutput(action="error", message=f"Generation failed: {exc}")

        msg = f"Generated {len(questions)} questions for {notebook_name}."
        try:
            with psycopg.connect(url) as conn:
                for q in questions:
                    conn.execute(
                        """
                        INSERT INTO revision_questions
                            (notebook_id, source_item_ids, question, expected_answer,
                             next_review_date, interval_days)
                        VALUES (%s, %s::uuid[], %s, %s, CURRENT_DATE, 1)
                        """,
                        (notebook_id, source_ids, q["question"], q["expected_answer"]),
                    )
                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                conn.commit()
        except Exception as exc:
            logger.exception("generate: write transaction failed")
            return RevisionOutput(action="error", message=f"DB write failed: {exc}")

        return RevisionOutput(action="generated", message=msg)

    def _handle_show(self, chat_id: Optional[str]) -> RevisionOutput:
        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                question = conn.execute(_DUE_QUERY).fetchone()

                if question is None:
                    msg = "No questions due for review. Come back later!"
                else:
                    nb_row = conn.execute(
                        "SELECT name FROM notebooks WHERE id = %s", (question[4],)
                    ).fetchone()
                    nb_name = nb_row[0] if nb_row else "Unknown"
                    msg = f"*[{nb_name}]*\n\n{question[1]}"

                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                    conn.commit()
        except Exception as exc:
            logger.exception("show: failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        action = "empty" if question is None else "showed"
        return RevisionOutput(action=action, message=msg)

    def _handle_answer(self, user_answer: str, chat_id: Optional[str]) -> RevisionOutput:
        if not user_answer:
            return RevisionOutput(action="error", message="Usage: /revise answer <your answer>")

        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                question = conn.execute(_DUE_QUERY).fetchone()
        except Exception as exc:
            logger.exception("answer: due-queue read failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        if question is None:
            msg = "No questions due for review."
            if chat_id:
                try:
                    with psycopg.connect(url) as conn:
                        self._write_outbox(conn, chat_id, msg)
                        conn.commit()
                except Exception:
                    logger.exception("answer: outbox write failed (empty)")
            return RevisionOutput(action="empty", message=msg)

        q_id, q_text, q_expected, q_interval, _ = question

        # Grade — outside write transaction
        try:
            prompt = _GRADE_TEMPLATE.format(
                question=q_text,
                expected_answer=q_expected,
                user_answer=user_answer,
            )
            raw_score = self._call_llm(prompt).strip()
            m = re.search(r"\d+", raw_score)
            if not m:
                raise ValueError(f"no digit in LLM grade response: {raw_score!r}")
            score = max(0, min(10, int(m.group())))
        except Exception as exc:
            logger.exception("answer: LLM grade failed")
            return RevisionOutput(action="error", message=f"Grading failed: {exc}")

        interval_after = next_interval(current=q_interval, score=score)
        suffix = "day" if interval_after == 1 else "days"
        msg = f"Score: {score}/10. Next review in {interval_after} {suffix}."

        try:
            with psycopg.connect(url) as conn:
                conn.execute(
                    """
                    INSERT INTO revision_reviews (question_id, score, interval_days_after)
                    VALUES (%s, %s, %s)
                    """,
                    (q_id, score, interval_after),
                )
                conn.execute(
                    """
                    UPDATE revision_questions
                       SET interval_days    = %s,
                           next_review_date = CURRENT_DATE + %s
                     WHERE id = %s
                    """,
                    (interval_after, interval_after, q_id),
                )
                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                conn.commit()
        except Exception as exc:
            logger.exception("answer: write transaction failed")
            return RevisionOutput(action="error", message=f"DB write failed: {exc}")

        return RevisionOutput(action="graded", message=msg)

    def _handle_skip(self, chat_id: Optional[str]) -> RevisionOutput:
        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                question = conn.execute(_DUE_QUERY).fetchone()
        except Exception as exc:
            logger.exception("skip: due-queue read failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        if question is None:
            msg = "No questions due for review."
            if chat_id:
                try:
                    with psycopg.connect(url) as conn:
                        self._write_outbox(conn, chat_id, msg)
                        conn.commit()
                except Exception:
                    logger.exception("skip: outbox write failed (empty)")
            return RevisionOutput(action="empty", message=msg)

        q_id = question[0]
        msg = "Skipped. Question returns tomorrow."

        try:
            with psycopg.connect(url) as conn:
                conn.execute(
                    """
                    UPDATE revision_questions
                       SET next_review_date = CURRENT_DATE + 1
                     WHERE id = %s
                    """,
                    (q_id,),
                )
                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                conn.commit()
        except Exception as exc:
            logger.exception("skip: write transaction failed")
            return RevisionOutput(action="error", message=f"DB write failed: {exc}")

        return RevisionOutput(action="skipped", message=msg)


_agent = RevisionAgent()


def revision_agent_node(state: GraphState) -> dict:
    result = _agent.handle(RevisionInput(
        raw_input=state["raw_input"],
        item_id=state.get("capture_uuid"),
    ))
    return {"specialist_result": result.model_dump()}
