# agents/sub/formula_catalog.py — catalog of the formulas stated in the guideline
# ─────────────────────────────────────────────────────────────────────────────
#
# Design basis:
#   - PAL (Gao et al., 2022, arXiv:2211.10435): calculation is delegated to a deterministic runtime.
#   - Blueprint First (arXiv:2508.02721): the workflow path is deterministic and the LLM is
#     used only as a tool for bounded subtasks, which keeps the process auditable.
#
# Principle:
#   Only formulas stated in the guideline text are registered. Each formula declares its
#   required_inputs, and it is executed deterministically in any subsection once those
#   inputs are available from the database. What triggers a calculation is whether the
#   inputs of a guideline formula are satisfied, not a label assigned to the subsection.
#
# Criteria for registration:
#   - The formula is written out in the text of the KDI detailed guideline for the medical
#     sector (Dec. 2022). Each Formula.guideline_ref gives its location in the guideline.
#   - Values that depend on the case or the year (e.g. the national average number of beds
#     per 1,000 population) and case-specific constants are inputs, not formulas; they are
#     not in the catalog and are obtained from the database.
#
# Notes:
#   - The catalog holds only the structure of each formula. The caller (the Computation
#     Agent) obtains the input values from the database. The catalog knows only what the
#     inputs are (required_inputs) and how to compute (compute).
#   - Methodological choices that the guideline leaves open are not registered.
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Formula:
    """One deterministic formula stated in the guideline.

        Attributes:
          formula_id:      identifier (snake_case)
          guideline_ref:   location in the KDI guideline text, for citation and tracing
          description:     description of the formula in the guideline's own terms
          required_inputs: input keys needed. compute is run only when the caller has
                           obtained all of them from the database.
          compute:         pure function from the input dict to the result (float/int). No database access.
    """
    formula_id: str
    guideline_ref: str
    description: str
    required_inputs: tuple[str, ...]
    compute: Callable[[dict], float]

    def can_run(self, available: dict) -> bool:
        """Whether every key in required_inputs is present in available (the obtained inputs) and not None."""
        return all(
            k in available and available[k] is not None
            for k in self.required_inputs
        )

    def missing_inputs(self, available: dict) -> list[str]:
        """Input keys that are missing; used to record why a formula was skipped."""
        return [
            k for k in self.required_inputs
            if k not in available or available[k] is None
        ]

    def run(self, available: dict) -> float:
        """Run the calculation. Call can_run first to avoid a KeyError when inputs are missing."""
        return self.compute({k: available[k] for k in self.required_inputs})


# ─────────────────────────────────────────────────────────────────────────────
# Formula definitions — all stated in the text of the KDI guideline
# ─────────────────────────────────────────────────────────────────────────────
#
# Formulas in the guideline (예타_세부지침_의료부문_KDI_202212):
#   annual inpatients = population x inpatient utilization rate (days) x relevance index (RI)
#   annual capacity per bed = bed utilization x 365 x commitment index (CI)
#   required beds = annual inpatients / annual capacity per bed
#   beds of a specific type = required beds x share of inpatients of that type
#   RI = CI = 100% is assumed
#   additional beds = (national average beds per 1,000 - local value) x local population / 1000
#   annual inpatients (facility) = beds x bed utilization x 365
#   annual outpatients = annual inpatients x outpatient-to-inpatient ratio
#   emergency deaths = projected population x emergency mortality rate (m)
#   preventable deaths = emergency deaths x reduction in the emergency mortality rate
# ─────────────────────────────────────────────────────────────────────────────


# Section 3-3, Method 1 — required beds in the catchment
_annual_inpatient_method1 = Formula(
    formula_id="annual_inpatient_method1",
    guideline_ref="KDI 지침 §3-3 (L1725, L1769)",
    description="연간 입원환자 수 = 인구 × 입원의료이용률(1인당 연평균입원일수) × 지역친화도(RI)",
    required_inputs=("population", "inpatient_days", "ri"),
    compute=lambda d: d["population"] * d["inpatient_days"] * d["ri"],
)

_capacity_per_bed = Formula(
    formula_id="capacity_per_bed",
    guideline_ref="KDI 지침 §3-3 (L1729, L1768)",
    description="병상당 연간 수용능력 = 병상이용률 × 365 × 지역환자구성비(CI)",
    required_inputs=("bed_utilization", "ci"),
    compute=lambda d: d["bed_utilization"] * 365 * d["ci"],
)

_required_beds_method1 = Formula(
    formula_id="required_beds_method1",
    guideline_ref="KDI 지침 §3-3 (L1735~1768)",
    description="요구 병상 수 = 연간 입원환자 수 ÷ 병상당 연간 수용능력",
    required_inputs=("annual_inpatient", "capacity_per_bed"),
    # The result is not rounded. The guideline has no rule on rounding or decimal places, so the
    # value is returned as the formula gives it.
    compute=lambda d: d["annual_inpatient"] / d["capacity_per_bed"],
)

