"""
ProjectAgent — Coding Agent, spec-packaging phase only.

No LLM call. Strips the /build prefix (matching notebook.py / revision.py convention),
parses alias + task text, validates the project is build-enabled, and writes a
pending_approvals row. Aider invocation comes in a later session.
"""
import logging
import os
import re
from datetime import datetime, timezone
from typing import ClassVar, Optional

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

logger = logging.getLogger("brain")


class ProjectInput(NarrowModel):
    item_id: str   # capture UUID from GraphState
    raw: str       # full raw_input including /build prefix
    source: str


class ProjectOutput(NarrowModel):
    item_id: str
    approval_id: Optional[str] = None
    accepted: bool
    reason: str


class ProjectAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.always
    cost_tier: ClassVar[CostTier] = CostTier.free
    requires_context: ClassVar[list[str]] = []

    InputSchema = ProjectInput
    OutputSchema = ProjectOutput

    def handle(self, input: ProjectInput) -> ProjectOutput:
        raw = input.raw.strip()
        after_slash = re.sub(r"^/build\s*", "", raw, flags=re.IGNORECASE).strip()
        parts = after_slash.split(None, 1)
        alias = parts[0].lower() if parts else ""
        task_text = parts[1].strip() if len(parts) > 1 else ""

        if not alias:
            _write_outbox(f"Build rejected: no alias provided\nraw: {raw[:200]}")
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason="no project alias provided",
            )
        if not task_text:
            _write_outbox(f"Build rejected: no task text for alias '{alias}'")
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason="no task text provided",
            )

        project = _lookup_project(alias)
        if project is None:
            _write_outbox(
                f"Build rejected: unknown project alias '{alias}'\n"
                f"task: {task_text[:200]}"
            )
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason=f"unknown project alias '{alias}'",
            )
        if project["status"] != "active":
            _write_outbox(
                f"Build rejected: project '{alias}' is {project['status']}\n"
                f"task: {task_text[:200]}"
            )
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason=f"project '{alias}' is {project['status']}",
            )
        if not project["build_enabled"]:
            _write_outbox(
                f"Build rejected: build not enabled for '{alias}'\n"
                f"task: {task_text[:200]}"
            )
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason=f"build not enabled for project '{alias}'",
            )

        engine = "aider" if re.search(r"(?:^|\s)--aider(?:\s|$)", task_text, re.IGNORECASE) else "claude"
        task_text = re.sub(r"\s*--aider\b", "", task_text, flags=re.IGNORECASE)
        task_text = re.sub(r"\s+", " ", task_text).strip()

        if not task_text:
            _write_outbox(f"Build rejected: no task text for alias '{alias}'")
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason="no task text provided",
            )

        spec = {
            "alias": alias,
            "task": task_text,
            "engine": engine,
            "repo_url": project["repo_url"],
            "local_path": project["local_path"],
            "default_branch": project["default_branch"],
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            approval_id = _write_approval(project["id"], spec, engine)
        except UniqueViolation:
            _write_outbox(
                f"Build rejected: a build for this task is already in flight\n"
                f"task: {task_text[:200]}"
            )
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason="a build for this task is already in flight",
            )
        if approval_id is None:
            _write_outbox(
                f"Build rejected: database error\n"
                f"task: {task_text[:200]}"
            )
            return ProjectOutput(
                item_id=input.item_id,
                accepted=False,
                reason="database error writing approval",
            )

        logger.info(
            "project_agent: approval written",
            extra={"ctx": {"item_id": input.item_id, "approval_id": approval_id,
                           "alias": alias}},
        )
        return ProjectOutput(
            item_id=input.item_id,
            approval_id=approval_id,
            accepted=True,
            reason="pending approval created",
        )


# ---------------------------------------------------------------------------
# DB helpers — each opens its own connection; commits immediately
# ---------------------------------------------------------------------------

def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


def _tg_chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def _write_outbox(message: str) -> None:
    chat_id = _tg_chat_id()
    url = _db_url()
    if not chat_id or not url:
        return
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                (chat_id, message),
            )
            conn.commit()
    except Exception:
        logger.exception("project_agent: outbox write failed")


def _lookup_project(alias: str) -> Optional[dict]:
    url = _db_url()
    if not url:
        return None
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                """SELECT id, alias, status, build_enabled,
                          repo_url, local_path, default_branch
                   FROM projects WHERE alias = %s""",
                (alias,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "alias": row[1],
            "status": row[2],
            "build_enabled": row[3],
            "repo_url": row[4],
            "local_path": row[5],
            "default_branch": row[6],
        }
    except Exception:
        logger.error("project_agent: project lookup failed", exc_info=True)
        return None


def _write_approval(project_id: str, spec: dict, engine: str) -> Optional[str]:
    url = _db_url()
    if not url:
        return None
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                """INSERT INTO pending_approvals
                       (project_id, spec, status, engine, expires_at)
                   VALUES (%s, %s, 'pending', %s, now() + interval '24 hours')
                   RETURNING id""",
                (project_id, Jsonb(spec), engine),
            ).fetchone()
            conn.commit()
            return str(row[0]) if row else None
    except UniqueViolation:
        raise
    except Exception:
        logger.error("project_agent: approval insert failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

_agent = ProjectAgent()


def project_agent_node(state: GraphState) -> dict:
    capture_uuid = state.get("capture_uuid")
    if not capture_uuid:
        logger.error("project_agent_node: no capture_uuid in state — skipping")
        return {"specialist_result": {"item_id": None, "accepted": False,
                                      "reason": "no capture_uuid"}}

    result = _agent.handle(
        ProjectInput(
            item_id=capture_uuid,
            raw=state["raw_input"],
            source=state.get("source", ""),
        )
    )
    return {"specialist_result": result.model_dump()}
