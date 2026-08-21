"""
Brain agent graph.

Connection: BRAIN_DB_URL from .env — same Supavisor-qualified URL used by Alembic
(postgres.your-tenant-id @ localhost:5432). psycopg v3 accepts the same postgresql:// scheme.

checkpointer.setup() is called inside build_graph(). It is idempotent and creates
LangGraph's own checkpoint tables (checkpoints, checkpoint_blobs, checkpoint_writes).
This is NOT an Alembic migration — LangGraph manages that schema itself.
"""
import os

from dotenv import load_dotenv
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from agents.base import GraphState
from agents.personal import personal_agent_node, route_from_personal
from agents.specialists.capture import capture_agent_node
from agents.specialists.echo import EchoInput, echo_agent_node
from agents.specialists.scheduling import scheduling_agent_node
from agents.specialists.why import why_agent_node

load_dotenv()
DB_URL = os.environ["BRAIN_DB_URL"]


def build_graph(checkpointer: PostgresSaver):
    builder = StateGraph(GraphState)

    builder.add_node("personal_agent", personal_agent_node)
    builder.add_node("capture_agent", capture_agent_node)
    builder.add_node("echo_agent", echo_agent_node)
    builder.add_node("scheduling_agent", scheduling_agent_node)
    builder.add_node("why_agent", why_agent_node)

    builder.set_entry_point("personal_agent")
    builder.add_conditional_edges("personal_agent", route_from_personal)
    builder.add_edge("capture_agent", "personal_agent")
    builder.add_edge("echo_agent", "personal_agent")
    builder.add_edge("scheduling_agent", "personal_agent")
    builder.add_edge("why_agent", "personal_agent")

    return builder.compile(checkpointer=checkpointer)


def run_proof() -> None:
    with PostgresSaver.from_conn_string(DB_URL) as checkpointer:
        checkpointer.setup()
        graph = build_graph(checkpointer)

        config = {"configurable": {"thread_id": "proof-001"}}
        result = graph.invoke(
            {
                "raw_input": "hello brain",
                "source": "test",
                "capture_uuid": None,
                "routed_to": None,
                "specialist_result": None,
            },
            config=config,
        )
        assert result["specialist_result"] == {"echoed": "hello brain"}, result
        print("PASS  happy path →", result["specialist_result"])

        # Extra key must be rejected by Pydantic before the graph is touched.
        try:
            EchoInput(text="hi", unexpected_key="boom")
            print("FAIL  ValidationError not raised")
        except ValidationError as exc:
            err = exc.errors()[0]
            assert err["type"] == "extra_forbidden", err
            print("PASS  extra key rejected →", err["type"])


if __name__ == "__main__":
    run_proof()
