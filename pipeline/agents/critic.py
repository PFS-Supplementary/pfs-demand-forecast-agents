# agents/critic.py
# Critic agent: checks the generated text
#   Check 1 (rule-based): interpretation expressions — detects the listed patterns
#   Check 2 (LLM):        guideline compliance — compares the text with the full guideline chunks
# The verdict of every attempt is recorded in critique_history. When a subsection is
# regenerated, the verdict is passed to the Writer (see writer.py).
# The helpers for a numeric check (whether figures in the text appear in the allowed sources)
# are kept below but are not part of the pass/fail decision.
#

import re

from state import GraphState
from agents.sub.llm import call_json

# Interpretation patterns
# '의미한다' is excluded because it is used normally in definitions ("진료권이란 X를 의미한다").
_INTERP_PATTERNS = [
    r"시사한다", r"나타낸다",
    r"필요가 있다", r"우려가 있다", r"기대된다",
    r"판단된다", r"보인다", r"것으로 보임",
]
_INTERP_RE = re.compile("|".join(_INTERP_PATTERNS))

# Pattern for extracting figures
_DATE_RE   = re.compile(r"\d{4}\.\d{1,2}(?:\.\d{1,2})?")
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")

# General-purpose constants used in formulas — no source check needed
# e.g. 365 (days per year) in the formula for the annual number of inpatients
#
# Also allowed:
#   - "0.129", "0.13": regression coefficient of Model 3 in Table III-14 of the KDI guideline
#     (a 1% increase in emergency beds lowers the emergency death rate by 0.129%)
#   - "100000": unit conversion for the emergency death rate (per 100,000 population)
#   - "10000":  unit used in the guideline text (per 10,000 population)
_FORMULA_CONSTANTS = {
    "365", "100", "52", "12",
    "0.129", "0.13", "100000", "10000",
}


def _normalize_num(n: str) -> str:
    """Normalize by removing commas: '3,018' -> '3018'"""
    return n.replace(",", "")


'''
def _check_numbers(draft: str, source: str,
                   extra_allowed: set = None) -> dict:
    """draft의 수치가 허용 소스에 있는지 검사.

    extra_allowed: calc_results["numbers"]에서 직접 추출한 허용 수치 집합.
      _DATE_RE가 대형 소수를 잘못 제거하는 버그를 우회하기 위해 별도 전달.
    """
    if not source.strip():
        return {"pass": True, "missing": []}

    draft_clean  = _DATE_RE.sub("", draft)
    source_clean = _DATE_RE.sub("", source)

    draft_nums  = {_normalize_num(n) for n in _NUMBER_RE.findall(draft_clean)
                   if len(n.replace(",", "")) >= 3}
    source_nums = {_normalize_num(n) for n in _NUMBER_RE.findall(source_clean)
                   if len(n.replace(",", "")) >= 3}

    missing = draft_nums - source_nums - _FORMULA_CONSTANTS
    if extra_allowed:
        missing -= extra_allowed

    # 소수점 중간값 처리: 반올림한 값이 허용 소스에 있으면 통과
    # 예: LLM이 206912/311.856=663.31 자체 계산 시 → round → 663 허용
    all_allowed = source_nums | _FORMULA_CONSTANTS | (extra_allowed or set())
    truly_missing = set()
    for m in missing:
        try:
            rounded = str(round(float(m)))
            if rounded not in all_allowed:
                truly_missing.add(m)
        except ValueError:
            truly_missing.add(m)

    return {
        "pass":    len(truly_missing) == 0,
        "missing": list(truly_missing),
    }
'''

def _check_interpretation(draft: str) -> dict:
    hits = _INTERP_RE.findall(draft)
    return {
        "pass":       len(hits) == 0,
        "violations": hits,
    }


def _score_validate(obj):
    sc = obj.get("score")
    if not isinstance(sc, int) or not (1 <= sc <= 4):
        return "score 가 1~4 정수가 아님"
    return None