_specific_type_beds = Formula(
    formula_id="specific_type_beds",
    guideline_ref="KDI 지침 §3-3 (L1768)",
    description="특정유형 병상 수 = 요구 병상 수 × 특정유형 입원환자 비중",
    required_inputs=("required_beds", "type_share"),
    compute=lambda d: d["required_beds"] * d["type_share"],
)


# Section 3-3, Method 2 — additional beds needed, based on bed supply relative to population
_delta_beds_method2 = Formula(
    formula_id="delta_beds_method2",
    guideline_ref="KDI 지침 §3-3 (L1798~1802)",
    description=("추가 소요 병상(△병상) = (인구 천명당 병상수의 전국평균 − 지역값) "
                 "× 지역인구 / 1000"),
    required_inputs=("national_per_1000", "local_per_1000", "population"),
    compute=lambda d: (
        (d["national_per_1000"] - d["local_per_1000"]) * d["population"] / 1000
    ),
)

_local_per_1000 = Formula(
    formula_id="local_per_1000",
    guideline_ref="KDI 지침 §3-3 (L1789~1798, 천명당 병상수 정의)",
    description="지역 인구 천명당 병상 수 = 공급 병상 수 ÷ (지역인구 / 1000)",
    required_inputs=("supply_beds", "population"),
    compute=lambda d: d["supply_beds"] / (d["population"] / 1000),
)


# Section 3-4 — annual inpatients and outpatients of the facility
_annual_inpatient_facility = Formula(
    formula_id="annual_inpatient_facility",
    guideline_ref="KDI 지침 §3-4 (L1882)",
    description="연간 입원환자 수(시설) = 병상수 × 병상이용률 × 365",
    required_inputs=("beds", "bed_utilization"),
    compute=lambda d: d["beds"] * d["bed_utilization"] * 365,
)

# Outpatients in section 3-4 are chained from the facility's inpatients in section 3-4.
# Section 4 of the guideline states that the required beds of the catchment (section 3-3,
# Method 1: demand of the whole catchment) are not the number of patients who use the
# facility. The facility's annual inpatients = beds x bed utilization x 365
# (annual_inpatient_facility), and annual outpatients = that inpatient estimate x the
# outpatient-to-inpatient ratio. required_inputs therefore names
# 'annual_inpatient_facility' rather than the generic 'annual_inpatient', which is shared
# with the Method 1 alias, so that the section 3-4 chain stays separate from section 3-3.
_annual_outpatient = Formula(
    formula_id="annual_outpatient",
    guideline_ref="KDI 지침 §3-4 (L1926; 제4절 본문 확인)",
    description="연간 외래환자 수 = 연간 입원환자 수(시설) × 입원대비외래 배율",
    required_inputs=("annual_inpatient_facility", "outpatient_ratio"),
    compute=lambda d: d["annual_inpatient_facility"] * d["outpatient_ratio"],
)


# Section 3-5 — emergency deaths / preventable deaths
_emergency_deaths = Formula(
    formula_id="emergency_deaths",
    guideline_ref="KDI 지침 §3-5 (L2167)",
    description="응급사망자 수 = 인구 추계 × 응급사망률(m)",
    required_inputs=("population", "death_rate"),
    compute=lambda d: d["population"] * d["death_rate"],
)

_preventable_deaths = Formula(
    formula_id="preventable_deaths",
    guideline_ref="KDI 지침 §3-5 (L2164)",
    description="예방 가능 사망자 수 = 응급사망자 수 × 응급사망률 감소율",
    required_inputs=("emergency_deaths", "death_rate_reduction"),
    compute=lambda d: d["emergency_deaths"] * d["death_rate_reduction"],
)

# The reduction in the emergency mortality rate applies the estimate of the regression
# model in the guideline (Table III-14, Model 3: about a 0.13% reduction for a 1% increase
# in beds). The coefficient is a guideline reference value (kdi_reference_values).
# Reduction rate = rate of increase in emergency beds x elasticity (regression coefficient).
_death_rate_reduction = Formula(
    formula_id="death_rate_reduction",
    guideline_ref="KDI 지침 §3-5-3 (표 Ⅲ-14 모형3 적용)",
    description="응급사망률 감소율 = 응급병상 증가율(%) × 응급병상 탄력성(회귀계수)",
    required_inputs=("bed_increase_rate_pct", "elasticity"),
    # The elasticity (regression coefficient) is negative (-0.12917: more beds, lower mortality).
    # The reduction rate is its magnitude, so abs() is applied; the direction is carried by the
    # variable name. Example: an increase of 133.3% -> |133.3 x (-0.12917) / 100| = 0.1722 (17.2%).
    compute=lambda d: abs(d["bed_increase_rate_pct"] * d["elasticity"] / 100),
)


