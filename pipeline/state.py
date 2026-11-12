# state.py
# GraphState: the state shared by all agents
#
# Keys worth noting:
#   facility_level     Level of care of the new facility as judged by the Writer in section 3-2
#                      (general hospital / tertiary general hospital). Period uses it as the
#                      institution type for bed utilization.
#   catchment_parse    How the catchment block of section 3-2 was parsed (rule/llm) and what
#                      was normalized
#   critique_history   Critic verdicts of every attempt

from typing import TypedDict


class GraphState(TypedDict, total=False):
    # Input
    section_id:   str
    project_id:   str

    # Retrieval
    guideline_chunks: list   # [{"chunk_id", "text", "heading", "version", "distance"}]

    # Memory
    section_facts:    dict
    confirmed_facts:  dict

    # Summaries of the subsections generated earlier in this run, passed in by the driver
    prior_sections:   dict

    # Computation
    calc_results:     dict

    # Writer
    draft_text:       str

    # Catchment (filled in section 3-2 and passed to later subsections)
    catchment_result: dict
    catchment_parse:  dict
    facility_level:   dict  # {"level": "종합병원급|상급종합병원급", "source": ..., "note": ...}

    # Period (decided on entering section 3-3 and passed to sections 3-3, 3-4, and 3-5)
    base_period: dict

    # Critic
    critique:         dict
    critique_history: list
    retry_count:      int

    # Final output
    final_text:       str
