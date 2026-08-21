from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel


class EchoInput(NarrowModel):
    text: str


class EchoOutput(NarrowModel):
    echoed: str


class EchoAgent(BaseAgent):
    interrupt_tier = InterruptTier.log_only
    cost_tier = CostTier.free
    requires_context: list[str] = []

    InputSchema = EchoInput
    OutputSchema = EchoOutput

    def handle(self, input: EchoInput) -> EchoOutput:
        return EchoOutput(echoed=input.text)


_agent = EchoAgent()


def echo_agent_node(state: GraphState) -> dict:
    """Narrow slice in, narrow slice out. Pulls only text from state; writes only to specialist_result."""
    result = _agent.handle(EchoInput(text=state["raw_input"]))
    return {"specialist_result": result.model_dump()}
