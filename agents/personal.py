from langgraph.graph import END

from agents.base import GraphState

# Phase 2.3 will populate this from capture_shortcuts table + intent classification.
# For now: one stub entry to prove routing works.
DISPATCH_MAP: dict[str, str] = {
    "echo": "echo_agent",
}


def personal_agent_node(state: GraphState) -> dict:
    """
    Sole entry point. Two modes:
    - specialist_result is None  → determine routing, set routed_to
    - specialist_result is set   → merge complete, clear routed_to and stop
    """
    if state.get("specialist_result") is not None:
        return {"routed_to": None}

    # Phase 2.3: dispatch map lookup replaces this default
    routed_to = state.get("routed_to") or "echo"
    return {"routed_to": routed_to}


def route_from_personal(state: GraphState) -> str:
    if state.get("specialist_result") is not None:
        return END
    routed_to = state.get("routed_to", "")
    return DISPATCH_MAP.get(routed_to, END)
