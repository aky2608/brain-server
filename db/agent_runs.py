"""Context manager for writing one agent_runs row per component invocation."""
import os
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional


@dataclass
class RunContext:
    outcome: str = "no_action_needed"
    detail: Optional[str] = None


def _db_url() -> str:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise RuntimeError("BRAIN_DB_URL not set")
    return url


def _insert(component: str) -> int:
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        row = conn.execute(
            "INSERT INTO agent_runs (component) VALUES (%s) RETURNING id",
            (component,),
        ).fetchone()
        conn.commit()
        return row[0]


def _finish(run_id: int, outcome: str, detail: Optional[str], error: Optional[str]) -> None:
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            """UPDATE agent_runs
               SET finished_at = now(), outcome = %s, detail = %s, error = %s
               WHERE id = %s""",
            (outcome, detail, error, run_id),
        )
        conn.commit()


@contextmanager
def record_run(component: str):
    """Write one agent_runs row for this invocation.

    Yields a RunContext the caller can mutate:
        run.outcome = "acted"          # default: no_action_needed
        run.detail  = "3 items sent"   # optional human-readable note

    On unhandled exception: outcome='failed', error=full traceback, re-raises.
    """
    ctx = RunContext()
    run_id = _insert(component)
    try:
        yield ctx
    except Exception:
        _finish(run_id, "failed", ctx.detail, traceback.format_exc())
        raise
    else:
        _finish(run_id, ctx.outcome, ctx.detail, None)
