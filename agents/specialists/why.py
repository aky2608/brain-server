import logging
import os
from typing import Optional

import psycopg
from dotenv import load_dotenv

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

load_dotenv()

logger = logging.getLogger("brain")


class WhyInput(NarrowModel):
    query: str


class WhyOutput(NarrowModel):
    answer: str


class WhyAgent(BaseAgent):
    """
    Reads from agent_decisions and returns stored reasoning. Zero LLM cost.
    Triggered by 'why ...' or 'explain ...' prefix in personal_agent_node.
    """

    interrupt_tier = InterruptTier.log_only
    cost_tier = CostTier.free
    requires_context: list[str] = []

    InputSchema = WhyInput
    OutputSchema = WhyOutput

    def handle(self, input: WhyInput) -> WhyOutput:
        rows = _fetch_recent_decisions(limit=5)
        if not rows:
            return WhyOutput(answer="No decisions logged yet.")
        lines = [f"[{ts}] {agent} — {reason}" for agent, reason, ts in rows]
        return WhyOutput(answer="\n".join(lines))


def _fetch_recent_decisions(limit: int = 5) -> list[tuple]:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        return []
    try:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                """SELECT agent_name, reason, created_at
                     FROM agent_decisions
                    ORDER BY created_at DESC
                    LIMIT %s""",
                (limit,),
            ).fetchall()
        return rows
    except Exception:
        logger.exception("agent_decisions fetch failed")
        return []


_agent = WhyAgent()


def why_agent_node(state: GraphState) -> dict:
    """Narrow slice in, narrow slice out. Reads agent_decisions; writes only specialist_result."""
    result = _agent.handle(WhyInput(query=state["raw_input"]))
    return {"specialist_result": result.model_dump()}
