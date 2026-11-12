# make_flip_audit.py — builds the audit sheet for explicature flips
#
# Why it is needed
# ─────────────────────────────────────────────────────────────────────────────
# The decisions that flip when explicature is applied are all attributable to the rewrite,
# but the rewrites are not homogeneous: some become much longer than the original, some
# still begin with a referring expression, and some are almost identical to the original.
# The cause of a flip is therefore not always the resolution of a referring expression, and
# the mechanism has to be separated before a change in recall is reported.
#
# Axis of judgment
# ─────────────────────────────────────────────────────────────────────────────
# One question is asked — is this flip a legitimate effect of explicature?
#
# A proper substitution  Only the referring expression is replaced by its referent in the
#                        preceding sentences and no proposition is added. The flip is
#                        substantive. Only this category is counted.
# B merged proposition   A clause or proposition of a preceding sentence is pulled in, so
#                        that more than one thing is judged. The sentence unit of ALCE is
#                        broken; invalid.
# C failed rewrite       The referring expression remains / only the ending or style
#                        changed / ungrammatical. The input is in effect the same while the
#                        decision differs; invalid.
# D added fact           Content not in the preceding sentences is generated. Invalid, and
#                        reported separately.
#
# For items judged A, one auxiliary axis is recorded.
#   ref_project (Y/N) — whether the substituted referent is a project-specific fact (project
#   name, site, number of beds, etc.).
#
# Anchoring
# ─────────────────────────────────────────────────────────────────────────────
# The direction of the flip (resolved / reversed) is not shown in the sheet, since knowing
# it would bias the judgment. The direction is kept only in the key.
#
# Usage:
#   python make_flip_audit.py --flips outputs/eval_explicature/explicature_flips.csv \
#       --out outputs/flip_audit

from __future__ import annotations

import argparse
import csv
import difflib
import random
import re
from pathlib import Path

SEED = 20260805

DEIX = re.compile(
    r"^(이|그|해당|동|본|위|앞|상기)(는|를|에|의|와|과|로|으로|것|러한|와 같|와같)"
    r"|^(이는|이를|이에|이와|이러한|그러나|그런데|따라서|또한|한편|아울러|다만|반면|"
    r"이후|이때|여기서|위와|앞서)"
)

GUIDE = """\
A  정상 치환   — 지시 표현만 선행 문장의 지시대상으로 바뀜. 명제 추가 없음
B  명제 병합   — 선행 문장의 절·명제를 끌어와 판정 대상이 둘 이상이 됨
C  재작성 실패 — 지시어 잔존 / 종결어미·문체만 변경 / 비문
D  사실 추가   — 선행 문장에 없는 내용이 생성됨(환각)
"""


def mark_diff(src: str, dst: str) -> str:
    """Wrap the newly inserted parts of the rewrite in «». This is an aid for faster reading,
        not a basis for the judgment (substitution and insertion cannot be told apart by form).

        The comparison is made by word (eojeol); a character-level comparison would split
        Korean particles and endings and fragment the marks.
    """
    a_tok, b_tok = src.split(), dst.split()
    sm = difflib.SequenceMatcher(None, a_tok, b_tok)
    out: list[str] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        seg = b_tok[j1:j2]
        if not seg:
            continue
        out.append(" ".join(seg) if tag == "equal"
                   else "«" + " ".join(seg) + "»")
    return " ".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flips", default="explicature_flips.csv")
    ap.add_argument("--out", default=".")
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()

    rows = list(csv.DictReader(Path(a.flips).open(encoding="utf-8-sig")))
    if not rows:
        print("[audit] 전환이 없다")
        return

    rnd = random.Random(a.seed)
    rnd.shuffle(rows)  # shuffled, since a block of one direction would reveal it by order
    for n, r in enumerate(rows, 1):
        r["aid"] = f"F{n:03d}"

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    md = ["# explicature 전환 기제 감사\n",
          f"\n총 {len(rows)}건. 묻는 것은 하나입니다 — "
          "**이 판정 변화가 지시 표현 해소 때문인가.**\n",
          "```\n" + GUIDE + "```\n",
          "`«»` 는 복원문에서 새로 들어간 대목입니다. 치환인지 삽입인지는 "
          "형식으로 가릴 수 없으니 표시일 뿐 판정 근거는 아닙니다.\n",
          "\n판정 요령\n",
          "- 복원문에서 «» 부분을 빼도 문장이 성립하고 뜻이 같으면 → B 의심\n",
          "- 원문의 지시어가 복원문에도 그대로 있으면 → C\n",
          "- «» 안의 내용이 앞 문장에서 왔는지 알 수 없고 새 사실로 보이면 → D\n",
          "\n기입은 `flip_audit_entry.csv` 에. A 로 적은 건만 "
          "`ref_project` 열에 Y/N 을 추가로 적으십시오.\n",
          "\n---\n"]

    for r in rows:
        s, e = r["sentence"], r["explicature"]
        ratio = len(e) / max(len(s), 1)
        flags = []
        if ratio >= 1.35:
            flags.append(f"길이 {ratio:.2f}배")
        if DEIX.match(e):
            flags.append("복원 후에도 지시어로 시작")
        if difflib.SequenceMatcher(None, s, e).ratio() >= 0.90:
            flags.append("원문과 거의 동일")
        md.append(f"\n## {r['aid']}  ({r['section']} · `{r['chunk_ids']}`)\n")
        if flags:
            md.append(f"\n> 참고: {' · '.join(flags)}\n")
        md.append(f"\n**원문**\n\n> {s}\n")
        md.append(f"\n**복원**\n\n> {mark_diff(s, e)}\n")

    (outdir / "flip_audit_sheet.md").write_text("\n".join(md), encoding="utf-8")

    with (outdir / "flip_audit_entry.csv").open(
            "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        # section is not included — spreadsheet software converts '3-2' to a date.
        w.writerow(["aid", "mechanism", "ref_project", "note"])
        for r in rows:
            w.writerow([r["aid"], "", "", ""])

    with (outdir / "flip_audit_key.csv").open(
            "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["aid", "direction", "case", "run", "section", "chunk_ids",
                    "len_ratio", "deix_remains"])
        for r in rows:
            s, e = r["sentence"], r["explicature"]
            w.writerow([r["aid"], r["direction"], r["case"], r["run"],
                        r["section"], r["chunk_ids"],
                        f"{len(e)/max(len(s),1):.2f}",
                        "Y" if DEIX.match(e) else "N"])

    print(f"[audit] {len(rows)}건 · "
          f"{outdir/'flip_audit_sheet.md'} / {outdir/'flip_audit_entry.csv'} / "
          f"{outdir/'flip_audit_key.csv'}")


if __name__ == "__main__":
    main()
