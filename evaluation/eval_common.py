# eval_common.py — parsing shared by the evaluators
#
# Purpose:
#   The three evaluators (eval_l1 / eval_rg / alce_eval) share one definition of what a
#   body sentence is and what a marker refers to.
#
#
# Conventions (one-to-one with the generation side, writer.py):
#   Line types
#     - '[소제목] …'             subheading (follows the convention)
#     - '[최종 진료권]' + '- …'  result block of section 3-2 (a prescribed form, not a subheading)
#     - starts with '#'/'##'/'###'  heading (violation — markdown is not allowed)
#     - '[3-4-1] …'              heading (violation — subsection number as a title)
#     - anything else            body
#   Markers (inside 「」, several separated by semicolons)
#     - '202212-III-3-1'         guideline citation (the first six digits are the guideline version)
#     - '계산: required_beds'    value produced by a formula
#     - '입력: ri'               value obtained from the database or the project plan
#     - anything else            name of a data source
#
# Design principle:
#   Headings are not identified by content (length, sentence ending). Everything is matched
#   by form. Lines that violate the convention are treated as headings but counted
#   separately as violations, so that the compliance rate can be reported.

from __future__ import annotations

import json
import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 1) Line classification
# ─────────────────────────────────────────────────────────────────────────────

LINE_SUBHEAD_OK = "subhead_ok"  # '[소제목] …'      subheading following the convention
LINE_SUBHEAD_MD = "subhead_md"  # '### …'           violation (markdown)
LINE_SUBHEAD_NUM = "subhead_num"  # '[3-4-1] …'       violation (subsection number)
LINE_RESULT_BLOCK = "result_block"  # '[최종 진료권]', '- <region>…'
LINE_BODY = "body"  # body

_RE_SUBHEAD_OK = re.compile(r"^\[소제목\]\s*")
_RE_MD_HEAD = re.compile(r"^#{1,6}\s")
_RE_SECTION_LABEL = re.compile(r"^\[\d+(?:-\d+)*\]")
# '[신규 시설 진료 수준]' in the result block of section 3-2 is not counted as a body sentence.
_RE_RESULT_HEAD = re.compile(r"^\[(최종\s*진료권|신규\s*시설\s*진료\s*수준)\]")
_RE_RESULT_ITEM = re.compile(r"^-\s")


def classify_line(line: str, in_result_block: bool = False) -> str:
    """Classify one line by form only.

        in_result_block: whether the previous line was inside a '[최종 진료권]' block.
          Needed so that a '- <category>: …' line inside the block is not counted as a body
          sentence. (A '- ' line outside the block may be a body list and stays as body.)
    """
    s = line.strip()
    if _RE_SUBHEAD_OK.match(s):
        return LINE_SUBHEAD_OK
    if _RE_RESULT_HEAD.match(s):
        return LINE_RESULT_BLOCK
    if in_result_block and _RE_RESULT_ITEM.match(s):
        return LINE_RESULT_BLOCK
    if _RE_MD_HEAD.match(s):
        return LINE_SUBHEAD_MD
    if _RE_SECTION_LABEL.match(s):
        return LINE_SUBHEAD_NUM
    return LINE_BODY


def iter_lines(text: str):
    """Yield (line, type) in order, tracking the state of the '[최종 진료권]' block."""
    in_block = False
    for raw in (text or "").split("\n"):
        s = raw.strip()
        if not s:
            in_block = False  # a blank line ends the block
            continue
        kind = classify_line(s, in_block)
        if kind == LINE_RESULT_BLOCK:
            in_block = True
        elif kind != LINE_RESULT_BLOCK:
            # A line that does not start with '- ' ends the block
            if in_block and not _RE_RESULT_ITEM.match(s):
                in_block = False
        yield s, kind


# ─────────────────────────────────────────────────────────────────────────────
# 2) Sentence splitting
# ─────────────────────────────────────────────────────────────────────────────
# A boundary is a period that (a) is not between digits (decimal point, thousands separator),
# (b) follows a Hangul character, a parenthesis, or 」, and (c) is followed by whitespace or
# the end of the text. A marker 「…」 comes before the period and is therefore kept.

_RE_SENT_SPLIT = re.compile(r"(?<=[가-힣\)\]」])\.(?=\s|$)")


def split_sentences(line: str) -> list[str]:
    """Split one body line into sentences. Fragments shorter than 2 characters are dropped."""
    out = []
    for p in _RE_SENT_SPLIT.split(line):
        p = p.strip()
        if len(p) >= 2:
            out.append(p)
    return out


def body_sentences(text: str) -> list[str]:
    """Return the sentences of the body lines only (subheadings and result blocks excluded)."""
    out = []
    for s, kind in iter_lines(text):
        if kind == LINE_BODY:
            out.extend(split_sentences(s))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 3) Marker parsing
# ─────────────────────────────────────────────────────────────────────────────

MARKER_GUIDELINE = "guideline"
MARKER_GUIDELINE_TABLE = "guideline_table"
MARKER_CALC = "calc"
MARKER_INPUT = "input"
MARKER_DATA = "data"

MARKER_TYPES = (MARKER_GUIDELINE, MARKER_GUIDELINE_TABLE,
                MARKER_CALC, MARKER_INPUT, MARKER_DATA)

