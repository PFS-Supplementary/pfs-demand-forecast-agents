# agents/writer.py
# Writer agent: calls the language model with the assembled context and generates a report subsection.
#
# Division of work: calculation is done by Computation (deterministic); the Writer only
# explains and narrates. The analytical procedure is taken from the guideline chunks, not
# from task instructions.
# ─────────────────────────────────────────────────────────────────────────────
#
# What the instructions ask of the Writer:
#   - Evidence markers: every sentence ends with its evidence in 「」 — the id of a retrieved
#     guideline chunk (「202212-III-3-1」), the label of a data source, 「계산: key」 for a
#     computed value, or 「입력: key」 for a retrieved input. Sentences that state whether a
#     method given in the guideline is applied cite the chunk that defines the method.
#   - Subheadings are written as '[소제목] …' lines so that the evaluation does not have to
#     guess which lines are body sentences. No line starts with '#', and section numbers are
#     not repeated as headings.
#   - Values that use bed utilization are reported in both variants (catchment average and
#     national); the guideline lists both bases without ranking them.
#   - Items that could not be computed are shown to the Writer, which states that the value
#     was not produced and why. The Writer does not do arithmetic of its own.
#   - Section 3-2 ends with a [최종 진료권] block and a [신규 시설 진료 수준] line (general
#     hospital / tertiary general hospital). Period uses the latter as the institution type
#     for bed utilization.
#   - Subsections after 3-2 receive the fixed catchment as place names.
#   - On regeneration, the previous Critic verdict (score, reason, detected expressions) is
#     passed; the previous draft itself is not.
# The examples in the instructions use neutral placeholder values, not values from the cases.
#
#
#
# Citation convention (shared with the evaluator alce_eval.py):
#   - The marker comes before the period of the sentence.  e.g. "...로 추정됨 「202212-III-3-1」."
#   - Several chunks for one sentence are separated by semicolons: 「202212-III-3-1; 202212-III-3-2」
#   - Ids outside the set of retrieved chunks must never be produced.
#

from state import GraphState
from agents.sub.llm import generate_text

_STYLE = """
당신은 KDI 예비타당성조사 보고서를 작성하는 전문 연구원입니다.
다음 규칙을 반드시 준수하십시오.
1. 제공된 수치와 사실만 서술한다. 없는 수치를 만들지 않는다.
2. 수치에서 시사점·해석·인과관계를 추론하지 않는다.
3. 문장은 명사형 종결(~임, ~됨, ~나타남)을 원칙으로 한다.
4. 표는 출처와 표 번호를 병기해 참조만 한다(예: 'KDI 세부지침 <표 Ⅲ-1>'). 표 내용을 본문에 중복 서술하지 않는다. 자료에 없는 표 번호를 임의로 만들지 않으며, 표 번호가 불분명하면 표가 규정하는 내용만 서술한다.
5. 출처가 불분명한 수치는 서술하지 않는다.
6. 마크다운 문법(**볼드**, # 헤더 등)을 사용하지 않는다. 특히 어떤 줄도 '#', '##', '###' 으로 시작하지 않는다.
7. 제목·표제 성격의 줄(소절 표제, 항목 소제목 등)은 위치를 불문하고 예외 없이 '[소제목] '으로 시작한다. 소절 번호('[3-4-1]' 등)나 장·절 번호('제Ⅲ장', '제2절' 등)를 표제로 쓰지 않는다. 소제목 줄에는 인용 마커를 달지 않는다. 본문 문장에는 '[소제목]'을 붙이지 않는다. 다만 별도 지시로 형식이 지정된 결과 블록('[최종 진료권]' 등)은 소제목이 아니므로 지시된 형식 그대로 쓴다.
""".strip()


def _filter_project_context(raw: str) -> str:
    """Pass the project context through unchanged."""
    return raw


