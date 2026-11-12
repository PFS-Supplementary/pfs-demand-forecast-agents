# agents/orchestrator.py
# Orchestrator: manages the order of agent execution with a LangGraph StateGraph
#
# Order: ... -> writer -> catchment -> critic -> (back to writer on retry)
# catchment_resolver_node acts only when section_id is "3-2" (catchment setting) and passes
# the state through unchanged for other subsections, so one graph handles every subsection.
# In section 3-2 it extracts region codes from the Writer's catchment statement and records
# them in state["catchment_result"].
# The retry loop (critic -> writer) also goes through writer -> catchment -> critic again, so
# catchment_result always reflects the final (or last retried) draft.

from langgraph.graph import StateGraph, END
from state import GraphState
from agents.retrieval          import retrieval_agent
from agents.memory             import memory_agent
from agents.computation        import computation_agent
from agents.writer             import writer_agent
from agents.sub.period_decider import period_decider_node
from agents.sub.catchment_resolver import catchment_resolver_node
from agents.critic             import critic_agent


def _route_after_critic(state: GraphState) -> str:
    """Decide the next node from the Critic result.

        Branching is based on draft_text:
          - when the Critic asks for a retry: draft_text is reset to "" and retry_count is increased
          - when the Critic stops (retries used up, or PASS): final_text is set and draft_text is kept

        draft_text == "" -> run the Writer again
        draft_text != "" -> END (final_text is set whether the result is PASS or the last FAIL)
    """
    if state["draft_text"] == "":
        return "writer"
    return "end"


def build_graph():
    g = StateGraph(GraphState)

    g.add_node("retrieval",   retrieval_agent)
    g.add_node("memory",      memory_agent)
    g.add_node("period",      period_decider_node)
    g.add_node("computation", computation_agent)
    g.add_node("writer",      writer_agent)
    g.add_node("catchment",   catchment_resolver_node)
    g.add_node("critic",      critic_agent)

    g.set_entry_point("retrieval")
    g.add_edge("retrieval",   "memory")
    g.add_edge("memory",      "period")
    g.add_edge("period",      "computation")
    g.add_edge("computation", "writer")
    g.add_edge("writer",      "catchment")
    g.add_edge("catchment",   "critic")

    g.add_conditional_edges(
        "critic",
        _route_after_critic,
        {"writer": "writer", "end": END},
    )

    return g.compile()


graph = build_graph()