_RE_MARKER = re.compile(r"「([^」]+)」")
_RE_CHUNK_ID = re.compile(r"^\d{6}-\S+$")
# Reference to a guideline table, in the form prescribed by rule 4 of the Writer
# ('KDI 세부지침 <표 Ⅲ-1>'). It has no chunk id, so counting it as a guideline citation
# would distort the denominator of the rate of nonexistent citation ids. It is not a data
# source either (it is a table of the guideline itself), so it is counted separately.
_RE_GUIDELINE_TABLE = re.compile(r"^KDI\s*세부지침")
_PREFIX_CALC = "계산:"
_PREFIX_INPUT = "입력:"
# A marker may carry the prefix '출처:' (e.g. 「출처: 시도추계(v2022) …」); the prefix is removed.
_PREFIX_SOURCE = "출처:"


def parse_markers(sentence: str) -> dict:
    """Break the markers of a sentence down by type.

        Returns:
          {"guideline": [id, ...],      # guideline chunk ids
           "calc":      [key, ...],     # key of 「계산: key」
           "input":     [key, ...],     # key of 「입력: key」
           "data":      [label, ...],   # other source names
           "raw":       [original token, ...],
           "has_marker": bool}

        Multiple markers are split on semicolons only. Splitting on commas would break source
        names that contain commas or parentheses.
    """
    res = {t: [] for t in MARKER_TYPES}
    res.update({"raw": [], "has_marker": False})
    for blk in _RE_MARKER.findall(sentence):
        res["has_marker"] = True
        for tok in blk.split(";"):
            tok = tok.strip()
            if not tok:
                continue
            res["raw"].append(tok)
            if tok.startswith(_PREFIX_CALC):
                res[MARKER_CALC].append(tok[len(_PREFIX_CALC):].strip())
            elif tok.startswith(_PREFIX_INPUT):
                res[MARKER_INPUT].append(tok[len(_PREFIX_INPUT):].strip())
            else:
                bare = tok[len(_PREFIX_SOURCE):].strip() \
                    if tok.startswith(_PREFIX_SOURCE) else tok
                if _RE_CHUNK_ID.match(bare):
                    res[MARKER_GUIDELINE].append(bare)
                elif _RE_GUIDELINE_TABLE.match(bare):
                    res[MARKER_GUIDELINE_TABLE].append(bare)
                else:
                    res[MARKER_DATA].append(bare)
    return res


def strip_markers(text: str) -> str:
    """The sentence with its markers removed (for the judge input and for manual checking)."""
    return re.sub(r"\s*「[^」]*」", "", text).strip()


def guideline_version(chunk_id: str) -> str:
    """First six digits of a chunk id = guideline version ('202212' medical, '202105' culture and tourism)."""
    return chunk_id.split("-", 1)[0]


# ─────────────────────────────────────────────────────────────────────────────
# 4) Loading outputs
# ─────────────────────────────────────────────────────────────────────────────

RUN_META_KEY = "_run"


def load_run(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def iter_sections(run: dict):
    """Yield (section_id, section_dict), skipping the run metadata ('_run')."""
    for sid, sec in run.items():
        if sid.startswith("_") or not isinstance(sec, dict):
            continue
        yield sid, sec


def find_runs(runs_dir: str | Path) -> list[Path]:
    """Return the chapter3_*.json files under runs_dir, sorted (subfolders included)."""
    return sorted(Path(runs_dir).rglob("chapter3_*.json"))


def project_of(run: dict, path: Path | None = None) -> str:
    """project_id of a run. Inferred from the file name if there is no metadata."""
    meta = run.get(RUN_META_KEY) or {}
    pid = meta.get("project_id")
    if pid:
        return pid
    if path is not None:
        m = re.match(r"chapter3_(.+?)(?:_r\d+)?\.json$", path.name)
        if m:
            return m.group(1)
    return "UNKNOWN"


def run_index_of(path: Path) -> str:
    """Run number from the file name (r01 -> '01'). '00' if absent."""
    m = re.search(r"_r(\d+)\.json$", path.name)
    return m.group(1) if m else "00"

def describe_runs_dir(runs_dir) -> str:
    """Diagnostic text for when the --runs path is not found.

        Shows the resolved absolute path and what is actually there, to tell a wrong path from
        missing files.
    """
    p = Path(runs_dir).resolve()
    lines = [f"  해석된 절대경로: {p}"]
    if not p.exists():
        lines.append("  → 그 경로가 존재하지 않습니다. 현재 작업 폴더: "
                     f"{Path.cwd()}")
        parent = p.parent
        if parent.exists():
            kids = [c.name for c in parent.iterdir() if c.is_dir()][:12]
            lines.append(f"  → 상위 폴더({parent})의 하위 폴더: {kids}")
        return "\n".join(lines)
    subs = [c.name for c in p.iterdir() if c.is_dir()][:12]
    files = [c.name for c in p.iterdir() if c.is_file()][:8]
    lines.append(f"  경로는 존재합니다. 하위 폴더: {subs}")
    lines.append(f"  파일: {files}")
    lines.append("  → chapter3_*.json 을 재귀 탐색했으나 하나도 없습니다. "
                 "산출물 폴더(outputs/runs)를 가리키는지 확인하십시오.")
    return "\n".join(lines)
