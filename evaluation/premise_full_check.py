# premise_full_check.py — sensitivity check on the length of the judge input
#
# What it does
#   The main evaluation (alce_eval.py) gives the judge only the first 6,000 characters of
#   the premise text. This script selects the guideline-citing sentences whose cited chunks
#   together exceed 6,000 characters, judges them again with the same judge and the same
#   prompt on the full premise text, and compares the result with the original decision.
#   The outputs of the main evaluation (outputs/eval) are only read, not changed.
#
# Scope
#   Only the change in citation recall is computed (precision is not recomputed).
#   The explicature condition is not covered.
#
# Run (environment variable OPENAI_API_KEY)
#   python premise_full_check.py --dry-run    # counts the targets, no API call
#   python premise_full_check.py              # full run
#
# Output: outputs/premise_full_check/
#   premise_full_check.csv   original and full-text decisions per target sentence
#   premise_full_check.md    summary of the change in recall by case
#   judge_full_cache.json    cache of the full-text decisions (no repeated calls on rerun)
import argparse, csv, json, re, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOS / "evaluation"))
import eval_common as E                                  # noqa: E402
from alce_eval import (_JUDGE_PROMPT, classify_sentence,  # noqa: E402
                       CLS_GUIDELINE_PURE, CLS_GUIDELINE_MIXED)

LIMIT = 6000
MODEL = "gpt-5.4-mini"
RUNS = HOS / "outputs" / "runs"
CHUNKS = HOS / "chunks_dump.json"
METRICS = HOS / "outputs" / "eval" / "alce_metrics.json"
OUT = HOS / "outputs" / "premise_full_check"


def judge_full(client, premise: str, hypothesis: str) -> bool | None:
    """Same settings as alce_eval.OpenAICompatJudge (JSON mode, temperature not specified).
        The only difference is that the premise is not truncated.
    """
    prompt = _JUDGE_PROMPT.format(premise=premise, hypothesis=hypothesis)
    for att in range(6):
        try:
            r = client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"})
            text = (r.choices[0].message.content or "").strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                continue
            return bool(json.loads(m.group()).get("supports", False))
        except Exception as e:                            # noqa: BLE001
            print(f"  재시도 {att + 1}/6: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(min(60, 5 * 2 ** att))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    chunks = {c["chunk_id"]: c["text"] for c in
              json.loads(CHUNKS.read_text(encoding="utf-8"))}
    agg = json.loads(METRICS.read_text(encoding="utf-8"))["aggregate"]
    unsup = {(case, x["run"], x["section"], x["sentence"])
             for case, v in agg.items() for x in v["unsupported"]}

    targets = []  # guideline-citing sentences (instances) over 6,000 characters
    for f in sorted(RUNS.glob("*/chapter3_*.json")):
        run = json.loads(f.read_text(encoding="utf-8"))
        case = E.project_of(run, f)
        rid = E.run_index_of(f)
        for sid, sec in E.iter_sections(run):
            for s in E.body_sentences(sec.get("text") or ""):
                cls, mk = classify_sentence(s)
                if cls not in (CLS_GUIDELINE_PURE, CLS_GUIDELINE_MIXED):
                    continue
                ids = mk[E.MARKER_GUIDELINE]
                parts = [chunks[i] for i in ids if chunks.get(i)]
                if not parts:
                    continue
                premise = "\n\n".join(parts)
                if len(premise) <= LIMIT:
                    continue
                clean = E.strip_markers(s)
                targets.append({"case": case, "run": rid, "section": sid,
                                "ids": ids, "sentence": clean, "premise": premise,
                                "orig": (case, rid, sid, clean) not in unsup})
    pairs = {(tuple(t["ids"]), t["sentence"]): t["premise"] for t in targets}
    print(f"대상 문장 {len(targets)}건 · 고유 (청크 집합, 문장) 쌍 {len(pairs)}개 "
          f"· 원래 미뒷받침 {sum(not t['orig'] for t in targets)}건")
    if a.dry_run:
        return

    from openai import OpenAI
    from dotenv import load_dotenv
    load_dotenv(HOS / ".env"); load_dotenv()
    client = OpenAI()
    OUT.mkdir(parents=True, exist_ok=True)
    cache_p = OUT / "judge_full_cache.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    keyf = lambda k: json.dumps([list(k[0]), k[1]], ensure_ascii=False)
    todo = [k for k in pairs if keyf(k) not in cache]
    print(f"API 호출 {len(todo)}건")

    def work(k):
        return k, judge_full(client, pairs[k], k[1])
    with ThreadPoolExecutor(a.workers) as ex:
        for n, (k, v) in enumerate(ex.map(work, todo), 1):
            cache[keyf(k)] = v
            if n % 20 == 0:
                cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                print(f"  {n}/{len(todo)}")
    cache_p.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")

    rows, delta, flips, fails = [], Counter(), Counter(), 0
    for t in targets:
        new = cache.get(keyf((tuple(t["ids"]), t["sentence"])))
        if new is None:
            fails += 1
        elif new != t["orig"]:
            delta[t["case"]] += 1 if new else -1
            flips[(t["case"], "미뒷받침→뒷받침" if new else "뒷받침→미뒷받침")] += 1
        rows.append({"case": t["case"], "run": t["run"], "section": t["section"],
                     "ids": "; ".join(t["ids"]), "premise_chars": len(t["premise"]),
                     "orig_supported": t["orig"], "full_supported": new,
                     "sentence": t["sentence"]})
    with (OUT / "premise_full_check.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    L = ["# 근거 텍스트 전문 재판정 (민감도 확인)", "",
         f"판정기 {MODEL} · 프롬프트 동일 · 대상: 인용 청크 합 {LIMIT:,}자 초과 지침 인용 문장",
         f"대상 {len(targets)}건, 고유 쌍 {len(pairs)}개, 판정 실패 {fails}건", "",
         "| 사례 | recall(본 평가) | recall(전문 재판정 반영) | 미뒷받침→뒷받침 | 뒷받침→미뒷받침 |",
         "|---|---|---|---|---|"]
    for case in sorted(agg):
        wm = agg[case]["with_mixed"]
        num, den = wm["recall_num"], wm["recall_den"]
        L.append(f"| {case} | {num/den:.1%} ({num}/{den}) | "
                 f"{(num+delta[case])/den:.1%} ({num+delta[case]}/{den}) | "
                 f"{flips[(case,'미뒷받침→뒷받침')]} | {flips[(case,'뒷받침→미뒷받침')]} |")
    tot_u = sum(len(v["unsupported"]) for v in agg.values())
    L += ["", f"미뒷받침 문장: 본 평가 {tot_u}건 → 전문 재판정 반영 {tot_u - sum(delta.values())}건",
          "", "precision 은 다시 계산하지 않았다."]
    (OUT / "premise_full_check.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