def _build_context(state: GraphState) -> tuple[str, str]:
    section_id = state["section_id"]

    cr            = state.get("calc_results", {})
    numbers_dict  = cr.get("numbers", {})
    tables_str    = cr.get("tables", "")
    # Retrieved inputs are shown separately from formula results. They are facts from the
    # database or the project plan, not calculation results, so they are attributed to their
    # source rather than marked 「계산」.
    inputs_dict   = cr.get("inputs", {})
    input_sources = cr.get("input_sources", {})
    unresolved    = cr.get("unresolved", [])

    # ── Guideline chunk block ─────────────────────────────────────────────────
    # The chunk_id is shown with the heading, since the model needs the id for its markers.
    guideline_block = "\n\n".join(
        f"[지침 청크 id={c['chunk_id']} | {c['heading']}]\n{c['text']}"
        for c in state["guideline_chunks"]
    )
    cited_ids = [c["chunk_id"] for c in state["guideline_chunks"]]

    # ── Computed values / table block (from Computation — deterministic) ──────
    calc_block = ""
    if tables_str:
        calc_block += tables_str
    if numbers_dict:
        calc_block += "\n" + "\n".join(f"{k}: {v}" for k, v in numbers_dict.items())

    raw_context     = state["section_facts"].get("project_context", "")
    project_context = _filter_project_context(raw_context)
    catchment_data  = state["section_facts"].get("catchment_data", "")
    confirmed_catch = state["section_facts"].get("confirmed_catchment", "")

    confirmed_block = ""
    if state["confirmed_facts"]:
        confirmed_block = "\n".join(
            f"[{sid}] {val['title']}: {val['summary']}"
            for sid, val in state["confirmed_facts"].items()
        )

    # ── Instruction block ─────────────────────────────────────────────────────
    instructions = []
    # Ruling text filled in by memory.py only when the special-purpose gate of section 3-2
    # ruled that the clause does not apply. The ruling is shown to the Writer as a fact to take
    # into account when determining the type; no conclusion about the catchment is injected.
    sp_ruling = state["section_facts"].get("special_purpose_ruling", "")
    if sp_ruling:
        instructions.append("※ " + sp_ruling)
    if guideline_block:
        instructions.append(
            "※ 제공된 KDI 세부지침 청크가 요구하는 서술 항목과 구조를 따라 "
            "작성하시오. 지침에 제시된 공식·정의·판단 기준이 있으면 그 근거를 "
            "본문에 명시하되, 지침에 없는 분석 절차나 결론을 임의로 만들지 마시오."
        )
        instructions.append(
            "※ 표나 자료를 인용할 때는 그 자료에 명시된 출처를 함께 표기하시오. "
            "지침 청크 본문에 등장하는 표 참조(<표 Ⅲ-N>)는 KDI 세부지침의 표이므로 "
            "'KDI 세부지침 <표 Ⅲ-N>'으로 표기하시오. 제공 자료에 없는 표 번호나 "
            "'<표 X>' 같은 비어 있는 표 참조를 만들지 마시오. 출처가 명시되지 않은 "
            "수치·표는 인용하지 마시오."
        )
        # ── Evidence marker convention ────────────────────────────────────────
        instructions.append(
            "※ [인용 표기 규칙] 본문의 각 문장 끝(마침표 바로 앞)에, 그 문장의 "
            "근거가 된 지침 청크의 id 를 「」 안에 표기하시오.\n"
            f"   - 사용 가능한 청크 id: {', '.join(cited_ids) if cited_ids else '(없음)'}\n"
            "   - 예: '연간 입원환자 수는 인구·입원의료이용률·지역친화도의 곱으로 "
            "추정됨 「202212-III-3-1」.'\n"
            "   - 한 문장이 여러 청크에 근거하면 세미콜론으로 나열: "
            "「202212-III-3-1; 202212-III-3-2」.\n"
            "   - 위 목록에 없는 id 를 절대 만들지 마시오. 근거 청크가 분명치 않은 "
            "문장에는 마커를 달지 마시오(임의로 끼워 맞추지 말 것)."
        )
        # ── Markers for data and project-overview sources ─────────────────────
        # Population, distance, self-sufficiency rate, bed plans, etc. are facts from data, not from
        # the guideline. The [출처: …] label attached to each data block is copied as the marker, so
        # that the evaluator classifies the sentence as citing a data source. Sentences that mix
        # guideline and data list both, separated by ';'.
        instructions.append(
            "※ [인용 표기 규칙·자료] '사업 개요'나 '진료권 분석 자료'에서 가져온 "
            "사실(인구·성비·고령비율·거리·이동시간·자체충족률·인접시설·병상계획 등)은 "
            "지침 청크 id 대신, 그 자료에 붙어 있는 [출처: …] 라벨의 출처명을 「」 안에 "
            "그대로 옮겨 적으시오.\n"
            "   - 예: '○○시 총인구는 000,000명으로 나타남 "
            "「행정안전부 주민등록 인구통계 + NHIS 건강보험통계연보」.'\n"
            "   - 한 문장이 지침과 자료를 함께 근거로 하면 둘을 세미콜론으로 병기: "
            "「202212-III-2-2다; 사업 기본계획서」.\n"
            "   - 자료에 [출처:] 라벨이 없으면 그 사실에는 마커를 달지 마시오."
        )
    if numbers_dict:
        instructions.append(
            "※ '계산 수치' 항목의 값은 Computation Agent 가 산출한 결정적 계산 "
            "결과임. 이 값들만 수치로 사용하고, 사업 개요·이전 소절에서 추가 "
            "수치를 임의로 가져오거나 직접 계산하지 마시오. 계산 수치 항목의 "
            "변수명(key)은 본문 서술에 표기하지 마시오. 단, 아래 인용 표기 "
            "규칙에 따라 「」 안에는 반드시 표기하시오.\n"
            "   - 지침이 요구하는 값인데 '계산 수치' 항목에 없으면, 그 값을 직접 "
            "산출하거나 근사하지 말고 '산출되지 않았음'을 명시하시오. 제공된 값들을 "
            "더하거나 곱하거나 빼서 새 수치를 만들지 마시오."
        )
        # ── Sentences with computed values carry a 「계산」 marker ─────────────
        instructions.append(
            "※ [인용 표기 규칙·계산] '계산 수치'에서 가져온 값을 담은 문장은 "
            "지침 청크 id 대신 「계산: <변수명>」으로 표기하시오. <변수명>은 그 값이 "
            "'계산 수치' 항목에 적혀 있던 key 를 그대로 옮긴 것이며, 임의로 만들거나 "
            "바꾸지 마시오.\n"
            "   - 예: '진료권 내 인구는 000,000명으로 나타남 「계산: population」.'\n"
            "   - 한 문장에 계산 수치가 둘 이상이면 세미콜론으로 나열: "
            "「계산: population; 계산: bed_utilization」.\n"
            f"   - 사용 가능한 변수명: {', '.join(numbers_dict.keys())}"
        )
    # Instruction to report both variants of bed utilization
    if any(k.startswith("bed_utilization_") for k in
           list(numbers_dict) + list(inputs_dict)):
        instructions.append(
            "※ [병상이용률 두 갈래] 병상이용률과 그것으로 산출된 값들이 "
            "'_catchment'(해당 진료권의 평균값)와 '_national'(대상 의료시설 종별에 "
            "따른 전국값) 두 벌로 제공됨. 지침은 병상이용률의 근거로 해당 진료권의 "
            "평균값·종별 병상이용률·유사 의료시설 병상이용률을 제시하되 어느 것을 "
            "우선할지는 정하지 않으므로, 둘 중 하나를 고르지 말고 두 결과를 모두 "
            "서술하시오.\n"
            "   - 각 값이 어느 기준으로 산정된 것인지 한국어로 밝히시오"
            "(예: '해당 진료권의 평균 병상이용률을 적용한 경우', '대상 의료시설 "
            "종별에 따른 전국 병상이용률을 적용한 경우').\n"
            "   - 두 결과의 차이를 근거 없이 해석하거나 어느 쪽이 타당하다고 "
            "단정하지 마시오. 지침이 우선순위를 정하지 않았다는 사실만 밝히시오.\n"
            "   - 마커에는 변수명을 그대로 표기하시오"
            "(예: 「계산: required_beds_catchment」)."
        )
    if inputs_dict:
        instructions.append(
            "※ [인용 표기 규칙·조달 입력값] '조달 입력값' 항목의 값은 계산 결과가 "
            "아니라 데이터베이스·사업계획서에서 가져온 사실임. 이 값을 담은 문장에는 "
            "「계산」을 쓰지 말고, 그 항목에 [출처: …]가 적혀 있으면 그 출처명을, "
            "적혀 있지 않으면 「입력: <변수명>」을 「」 안에 표기하시오.\n"
            "   - 예: '총 병상은 300병상으로 계획됨 「사업 기본계획서」.'\n"
            "   - 예: '지역친화도는 100%로 가정됨 「입력: ri」.'\n"
            "   - 조달 입력값을 이용해 새 수치를 직접 계산하지 마시오."
        )
    if unresolved:
        instructions.append(
            "※ [미산출 항목의 명시] 아래 '미산출 항목'은 지침이 요구하나 입력 자료가 "
            "확보되지 않아 산출되지 않은 값임. 해당 항목을 서술에서 빠뜨리지 말고, "
            "값을 만들거나 근사하지 말고, 산출되지 않았다는 사실과 그 사유(확보되지 "
            "않은 자료가 무엇인지)를 본문에 명시하시오. 이때 변수명(key)을 그대로 "
            "쓰지 말고 그 값이 무엇인지 한국어로 서술하시오. 이 문장에는 인용 마커를 "
            "달지 마시오."
        )
    if tables_str:
        instructions.append(
            "※ 제공된 표 데이터의 수치만 사용하고, 표에 없는 수치를 본문에 직접 "
            "만들지 마시오. 표 번호를 추측·생성하지 마시오."
        )
    if section_id.startswith("3-2"):
        instructions.append(
            "※ [지침 제시 방법의 처리] 지침 청크가 해당 유형에 대해 제시하는 진료권 "
            "설정 방법이 둘 이상이면, 각 방법에 대해 적용 여부를 밝히시오. 자료 부재로 "
            "적용하지 못한 방법은 생략하지 말고, 그 방법과 함께 적용에 필요한데 확보되지 "
            "않은 구체적 데이터가 무엇인지를 상세히 명시하고, 그로 인해 대신 사용한 "
            "방법을 밝히시오. 지침에 제시되지 않은 방법을 임의로 추가하지 마시오. "
            "제시된 방법이 하나뿐인 유형이면 이 항목은 적용되지 않음.\n"
            "   - 적용 여부를 밝힌 문장에는, 적용한 경우든 적용하지 않은 경우든, 그 "
            "방법을 규정한 지침 청크의 id 를 인용 표기 규칙에 따라 「」 안에 표기하시오. "
            "미적용 판단도 지침에 근거한 판단이므로 근거를 밝혀야 하며, 판단의 근거가 "
            "된 청크가 여럿이면 세미콜론으로 나열하시오."
        )
        instructions.append(
            "※ [§3-2 진료권 결론 표기] 가이드라인 논리에 따라 본문에서 도출한 '최종 "
            "진료권'을 서술 맨 끝에 아래 형식으로 명시하시오. 어떤 지역을 왜 포함·제외했는지 "
            "그 근거는 [최종 진료권] 블록 '앞' 본문에서 밝히고, 블록 안에는 진료권에 "
            "포함하기로 결론 내린 지역만 적으시오(제외한 지역명이나 근거 설명을 블록 안에 "
            "넣지 마시오). 거리·경쟁시설 등 맥락으로만 언급한 지역은 넣지 마시오. 진료권을 "
            "진료 대상에 따라 둘 이상으로 나누었다면 줄을 나누어 각각 적으시오. "
            "분석 단계·절차는 지침 청크가 제시하는 바를 따르고 임의로 만들지 마시오.\n"
            "   이어서, 본문에서 판단한 신규 의료시설의 진료 수준을 [신규 시설 진료 수준] "
            "블록에 '종합병원급' 또는 '상급종합병원급' 중 하나로 적으시오. 판단 근거는 "
            "블록 '앞' 본문에서 밝히시오.\n"
            "   형식(블록 안에는 포함 지역과 진료 수준만):\n"
            "   [최종 진료권]\n"
            "   - <진료권 구분>: <지역명>, <지역명>, ...\n"
            "   [신규 시설 진료 수준]\n"
            "   - <종합병원급 또는 상급종합병원급>"
        )
    if confirmed_catch:
        instructions.append(
            "※ [확정 진료권] '확정 진료권' 항목은 진료권 설정 소절에서 이번 실행이 확정한 "
            "진료권임. 진료권의 지역 구성을 언급할 때는 이 항목에 적힌 지역만 쓰고, "
            "진료권을 다시 판단하지 마시오. 이 사실을 담은 문장에는 "
            "「진료권 설정 결과」를 표기하시오."
        )
    # ── Subheadings as explicit blocks ────────────────────────────────────────
    # Subheadings are separated at generation time so that the evaluator does not have to infer
    # which lines are body sentences. A subheading is not a factual claim and is not evaluated.
    instructions.append(
        "※ [소제목 표기] 제목·표제 성격의 줄은 위치를 불문하고 반드시 '[소제목] '으로 "
        "시작하는 별도 줄로 쓰시오(예: '[소제목] 거리 및 이동시간에 따른 진료권 검토').\n"
        "   - 소절의 표제는 별도로 관리되므로 서술 첫머리에 다시 쓰지 마시오. "
        "부득이 쓰는 경우에도 소절 번호 없이 '[소제목] '으로 시작하시오.\n"
        "   - 다음과 같은 표제 형식을 쓰지 마시오: "
        "'[3-4-1] 연간 입원환자 수의 추정', '### 3-5-2. 응급사망자 수 추정', "
        "'# 제Ⅲ장 수요 추정', '제1절 수요 추정 및 진료권 설정'.\n"
        "   - 소제목 줄에는 인용 마커를 달지 마시오. 본문 문장은 '[소제목]' 없이 "
        "그대로 쓰고 규칙에 따라 마커를 다시오.\n"
        "   - 소제목을 쓰지 않고 서술해도 무방하며, 소제목을 만들기 위해 없는 항목을 "
        "지어내지 마시오.\n"
        "   - 단, '[최종 진료권]'·'[신규 시설 진료 수준]' 블록은 소제목이 아니라 지정된 "
        "결과 표기 형식이므로 '[소제목]'을 붙이지 말고 지시된 형식 그대로 쓰시오."
    )

    instruction_block = "\n".join(instructions)

    user_prompt = (
        f"다음 자료를 바탕으로 예비타당성조사 보고서 소절 [{section_id}]의 본문을 "
        f"작성하시오.\n"
        f"(소절 번호 '[{section_id}]'는 작성 대상을 지정하기 위한 표기이며, 본문이나 "
        f"표제로 옮겨 적지 마시오.)\n\n"
        f"{instruction_block}"
        f"\n\n## 사업 개요 (기본계획서)\n"
        f"{project_context if project_context else '없음'}"
        f"\n\n## KDI 세부지침\n"
        f"{guideline_block if guideline_block else '없음'}"
    )
    if calc_block.strip():
        user_prompt += f"\n\n## 원문 표 데이터 / 계산 수치\n{calc_block.strip()}"
    if inputs_dict:
        lines = []
        for k, v in inputs_dict.items():
            src = input_sources.get(k)
            lines.append(f"{k}: {v}" + (f"  [출처: {src}]" if src else ""))
        user_prompt += "\n\n## 조달 입력값 (DB·사업계획서에서 가져온 사실)\n" + "\n".join(lines)
    if unresolved:
        user_prompt += "\n\n## 미산출 항목 (지침 요구 항목 중 입력 부족으로 산출 불가)\n" \
                       + "\n".join(f"- {u}" for u in unresolved)
    if catchment_data:
        user_prompt += f"\n\n## 진료권 분석 자료 (raw)\n{catchment_data}"
    if confirmed_catch:
        user_prompt += f"\n\n## 확정 진료권 (진료권 설정 소절의 결과)\n{confirmed_catch}"
    if confirmed_block:
        user_prompt += f"\n\n## 이전 소절 확정값\n{confirmed_block}"

    # On regeneration, pass the previous Critic verdict. The first generation has no such block.
    # The previous draft itself is not passed.
    crit = state.get("critique") or {}
    if state.get("retry_count", 0) >= 1 and crit:
        gc = crit.get("guideline_check") or {}
        ic = crit.get("interpretation_check") or {}
        viol = ic.get("violations") or []
        user_prompt += (
            "\n\n## 직전 생성본에 대한 검증 결과\n"
            "직전에 생성한 본문은 검증을 통과하지 못했음.\n"
            f"- 지침 준수 점수: {gc.get('score', '-')}점 (3점 이상이어야 통과)\n"
            f"- 지침 준수 판정 이유: {gc.get('reason', '-')}\n"
            f"- 해석 금지 표현 검출: {', '.join(viol) if viol else '없음'}\n"
            "위 검증 결과를 반영하여 본문을 다시 작성하시오. 이때에도 위의 모든 지시와 "
            "규칙을 그대로 따르시오."
        )

    return _STYLE, user_prompt


def writer_agent(state: GraphState) -> GraphState:
    system_prompt, user_prompt = _build_context(state)

    draft_text = generate_text(user_prompt, system=system_prompt)
    print(f"[Writer] {state['section_id']} 생성 완료: {len(draft_text)}자")

    return {**state, "draft_text": draft_text}