def _check_guideline(draft: str, chunks: list) -> dict:
    """Score guideline compliance with the LLM, showing it the full text of every chunk.
        A response format error triggers another call; if the score still cannot be obtained,
        the text is passed and the verdict is marked with judge_error.
    """
    if not chunks:
        return {"pass": True, "score": 4, "reason": "지침 청크 없음 - 스킵"}

    guideline_text = "\n\n".join(f"[{c['chunk_id']}] {c['text']}" for c in chunks)

    prompt = f"""
다음은 KDI 세부지침과 생성된 보고서 소절입니다.
지침에서 요구하는 서술 항목이 생성 텍스트에 적절히 포함되어 있는지 평가하십시오.

## KDI 세부지침
{guideline_text}

## 생성 텍스트
{draft}

## 평가 기준
- 4점: 지침의 주요 항목을 모두 포함
- 3점: 대부분 포함, 일부 누락
- 2점: 절반 이하 포함
- 1점: 지침과 무관한 내용

JSON 형식으로만 답하시오. 다른 텍스트 없이:
{{"score": <1~4 정수>, "reason": "<한 문장 이유>"}}
""".strip()

    obj, log = call_json(prompt, validate=_score_validate)
    if obj is None:
        return {"pass": True, "score": None, "reason": "판정 실패(재호출 소진) - 통과 처리",
                "judge_error": True, "attempts": log["attempts"],
                "errors": log["errors"]}
    return {"pass": obj["score"] >= 3, "score": obj["score"],
            "reason": str(obj.get("reason", "")), "attempts": log["attempts"]}


def _flatten_numbers(d: dict, prefix: str = "") -> list[str]:
    """Flatten the numbers dict into a list of 'key: value' lines (nested dicts included)."""
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.extend(_flatten_numbers(v, prefix=f"{k}."))
        else:
            lines.append(f"{prefix}{k}: {v}")
    return lines


def _get_allowed_computation_nums(calc_results: dict) -> set:
    """Return the figures in calc_results["numbers"] as a set of normalized strings.

        Built directly from the numbers dict, outside the regex pipeline, so that large decimals
        (e.g. 46132.9) are not dropped by _DATE_RE.
    """
    allowed = set()
    if not calc_results or "numbers" not in calc_results:
        return allowed

    def _extract(d):
        for v in d.values():
            if isinstance(v, dict):
                _extract(v)
            elif isinstance(v, (int, float)):
                # Add both decimals and integers after normalization (commas removed)
                s = _normalize_num(str(v))
                if len(s.replace(".", "")) >= 3:
                    allowed.add(s)

    _extract(calc_results["numbers"])
    return allowed


def _build_source(state: GraphState) -> str:
    """Assemble the sources allowed in the numeric check.

        Uses tables, numbers, and project_context; source_text is excluded because the Writer
        generates without it. Figures in project_context (the project overview) can be used by
        the Writer, so they are included.
    """
    parts = []
    cr = state.get("calc_results", {})
    if "tables" in cr and isinstance(cr["tables"], str):
        parts.append(cr["tables"])
    if "numbers" in cr and isinstance(cr["numbers"], dict):
        parts.append("\n".join(_flatten_numbers(cr["numbers"])))
    # project_context: figures in the project overview (total beds, floor area, etc.) that the Writer may use
    project_context = state.get("section_facts", {}).get("project_context", "")
    if project_context:
        parts.append(project_context)
    return "\n".join(p for p in parts if p)


def critic_agent(state: GraphState) -> GraphState:
    draft       = state["draft_text"]
    source      = _build_source(state)
    chunks      = state["guideline_chunks"]

    extra_allowed = _get_allowed_computation_nums(state.get("calc_results", {}))
    interp_check = _check_interpretation(draft)
    guide_check  = _check_guideline(draft, chunks)
    overall_pass = (
        interp_check["pass"] and
        guide_check["pass"]
    )

    critique = {
        "attempt":              state.get("retry_count", 0) + 1,  # 1 = first generation
        "pass":                 overall_pass,
        "interpretation_check": interp_check,
        "guideline_check":      guide_check,
    }
    # Keep the verdict of every attempt.
    history = list(state.get("critique_history") or []) + [critique]

    status = "PASS" if overall_pass else "FAIL"
    print(f"[Critic] {state['section_id']} → {status}")
    interp_msg = "OK" if interp_check["pass"] else f"FAIL {interp_check['violations']}"
    print(f"  해석 금지:   {interp_msg}")
    print(f"  지침 준수:   {guide_check['score']}점 - {guide_check['reason']}")

    new_state = {**state, "critique": critique, "critique_history": history}
    if not overall_pass and state["retry_count"] < 2:
        new_state["draft_text"]   = ""
        new_state["retry_count"]  = state["retry_count"] + 1
        print(f"  → 재생성 ({state['retry_count'] + 1}/2회)")
    else:
        new_state["final_text"] = draft

    return new_state