# ─────────────────────────────────────────────────────────────────────────────
# Catalog registration
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Two variants for bed utilization
# Section 3-4-1 of the guideline lists the bases for bed utilization — the average of the
# catchment, the bed utilization for the type of the facility, or that of similar
# facilities — without ranking them. Both variants are therefore computed, and the choice
# is left to the user.
#
# Rule: every formula that takes bed_utilization as an input, directly or indirectly, is
# duplicated as _catchment and _national, with the same suffix on its input and output keys
# so that the two chains stay separate. Formulas that do not use bed utilization
# (delta_beds_method2, annual_inpatient_method1, local_per_1000, the emergency formulas)
# are kept as a single copy.
_BU_VARIANT_SOURCES = [
    _capacity_per_bed, _required_beds_method1, _specific_type_beds,
    _annual_inpatient_facility, _annual_outpatient,
]
_BU_CHAIN_KEYS = {
    "bed_utilization", "capacity_per_bed", "required_beds",
    "specific_type_beds", "annual_inpatient_facility", "annual_outpatient",
}
BU_VARIANTS = ("catchment", "national")
BU_VARIANT_LABEL = {
    "catchment": "해당 진료권의 평균 병상이용률",
    "national": "대상 의료시설 종별에 따른 전국 병상이용률",
}


def _suffix_key(key: str, variant: str) -> str:
    return f"{key}_{variant}" if key in _BU_CHAIN_KEYS else key


def _make_variant(base: Formula, variant: str) -> Formula:
    """Duplicate a base formula for one variant. compute is reused after mapping the keys back."""
    new_inputs = tuple(_suffix_key(k, variant) for k in base.required_inputs)
    original = base.required_inputs

    def compute(d, _orig=original, _v=variant, _f=base.compute):
        return _f({k: d[_suffix_key(k, _v)] for k in _orig})

    return Formula(
        formula_id=f"{base.formula_id}_{variant}",
        guideline_ref=base.guideline_ref,
        description=f"{base.description} [{BU_VARIANT_LABEL[variant]} 기준]",
        required_inputs=new_inputs,
        compute=compute,
    )


_BU_VARIANT_FORMULAS: dict[str, Formula] = {}
for _base in _BU_VARIANT_SOURCES:
    for _v in BU_VARIANTS:
        _f = _make_variant(_base, _v)
        _BU_VARIANT_FORMULAS[_f.formula_id] = _f


CATALOG: dict[str, Formula] = {
    f.formula_id: f for f in [
        _annual_inpatient_method1,
        _delta_beds_method2,
        _local_per_1000,
        _emergency_deaths,
        _preventable_deaths,
        _death_rate_reduction,
        # The five formulas that use bed utilization (_capacity_per_bed, _required_beds_method1,
        # _specific_type_beds, _annual_inpatient_facility, _annual_outpatient) are not registered
        # here. Registering them would create keys without a suffix and mix the section 3-3 and 3-4
        # chains with the two variants. Only the _catchment/_national variants are added by
        # CATALOG.update() below.
    ]
}

# Merge the two bed-utilization variants into the catalog. The five base formulas
# themselves are not registered (see above).
CATALOG.update(_BU_VARIANT_FORMULAS)


def get_formula(formula_id: str) -> Formula | None:
    return CATALOG.get(formula_id)


def runnable_formulas(available: dict) -> list[Formula]:
    """Formulas that can run with the inputs obtained so far (available).

        Triggered only by input availability. Some formulas take the output of another formula
        as an input (e.g. required_beds_method1 <- annual_inpatient, capacity_per_bed), so the
        caller resolves the chain by topological ordering or repeated passes.
    """
    return [f for f in CATALOG.values() if f.can_run(available)]


def resolve_chain(seed_inputs: dict, max_passes: int = 5) -> dict:
    """Run the formulas whose inputs are satisfied, repeatedly, to resolve the dependency chain.

        seed_inputs: source inputs obtained from the database (population, inpatient_days, ri, ...).
        Returns: a dict accumulating the seed and the outputs of every formula that could run.

        How it works:
          - Each pass runs the formulas that have no result yet and whose inputs are available.
          - It stops when nothing more can run; max_passes prevents an infinite loop.
          - The output key of a formula is its formula_id, which other formulas may name as a
            required input, provided the key names agree (see ALIAS below).

        Key agreement:
          An output feeds an input only if the key names are the same. For example,
            annual_inpatient_method1 -> output key 'annual_inpatient_method1'
            required_beds_method1    -> requires the input 'annual_inpatient'
          ALIAS absorbs this difference.
    """
    acc = dict(seed_inputs)

    for _ in range(max_passes):
        progressed = False
        for f in CATALOG.values():
            if f.formula_id in acc:
                continue  # already computed
            if f.can_run(acc):
                acc[f.formula_id] = f.run(acc)
                # Expose the output under its alias as well, so that later formulas can refer to it
                alias = OUTPUT_ALIAS.get(f.formula_id)
                if alias and alias not in acc:
                    acc[alias] = acc[f.formula_id]
                progressed = True
        if not progressed:
            break

    return acc


