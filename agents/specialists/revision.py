"""
RevisionAgent — spaced-repetition specialist.

Handles both /revise and /drill commands (kept together to share the SM
ladder, question pool, and revision_reviews writes).

  /revise generate <subject_hint>  → generate questions from notebook items
  /revise                          → show next due question
  /revise answer X                 → grade answer, update SM state
  /revise skip                     → defer question by 1 day

  /drill start <subject> <N>       → open timed drill session, show Q1
  /drill answer <text>             → record answer, show next question
  /drill submit                    → batch-grade, close session, update SM state

chat_id is read from items.metadata['chat_id'], seeded by telegram_bot.py.
All replies go through outbox — no synchronous HTTP responses.
"""
import json
import logging
import os
import pathlib
import re
import statistics
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

_DRILL_GRADE_TEMPLATE: str = (
    pathlib.Path(__file__).parent.parent.parent / "prompts" / "drill_grade_batch.txt"
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

        drill_match = re.match(r"^/drill\s+(\S+)(.*)", raw, flags=re.IGNORECASE)
        if drill_match:
            subcmd = drill_match.group(1).lower()
            rest = drill_match.group(2).strip()
            chat_id = self._get_chat_id(input.item_id)
            if subcmd == "start":
                return self._handle_drill_start(rest, chat_id)
            if subcmd == "answer":
                return self._handle_drill_answer(rest, chat_id)
            if subcmd == "submit":
                return self._handle_drill_submit(chat_id)
            return RevisionOutput(action="error", message=f"Unknown drill subcommand: {subcmd!r}")

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


    # ------------------------------------------------------------------
    # Drill flow
    # ------------------------------------------------------------------

    def _handle_drill_start(self, rest: str, chat_id) -> RevisionOutput:
        parts = rest.split()
        if len(parts) < 2 or not parts[-1].isdigit():
            return RevisionOutput(action="error", message="Usage: /drill start <subject> <N>")
        n = int(parts[-1])
        subject_hint = " ".join(parts[:-1])
        notebook_name = SUBJECT_ALIASES.get(subject_hint.lower(), subject_hint)

        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                open_sess = conn.execute(
                    "SELECT id FROM drill_sessions WHERE ended_at IS NULL LIMIT 1"
                ).fetchone()
                if open_sess:
                    return RevisionOutput(
                        action="error",
                        message="A drill session is already open. Submit it first with /drill submit.",
                    )

                nb_row = conn.execute(
                    "SELECT id FROM notebooks WHERE name = %s AND notebook_type = 'gate_subject' "
                    "AND archived_at IS NULL LIMIT 1",
                    (notebook_name,),
                ).fetchone()
                if nb_row is None:
                    return RevisionOutput(action="error", message=f"GATE notebook {notebook_name!r} not found.")
                notebook_id = nb_row[0]

                questions = conn.execute(
                    """SELECT id, question
                       FROM revision_questions
                       WHERE notebook_id = %s AND archived_at IS NULL
                       ORDER BY next_review_date ASC NULLS LAST, id ASC
                       LIMIT %s""",
                    (notebook_id, n),
                ).fetchall()

                if not questions:
                    return RevisionOutput(
                        action="empty",
                        message=f"No questions available for {notebook_name}. Run /revise generate {subject_hint} first.",
                    )

                sess_id = conn.execute(
                    """INSERT INTO drill_sessions
                           (notebook_id, started_at, questions_total, questions_answered,
                            verified, reason)
                       VALUES (%s, now(), %s, 0, false, 'pending')
                       RETURNING id""",
                    (notebook_id, len(questions)),
                ).fetchone()[0]

                for pos, (q_id, _) in enumerate(questions, start=1):
                    conn.execute(
                        "INSERT INTO drill_session_answers (session_id, question_id, position) "
                        "VALUES (%s, %s, %s)",
                        (sess_id, q_id, pos),
                    )

                first_q = questions[0][1]
                msg = f"Drill started ({len(questions)} questions).\n\n[1/{len(questions)}] {first_q}"
                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                conn.commit()
        except Exception as exc:
            logger.exception("drill_start: failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        return RevisionOutput(action="drill_started", message=msg)

    def _handle_drill_answer(self, user_answer: str, chat_id) -> RevisionOutput:
        if not user_answer:
            return RevisionOutput(action="error", message="Usage: /drill answer <your answer>")

        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                sess = conn.execute(
                    "SELECT id, questions_total FROM drill_sessions WHERE ended_at IS NULL LIMIT 1"
                ).fetchone()
                if sess is None:
                    return RevisionOutput(
                        action="error",
                        message="No open drill session. Start one with /drill start <subject> <N>.",
                    )
                sess_id, questions_total = sess

                current = conn.execute(
                    """SELECT dsa.id, dsa.position, rq.question
                       FROM drill_session_answers dsa
                       JOIN revision_questions rq ON rq.id = dsa.question_id
                       WHERE dsa.session_id = %s AND dsa.user_answer IS NULL
                       ORDER BY dsa.position ASC LIMIT 1""",
                    (sess_id,),
                ).fetchone()
                if current is None:
                    return RevisionOutput(
                        action="error",
                        message="All questions already answered. Send /drill submit to close.",
                    )

                dsa_id, position, _ = current
                conn.execute(
                    "UPDATE drill_session_answers SET user_answer = %s, answered_at = now() WHERE id = %s",
                    (user_answer, dsa_id),
                )

                next_q = conn.execute(
                    """SELECT rq.question, dsa.position
                       FROM drill_session_answers dsa
                       JOIN revision_questions rq ON rq.id = dsa.question_id
                       WHERE dsa.session_id = %s AND dsa.user_answer IS NULL
                       ORDER BY dsa.position ASC LIMIT 1""",
                    (sess_id,),
                ).fetchone()

                if next_q:
                    msg = f"[{next_q[1]}/{questions_total}] {next_q[0]}"
                else:
                    msg = "All questions answered. Send /drill submit to close the session."

                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                conn.commit()
        except Exception as exc:
            logger.exception("drill_answer: failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        return RevisionOutput(action="drill_answered", message=msg)

    def _handle_drill_submit(self, chat_id) -> RevisionOutput:
        url = _db_url()
        if not url:
            return RevisionOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                sess = conn.execute(
                    "SELECT id, questions_total FROM drill_sessions WHERE ended_at IS NULL LIMIT 1"
                ).fetchone()
                if sess is None:
                    return RevisionOutput(action="error", message="No open drill session.")
                sess_id, questions_total = sess

                answered_rows = conn.execute(
                    """SELECT dsa.position, rq.question, rq.expected_answer,
                              rq.id, rq.interval_days, dsa.user_answer
                       FROM drill_session_answers dsa
                       JOIN revision_questions rq ON rq.id = dsa.question_id
                       WHERE dsa.session_id = %s AND dsa.user_answer IS NOT NULL
                       ORDER BY dsa.position""",
                    (sess_id,),
                ).fetchall()

                questions_answered = len(answered_rows)

                # min_required lives in agent_watch_rules so "what counts as a real
                # drill" is defined in one place, not duplicated across two files.
                min_rule = conn.execute(
                    "SELECT condition FROM agent_watch_rules "
                    "WHERE rule_type = 'gate_missed' AND enabled = true LIMIT 1"
                ).fetchone()
                min_required = int((min_rule[0] or {}).get("min_required", 3)) if min_rule else 3

                # Set ended_at first so the generated elapsed_seconds column fires.
                conn.execute(
                    "UPDATE drill_sessions SET ended_at = now(), questions_answered = %s WHERE id = %s",
                    (questions_answered, sess_id),
                )
                elapsed = conn.execute(
                    "SELECT elapsed_seconds FROM drill_sessions WHERE id = %s", (sess_id,)
                ).fetchone()[0] or 0

                # ── short-circuit: zero LLM cost for below-minimum submissions ──
                if questions_answered < min_required:
                    reason = (f"rejected: only {questions_answered}/{questions_total} "
                              f"answered (min {min_required})")
                    conn.execute(
                        "UPDATE drill_sessions SET verified = false, reason = %s WHERE id = %s",
                        (reason, sess_id),
                    )
                    msg = (f"Session closed without clearing "
                           f"({questions_answered}/{questions_total} answered, min {min_required} required).")
                    if chat_id:
                        self._write_outbox(conn, chat_id, msg)
                    conn.commit()
                    return RevisionOutput(action="rejected", message=msg)

                # ── LLM only reached when questions_answered >= min_required ──
                pairs_text = "\n\n".join(
                    f"[{row[0]}] Q: {row[1]}\nExpected: {row[2]}\nStudent: {row[5]}"
                    for row in answered_rows
                )
                try:
                    prompt = _DRILL_GRADE_TEMPLATE.format(pairs=pairs_text)
                    raw = self._call_llm(prompt).strip().replace("```json", "").replace("```", "").strip()
                    grades = {g["position"]: g["score"] for g in json.loads(raw)}
                except Exception as exc:
                    logger.exception("drill_submit: batch grading failed")
                    return RevisionOutput(action="error", message=f"Grading failed: {exc}")

                for row in answered_rows:
                    pos, _, _, q_id, q_interval, _ = row
                    score = max(0, min(10, grades.get(pos, 5)))
                    interval_after = next_interval(current=q_interval, score=score)
                    conn.execute(
                        "UPDATE drill_session_answers SET score = %s "
                        "WHERE session_id = %s AND position = %s",
                        (score, sess_id, pos),
                    )
                    conn.execute(
                        "INSERT INTO revision_reviews (question_id, score, interval_days_after) "
                        "VALUES (%s, %s, %s)",
                        (q_id, score, interval_after),
                    )
                    conn.execute(
                        "UPDATE revision_questions "
                        "SET interval_days = %s, next_review_date = CURRENT_DATE + %s "
                        "WHERE id = %s",
                        (interval_after, interval_after, q_id),
                    )

                scores = [max(0, min(10, grades.get(row[0], 5))) for row in answered_rows]
                score_avg = round(statistics.mean(scores), 2)
                flag_fast = elapsed < 120

                if flag_fast:
                    reason = (f"cleared: {questions_answered}/{questions_total} answered, "
                              f"{elapsed}s elapsed — flagged as fast, score {score_avg}/10")
                else:
                    reason = (f"cleared: {questions_answered}/{questions_total} answered, "
                              f"{elapsed}s elapsed, score {score_avg}/10")

                conn.execute(
                    "UPDATE drill_sessions "
                    "SET verified = true, flag_fast = %s, score_avg = %s, reason = %s "
                    "WHERE id = %s",
                    (flag_fast, score_avg, reason, sess_id),
                )
                msg = f"Drill complete. {reason}"
                if chat_id:
                    self._write_outbox(conn, chat_id, msg)
                conn.commit()
        except Exception as exc:
            logger.exception("drill_submit: failed")
            return RevisionOutput(action="error", message=f"DB error: {exc}")

        return RevisionOutput(action="drill_submitted", message=msg)


_agent = RevisionAgent()


def revision_agent_node(state: GraphState) -> dict:
    result = _agent.handle(RevisionInput(
        raw_input=state["raw_input"],
        item_id=state.get("capture_uuid"),
    ))
    return {"specialist_result": result.model_dump()}
