"""
NotebookAgent — zero LLM, deterministic parse + DB write.

Two behaviors dispatched from raw_input:

  /gate <subject_hint> <content>
      Resolves subject_hint via SUBJECT_ALIASES → notebook_id.
      UPDATEs items SET notebook_id=X, subcategory='gate' WHERE id=item_id.
      Unknown hint → error reply, item left unrouted (not lost).

  /gate new "<name>" [type]
      Creates a new notebook. type defaults to 'gate_subject'.
      Quoted name required; unarchives if the name+type pair already exists.
"""
import logging
import os
import re
from typing import ClassVar, Optional

import psycopg
from dotenv import load_dotenv

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

load_dotenv()
logger = logging.getLogger("brain")

# Canonical notebook names must match what was seeded in 007_notebooks.
# Keys are lowercased user tokens; values are exact notebook names in DB.
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

VALID_TYPES = {"gate_subject", "general", "project"}


class NotebookInput(NarrowModel):
    raw_input: str
    item_id: Optional[str] = None


class NotebookOutput(NarrowModel):
    action: str          # "routed" | "created" | "error"
    notebook_id: Optional[int] = None
    notebook_name: Optional[str] = None
    message: str
    decisions: list[dict] = []


def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


def _resolve_subject(hint: str) -> Optional[str]:
    """Map user token → canonical notebook name, or None if unrecognised."""
    return SUBJECT_ALIASES.get(hint.lower())


def _notebook_id_by_name(conn: psycopg.Connection, name: str, notebook_type: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM notebooks WHERE name = %s AND notebook_type = %s",
        (name, notebook_type),
    ).fetchone()
    return row[0] if row else None


class NotebookAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.free
    requires_context: ClassVar[list[str]] = []

    InputSchema = NotebookInput
    OutputSchema = NotebookOutput

    def handle(self, input: NotebookInput) -> NotebookOutput:
        # Strip leading /gate prefix — everything after is the payload.
        raw = input.raw_input.strip()
        after_slash = re.sub(r"^/gate\s*", "", raw, flags=re.IGNORECASE).strip()
        tokens = after_slash.split()

        if not tokens:
            return NotebookOutput(
                action="error",
                message="Usage: /gate <subject> <content>  or  /gate new \"<name>\" [type]",
            )

        if tokens[0].lower() == "new":
            return self._handle_new(after_slash[len("new"):].strip())

        return self._handle_route(tokens, input.item_id)

    # ------------------------------------------------------------------

    def _handle_route(self, tokens: list[str], item_id: Optional[str]) -> NotebookOutput:
        subject_hint = tokens[0]
        notebook_name = _resolve_subject(subject_hint)

        if not notebook_name:
            valid = sorted(set(SUBJECT_ALIASES.keys()))
            return NotebookOutput(
                action="error",
                message=(
                    f"Unknown subject {subject_hint!r}. "
                    f"Valid hints: {', '.join(valid)}"
                ),
            )

        url = _db_url()
        if not url:
            return NotebookOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                notebook_id = _notebook_id_by_name(conn, notebook_name, "gate_subject")
                if notebook_id is None:
                    return NotebookOutput(
                        action="error",
                        message=f"Notebook {notebook_name!r} not found in DB — was it archived?",
                    )

                if item_id:
                    conn.execute(
                        """UPDATE items
                              SET notebook_id = %s,
                                  subcategory = 'gate'
                            WHERE id = %s""",
                        (notebook_id, item_id),
                    )
                    conn.commit()

        except Exception as exc:
            logger.exception("notebook route failed item_id=%s", item_id)
            return NotebookOutput(action="error", message=f"DB error: {exc}")

        content_preview = " ".join(tokens[1:])[:60] or "(no content)"
        return NotebookOutput(
            action="routed",
            notebook_id=notebook_id,
            notebook_name=notebook_name,
            message=f"Saved to [{notebook_name}]: {content_preview}",
            decisions=[{
                "action_taken": f"route_to_notebook:{notebook_id}",
                "reason": f"/gate {tokens[0]} → {notebook_name!r}",
                "item_id": item_id,
            }],
        )

    def _handle_new(self, rest: str) -> NotebookOutput:
        # Expect: "<name>" [type]
        m = re.match(r'^"([^"]+)"\s*(\w+)?', rest)
        if not m:
            return NotebookOutput(
                action="error",
                message='Usage: /gate new "<name>" [gate_subject|general|project]',
            )

        name = m.group(1).strip()
        raw_type = (m.group(2) or "gate_subject").lower()

        if raw_type not in VALID_TYPES:
            return NotebookOutput(
                action="error",
                message=f"Invalid type {raw_type!r}. Must be one of: {', '.join(sorted(VALID_TYPES))}",
            )

        url = _db_url()
        if not url:
            return NotebookOutput(action="error", message="BRAIN_DB_URL not set")

        try:
            with psycopg.connect(url) as conn:
                # Upsert: if the name+type pair was archived, reactivate it.
                row = conn.execute(
                    """INSERT INTO notebooks (name, notebook_type)
                       VALUES (%s, %s)
                       ON CONFLICT ON CONSTRAINT uq_notebooks_name_type
                       DO UPDATE SET archived_at = NULL
                       RETURNING id, name, notebook_type""",
                    (name, raw_type),
                ).fetchone()
                conn.commit()
        except Exception as exc:
            logger.exception("notebook create failed name=%r", name)
            return NotebookOutput(action="error", message=f"DB error: {exc}")

        return NotebookOutput(
            action="created",
            notebook_id=row[0],
            notebook_name=row[1],
            message=f"Created notebook [{row[1]}] ({row[2]})",
            decisions=[{
                "action_taken": f"create_notebook:{row[0]}",
                "reason": f"/gate new → {row[1]!r} type={row[2]}",
                "item_id": None,
            }],
        )


_agent = NotebookAgent()


def notebook_agent_node(state: GraphState) -> dict:
    result = _agent.handle(NotebookInput(
        raw_input=state["raw_input"],
        item_id=state.get("capture_uuid"),
    ))
    return {"specialist_result": result.model_dump()}