def compute_with_report(seed_inputs: dict, max_passes: int = 5) -> dict:
    """Return the results together with what was executed and what was skipped.

        There is no fallback: a formula without its inputs is not run, and the missing inputs
        are recorded. The record of skipped formulas shows where the report could not be
        generated as the guideline prescribes.

        Returns:
          {
            "numbers":  {formula_id: value, ...},    # outputs of executed formulas only
            "executed": [formula_id, ...],           # executed formulas
            "skipped":  {formula_id: [missing_keys]} # skipped formulas and their missing inputs
          }
    """
    acc = resolve_chain(seed_inputs, max_passes=max_passes)

    executed = [fid for fid in CATALOG if fid in acc]
    skipped = {}
    for fid, f in CATALOG.items():
        if fid not in acc:
            skipped[fid] = f.missing_inputs(acc)

    # Normalize the output keys to their semantic names (aliases)
    # The consumer (the per-subsection exposure filter in Computation) matches on semantic names
    # ('annual_inpatient', 'required_beds', 'delta_beds'), so an output keyed only by a
    # formula_id with a '_method1' / '_method2' suffix would fail the exact match and be dropped
    # without a warning. The keys of numbers therefore use the alias when there is one.
    # executed/skipped keep the formula_id so that identifiers in the log stay consistent.
    numbers = {}
    for fid in executed:
        key = OUTPUT_ALIAS.get(fid, fid)
        if key in numbers:  # guard against an alias collision (should not occur by design)
            print(f"[formula_catalog] 경고: 별칭 충돌 '{key}' "
                  f"(formula_id={fid}) — formula_id 키로 노출")
            key = fid
        numbers[key] = acc[fid]

    return {
        "numbers":  numbers,
        "executed": executed,
        "skipped":  skipped,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Aliases between output keys and input keys
# ─────────────────────────────────────────────────────────────────────────────
# The output key of a formula is its formula_id, while required_inputs of later formulas use
# semantic keys. compute_with_report normalizes the keys of numbers with these aliases, so
# the keys seen by consumers (the exposure filter in Computation, the citation markers of
# the Writer) are the semantic names given here. When a formula is added whose formula_id
# differs from its semantic name, an alias must be registered.
# Example: the result 'annual_inpatient_method1' is also exposed as 'annual_inpatient'.
#
# An alias states which input of formula B the output of formula A is, as given by the
# structure of the guideline formulas; it does not encode any case-specific conclusion.
# Aliases for the two bed-utilization variants are extended by the same rule
# (see _extend_alias_for_variants below).
OUTPUT_ALIAS: dict[str, str] = {
    "annual_inpatient_method1": "annual_inpatient",
    "capacity_per_bed":         "capacity_per_bed",
    "required_beds_method1":    "required_beds",
    # Without this alias, 'delta_beds_method2' does not match the exposure term 'delta_beds' and
    # the additional beds of section 3-3-2 are not passed to the Writer.
    "delta_beds_method2":       "delta_beds",
    "local_per_1000":           "local_per_1000",
    # annual_inpatient_facility is not aliased to 'annual_inpatient'. If Method 1 (section 3-3)
    # and the facility formula (section 3-4) shared the key 'annual_inpatient', Method 1, which
    # runs first, would take the alias and outpatients (section 3-4) would be computed from the
    # Method 1 inpatients. Outpatients refer to the key 'annual_inpatient_facility' directly,
    # so no alias is needed; the facility output is always exposed under its formula_id.
    "emergency_deaths":         "emergency_deaths",
    "death_rate_reduction":     "death_rate_reduction",
}


def _extend_alias_for_variants() -> None:
    """Apply the base aliases (annual_inpatient_method1 -> annual_inpatient, etc.) to the variants.

        e.g. required_beds_method1_catchment -> required_beds_catchment
        For formulas without an alias (specific_type_beds, etc.), the formula_id is already
        the semantic name, so the variant id is the semantic name and nothing more is needed.
    """
    add = {}
    for base_id, alias in list(OUTPUT_ALIAS.items()):
        for v in BU_VARIANTS:
            vid = f"{base_id}_{v}"
            if vid in CATALOG:
                add[vid] = f"{alias}_{v}"
    OUTPUT_ALIAS.update(add)


_extend_alias_for_variants()