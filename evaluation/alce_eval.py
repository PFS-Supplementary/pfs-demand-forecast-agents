# alce_eval.py — citation recall / precision for sentences that cite the guideline
#
# What is measured
# ─────────────────────────────────────────────────────────────────────────────
# Citation recall / precision of the sentences that cite the guideline, as defined in
# section 3.3 of Gao et al. (2023, ALCE).
#   recall    : among sentences with a citation, the share that the cited chunks together support.
#   precision : among the citations given, the share that contribute to supporting the
#               sentence (are not irrelevant).
#               The original paper sets recall = 1 as a prerequisite for precision = 1.
#               Without that prerequisite, a sentence with a single citation would get
#               precision 1 even when the citation does not support it at all.
#
# Design
# ─────────────────────────────────────────────────────────────────────────────
# (1) Marker parsing is done in eval_common, so the three evaluators use the same parser.
# (2) Headings are identified by form (the '[소제목]' block written by the Writer), not by content.
# (3) There is no lexical fallback. When a judge call fails, the pair is counted and
#     reported as judge_error, not replaced by a word-overlap decision.
# (4) The judge is separate from the generator. Panickssery et al. (2024, NeurIPS) showed
#     that LLM evaluators favor their own outputs, in a task of preference over summary
#     quality. The judge is a replaceable interface, and a warning is printed when it is of
#     the same family as the generation model.
#
# Sentence classes and the denominator
# ─────────────────────────────────────────────────────────────────────────────
# recall denominator = sentences citing the guideline + unmarked sentences making a factual claim.
# A filter decides whether an unmarked sentence is a factual claim. Headings and result
# blocks are already removed by form, so the filter in practice handles connective
# sentences. ALCE uses no such filter and puts every sentence in the denominator, but its
# setting is short QA answers with few connective sentences. Report prose is different, so
# the filter is kept and the excluded sentences are reported.
#
# Sentences that carry both a guideline id and a calculation/input marker are counted
# separately. Their figures come from the database or the calculation log, so the guideline
# chunks alone may not entail the whole sentence, yet removing them would drop sentences
# that do cite the guideline. Both the combined value and the value for pure sentences are
# reported.
#
# Explicature
# ─────────────────────────────────────────────────────────────────────────────
# Rashkin et al. (2023) prescribe that a sentence which cannot be interpreted without its
# context be rewritten as an explicature (a standalone sentence with the context restored)
# before attribution is judged. Judging sentences in isolation underestimates recall for
# context-dependent sentences.
# The rewriter is given only the preceding sentences and never the guideline chunks: with
# the chunks it could pull the evidence into the sentence, which would make the judgment
# easier and inflate recall.
# The rewriter must be of a different family from the judge. It is off by default. When on,
# the original sentence and its explicature are kept in the output.
#
# Usage:
#   python alce_eval.py --runs outputs/runs --out outputs/eval
#   python alce_eval.py --runs outputs/runs --out outputs/eval --explicature
#   python alce_eval.py --selftest          # checks parsing and aggregation without a judge

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import eval_common as E

CHROMA_PATH = Path(__file__).resolve().parent.parent / "data" / "chroma"
CHROMA_COLLECTION = "kdi_guidelines"

# Family of the generation model. A warning is printed if the judge is of the same family.
GENERATOR_FAMILY = "gemini"


# ─────────────────────────────────────────────────────────────────────────────
# 1) Chunk texts
# ─────────────────────────────────────────────────────────────────────────────

_chroma_cache: dict | None = None


def _get_chroma():
    global _chroma_cache
    if _chroma_cache is None:
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        _chroma_cache = client.get_collection(CHROMA_COLLECTION)
    return _chroma_cache


class ChunkStore:
    """chunk_id -> text. ChromaDB first, then an external map json.

        A failure to obtain a chunk is counted separately from a judgment failure: if an
        unreadable chunk became 'not supported', recall would measure database access rather
        than citation quality.
    """

    def __init__(self, chunk_map_path: str | None = None):
        self._cache: dict[str, str] = {}
        self._map: dict[str, str] = {}
        self.n_missing = 0
        self.missing_ids: set[str] = set()
        if chunk_map_path and Path(chunk_map_path).exists():
            loaded = json.loads(Path(chunk_map_path).read_text(encoding="utf-8"))
            # chunks_dump.json (list form) is also accepted
            if isinstance(loaded, list):
                loaded = {r["chunk_id"]: r["text"] for r in loaded}
            self._map = loaded

    def get(self, chunk_id: str) -> str:
        if chunk_id in self._cache:
            return self._cache[chunk_id]
        text = ""
        if self._map:
            text = self._map.get(chunk_id, "")
        if not text:
            try:
                got = _get_chroma().get(ids=[chunk_id], include=["documents"])
                docs = got.get("documents") or []
                text = docs[0] if docs else ""
            except Exception as e:                       # noqa: BLE001
                print(f"[alce_eval] 청크 조달 실패 {chunk_id}: {e}")
                text = ""
        if not text:
            self.n_missing += 1
            self.missing_ids.add(chunk_id)
        self._cache[chunk_id] = text
        return text


# ─────────────────────────────────────────────────────────────────────────────
# 2) Judge
# ─────────────────────────────────────────────────────────────────────────────
# Interface: supports(premise, hypothesis) -> bool | None
# None = the judgment failed (call error). No substitute decision is made.

class JudgeError(Exception):
    pass


# The judge prompt is a constant. If the prompt changed together with the judge, a change in
# the figures could not be attributed to the model or to the prompt.
_JUDGE_PROMPT = (
    "아래 '근거 텍스트'가 '문장'의 내용을 뒷받침(함의)하는지 판정하시오.\n"
    "문장의 핵심 주장이 근거 텍스트로부터 직접 확인되면 '예', "
    "근거 텍스트에 없거나 모순되면 '아니오'.\n\n"
    "## 근거 텍스트\n{premise}\n\n"
    "## 문장\n{hypothesis}\n\n"
    'JSON 으로만 답하시오: {{"supports": true 또는 false}}'
)


class GeminiJudge:
    def __init__(self, model: str):
        from dotenv import load_dotenv
        from google import genai
        load_dotenv()
        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self._model = model
        self.family = "gemini"

    def supports(self, premise: str, hypothesis: str) -> bool | None:
        prompt = _JUDGE_PROMPT.format(premise=premise[:6000],
                                      hypothesis=hypothesis)
        try:
            r = self._client.models.generate_content(
                model=self._model, contents=prompt,
                config={"temperature": 0})
            m = re.search(r"\{.*\}", r.text, re.DOTALL)
            if not m:
                return None
            return bool(json.loads(m.group()).get("supports", False))
        except Exception as e:                            # noqa: BLE001
            print(f"[alce_eval] 판정기 오류: {e}")
            return None


class OpenAICompatJudge:
    """OpenAI and OpenAI-compatible endpoints.

        The endpoint and the model name are arguments, so the judge can be replaced.

        The temperature is fixed at 0. If the judge gave different answers across runs, recall
        and precision would measure the variation of the scorer rather than of the pipeline.
        For models that do not accept a temperature argument (reasoning models), it is dropped
        automatically and the call is retried.
    """

    def __init__(self, model: str, base_url: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", family: str = "other",
                 reasoning_effort: str | None = None):
        from openai import OpenAI
        from dotenv import load_dotenv
        load_dotenv()
        self._client = OpenAI(api_key=os.environ.get(api_key_env),
                              base_url=base_url)
        self._model = model
        self.family = family
        self._use_temperature = True
        self._use_json_mode = True
        # Reasoning models (gpt-5.x, etc.) take reasoning_effort instead of temperature. A binary
        # entailment decision needs no deep reasoning, and a lower setting gives more consistent
        # decisions and fewer billed reasoning tokens.
        self._reasoning_effort = reasoning_effort

    def supports(self, premise: str, hypothesis: str, ids=None) -> bool | None:
        prompt = _JUDGE_PROMPT.format(premise=premise[:6000],
                                      hypothesis=hypothesis)
        # Transient errors (429, 5xx, timeouts) are retried here, since rate limits are expected
        # when the calls run in parallel.
        transient = ("rate limit", "429", "timeout", "timed out",
                     "500", "502", "503", "504", "overloaded",
                     "connection", "temporarily")
        attempt = 0
        while attempt < 6:
            attempt += 1
            kw: dict = {}
            if self._use_temperature:
                kw["temperature"] = 0
            if self._reasoning_effort:
                kw["reasoning_effort"] = self._reasoning_effort
            if self._use_json_mode:
                kw["response_format"] = {"type": "json_object"}
            try:
                r = self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}], **kw)
                text = (r.choices[0].message.content or "").strip()
                if not text:
                    # A reasoning model that spends its whole output budget on reasoning returns an empty body.
                    if attempt < 3:
                        time.sleep(1.0)
                        continue
                    return None
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if not m:
                    return None
                return bool(json.loads(m.group()).get("supports", False))
            except Exception as e:                        # noqa: BLE001
                msg = str(e).lower()
                # Whether an argument is unsupported is decided from the error message only, not from the
                # current state of the flags. In a parallel run, once one thread turns a flag off, a check
                # on the flag would be false for the other threads that received the same error.
                if "temperature" in msg:
                    if self._use_temperature:
                        self._use_temperature = False
                        print("[alce_eval] temperature 미지원 — 빼고 재시도 "
                              "(판정 결정론은 캐시가 보장한다)")
                    continue
                if "reasoning_effort" in msg:
                    if self._reasoning_effort:
                        self._reasoning_effort = None
                        print("[alce_eval] reasoning_effort 미지원 — 빼고 재시도")
                    continue
                if "response_format" in msg or "json_object" in msg:
                    if self._use_json_mode:
                        self._use_json_mode = False
                        print("[alce_eval] response_format 미지원 — 빼고 재시도")
                    continue
                if any(t in msg for t in transient) and attempt < 6:
                    wait = min(2 ** attempt, 30)
                    time.sleep(wait)
                    continue
                print(f"[alce_eval] 판정기 오류: {e}")
                return None
        return None


def _prompt_hash() -> str:
    """Hash of the judge prompt. Recorded in the cache header to detect a change of prompt."""
    return hashlib.sha1(_JUDGE_PROMPT.encode()).hexdigest()[:12]


class CachedJudge:
    """Disk cache. It serves two purposes.

        (1) Fewer calls — the same (chunk, sentence) pair recurs across the runs, and for a
            sentence with a single citation the 'alone' decision for precision has the same
            arguments as the recall decision, so it is absorbed.
        (2) An archive for reproducibility — hosted API models are retired over time. The
            decisions themselves are kept so that the aggregates can be recomputed from the
            file alone. The chunk id and the sentence text are stored with the hash, since a
            hash alone does not let a reader check what was judged.
    """

    VERSION = 3

    def __init__(self, inner, cache_path: Path, judge_label: str = ""):
        self._inner = inner
        self._path = cache_path
        self._label = judge_label
        self.family = getattr(inner, "family", "unknown")
        self._data: dict[str, dict] = {}
        if cache_path.exists():
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            if raw.get("_version") != self.VERSION:
                print(f"[alce_eval] 캐시 형식이 달라 무시함: {cache_path}")
            elif raw.get("_judge") != judge_label:
                # The cache is not used when the judge differs; otherwise decisions from another judge (or
                # from --estimate) would be reused without any call. The label is also part of the key.
                print(f"[alce_eval] ⚠ 캐시가 다른 판정기의 것이라 무시함 "
                      f"(캐시: {raw.get('_judge')} / 현재: {judge_label})")
            elif raw.get("_prompt") not in (None, _prompt_hash()):
                # The judge prompt is not part of the cache key, so its hash is recorded in the header and a
                # mismatch is detected there. A cache without '_prompt' is treated as made with the current
                # prompt.
                print(f"[alce_eval] ⚠ 캐시가 다른 판정 프롬프트의 것이라 무시함 "
                      f"(캐시: {raw.get('_prompt')} / 현재: {_prompt_hash()})")
            else:
                self._data = raw.get("entries", {})
        self.n_hit = 0
        self.n_call = 0
        self.n_error = 0

    def _key(self, premise: str, hypothesis: str) -> str:
        # The judge label is part of the key — decisions of different judges do not mix even if the cache file is shared.
        return hashlib.sha1(
            f"{self._label}\x00{premise}\x00{hypothesis}".encode()).hexdigest()

    def supports(self, premise: str, hypothesis: str,
                 ids: list[str] | None = None) -> bool | None:
        k = self._key(premise, hypothesis)
        if k in self._data:
            self.n_hit += 1
            return self._data[k]["supports"]
        self.n_call += 1
        v = self._inner.supports(premise, hypothesis, ids)
        if v is None:
            self.n_error += 1
            return None
        self._data[k] = {"supports": v, "hypothesis": hypothesis,
                         "chunk_ids": ids or [],
                         "premise_chars": len(premise)}
        return v

    def flush(self) -> None:
        # Results of the estimate mode are not decisions and are not saved.
        if self.family in ("estimate", "prefetch", "none"):
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(
            {"_version": self.VERSION, "_judge": self._label,
             "_prompt": _prompt_hash(),
             "entries": self._data}, ensure_ascii=False, indent=1),
            encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 3) Explicature rewriter
# ─────────────────────────────────────────────────────────────────────────────

_RE_CONTEXT_DEP = re.compile(
    r"^(이|그|해당|동|본|위|앞|상기)(는|를|에|의|와|과|로|으로|것|러한|와 같|와같)"
    r"|^(이는|이를|이에|이와|이러한|그러나|그런데|따라서|또한|한편|아울러|다만|반면|"
    r"이후|이때|여기서|위와|앞서)"
)


def is_context_dependent(sentence: str) -> bool:
    return bool(_RE_CONTEXT_DEP.match(sentence))


class Explicator:
    """Resolve referring expressions using only the preceding sentences.

        The guideline chunks are never part of the input: with them the rewriter could pull
        the evidence into the sentence, making the judgment easier and inflating recall.
    """

    def __init__(self, judge_like, n_context: int = 3):
        self._m = judge_like
        self._n = n_context
        self.n_converted = 0
        self.n_target = 0  # number of sentences judged context-dependent (whether or not the rewrite succeeded)
        # The mapping from each original sentence to its explicature is kept. A sentence that
        # becomes supported after restoration disappears from the list of unsupported sentences, so
        # the mapping is needed to check how it was restored.
        self.mapping: dict[str, str] = {}

    def build(self, prev: list[str], sentence: str) -> str:
        if not is_context_dependent(sentence):
            return sentence
        self.n_target += 1
        ctx = "\n".join(prev[-self._n:])
        prompt = (
            "아래 '대상 문장'은 앞 문맥의 지시 표현('이', '해당' 등)을 포함해 "
            "그 자체로는 무엇을 가리키는지 알 수 없습니다. 선행 문장만 이용해 "
            "지시 표현을 풀어 쓴 한 문장으로 바꾸시오.\n"
            "- 선행 문장에 없는 사실·수치를 새로 추가하지 마시오.\n"
            "- 문장의 주장 내용을 바꾸지 마시오.\n"
            "- 결과 문장 하나만 출력하시오.\n\n"
            f"## 선행 문장\n{ctx if ctx else '(없음)'}\n\n"
            f"## 대상 문장\n{sentence}"
        )
        try:
            out = self._m.rewrite(prompt)
        except Exception:                                 # noqa: BLE001
            return sentence
        if out and len(out) < len(sentence) * 4:
            self.n_converted += 1
            self.mapping[sentence] = out
            return out.strip()
        return sentence


class GeminiRewriter:
    """Explicature rewriter.

        Two settings are fixed.
          (1) Thinking is turned off. Gemini 2.5 models have it on by default, and resolving
              referring expressions is a mechanical task.
          (2) The temperature is 0. The rewrite is the input to the judgment, so if it
              varied, the comparison with and without explicature would not be reproducible.

        Some models do not accept thinking_config; on failure it is dropped and the call is retried.
    """

    def __init__(self, model: str):
        from dotenv import load_dotenv
        from google import genai
        load_dotenv()
        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self._model = model
        self.family = "gemini"
        self._use_thinking_cfg = True

    def _config(self) -> dict:
        cfg: dict = {"temperature": 0}
        if self._use_thinking_cfg:
            cfg["thinking_config"] = {"thinking_budget": 0}
        return cfg

    def rewrite(self, prompt: str) -> str:
        try:
            r = self._client.models.generate_content(
                model=self._model, contents=prompt, config=self._config())
        except Exception as e:                            # noqa: BLE001
            msg = str(e)
            if self._use_thinking_cfg and "thinking" in msg.lower():
                # This model does not accept thinking_config — drop it and retry.
                self._use_thinking_cfg = False
                r = self._client.models.generate_content(
                    model=self._model, contents=prompt, config=self._config())
            else:
                raise
        return (r.text or "").strip()


class _StubRewriter:
    """For the estimate mode. Returns the original sentence without a call."""

    family = "estimate"

    def rewrite(self, prompt: str) -> str:
        return ""


class CachedRewriter:
    """Cache of explicature rewrites.

        The same sentence recurs across the runs, so the cache avoids repeated calls and lets
        an interrupted run resume. Like the judgment cache, it also serves as an archive for
        reproducibility.
    """

    def __init__(self, inner, path: Path, label: str = ""):
        self._inner = inner
        self._path = path
        self._label = label
        self.family = getattr(inner, "family", "unknown")
        self._data: dict[str, str] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("_model") == label:
                self._data = raw.get("entries", {})
            else:
                print(f"[alce_eval] ⚠ 재작성 캐시가 다른 모델의 것이라 무시함 "
                      f"(캐시: {raw.get('_model')} / 현재: {label})")
        self.n_hit = 0
        self.n_call = 0

    def rewrite(self, prompt: str) -> str:
        k = hashlib.sha1(f"{self._label}\x00{prompt}".encode()).hexdigest()
        if k in self._data:
            self.n_hit += 1
            return self._data[k]
        self.n_call += 1
        out = self._inner.rewrite(prompt)
        if out:
            self._data[k] = out
        return out

    def flush(self) -> None:
        if self.family in ("estimate", "none"):
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(
            {"_model": self._label, "entries": self._data},
            ensure_ascii=False, indent=1), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 4) Sentence classes
# ─────────────────────────────────────────────────────────────────────────────

CLS_GUIDELINE_PURE = "guideline_pure"  # guideline id only
CLS_GUIDELINE_MIXED = "guideline_mixed"  # guideline id + calculation/input
CLS_NUMERIC = "numeric"  # calculation/input only -> covered by RG-P
CLS_DATA = "data"  # data source or guideline table reference only
CLS_UNCITED_CLAIM = "uncited_claim"  # unmarked factual claim -> recall miss
CLS_UNCITED_OTHER = "uncited_other"  # unmarked connective sentence -> outside the denominator

# Approximate test of whether an unmarked sentence is a factual claim that needs a citation.
_FACTUAL_KEYWORDS = ("추정", "정의", "산출", "방법", "지침", "기준",
                     "설정", "구분", "의미", "포함", "활용")


def looks_factual(clean: str) -> bool:
    if re.search(r"\d", clean):
        return True
    return any(k in clean for k in _FACTUAL_KEYWORDS)


def classify_sentence(sentence: str) -> tuple[str, dict]:
    mk = E.parse_markers(sentence)
    g = mk[E.MARKER_GUIDELINE]
    numeric = mk[E.MARKER_CALC] or mk[E.MARKER_INPUT]
    if g and numeric:
        return CLS_GUIDELINE_MIXED, mk
    if g:
        return CLS_GUIDELINE_PURE, mk
    if numeric:
        return CLS_NUMERIC, mk
    if mk[E.MARKER_DATA] or mk[E.MARKER_GUIDELINE_TABLE]:
        return CLS_DATA, mk
    clean = E.strip_markers(sentence)
    return (CLS_UNCITED_CLAIM if looks_factual(clean) else CLS_UNCITED_OTHER), mk


# ─────────────────────────────────────────────────────────────────────────────
# 5) Scoring one subsection
# ─────────────────────────────────────────────────────────────────────────────

def score_section(sid: str, sec: dict, judge, store: ChunkStore,
                  explicator: Explicator | None = None) -> dict:
    sents = E.body_sentences(sec.get("text") or "")
    cls_count = Counter()
    items: list[dict] = []
    n_judge_error = 0
    n_no_premise = 0
    prev: list[str] = []

    for s in sents:
        cls, mk = classify_sentence(s)
        cls_count[cls] += 1
        clean = E.strip_markers(s)

        if cls in (CLS_GUIDELINE_PURE, CLS_GUIDELINE_MIXED):
            hyp = explicator.build(prev, clean) if explicator else clean
            ids = mk[E.MARKER_GUIDELINE]
            premises = {c: store.get(c) for c in ids}
            joined = "\n\n".join(premises[c] for c in ids if premises[c])
            # Two kinds of failure are kept apart. A sentence that could not be judged because a chunk
            # could not be read is a matter of corpus access, not of citation quality; mixing the two
            # would make recall measure the state of the database. Judge call failures are likewise
            # counted separately.
            if not joined:
                n_no_premise += 1
                supported = None
                reason = "no_premise"
            else:
                supported = judge.supports(joined, hyp, ids)
                if supported is None:
                    n_judge_error += 1
                    reason = "judge_error"
                else:
                    reason = None
            items.append({
                "section": sid, "cls": cls, "ids": ids,
                "sentence": clean,
                "explicature": hyp if hyp != clean else None,
                "supported": supported, "failure": reason,
                "citations": _score_citations(ids, premises, hyp, judge,
                                              bool(supported)),
            })
        elif cls == CLS_UNCITED_CLAIM:
            items.append({"section": sid, "cls": cls, "ids": [],
                          "sentence": clean, "explicature": None,
                          "supported": False, "failure": None,
                          "citations": []})
        prev.append(clean)

    return {"classes": dict(cls_count), "items": items,
            "n_judge_error": n_judge_error, "n_no_premise": n_no_premise,
            "n_sentences": len(sents)}


def _score_citations(ids: list[str], premises: dict, hyp: str, judge,
                     sentence_supported: bool) -> list[dict]:
    """ALCE precision — with recall = 1 as a prerequisite.

        If the sentence is not supported, all of its citations enter the denominator with a
        score of 0. With a single citation, the remaining set is empty and condition (b) does
        not hold, so the citation is not 'irrelevant' (definition of the original paper).
    """
    out = []
    for cid in ids:
        if not sentence_supported:
            out.append({"id": cid, "relevant": False, "reason": "recall<1"})
            continue
        alone = judge.supports(premises.get(cid, ""), hyp, [cid]) \
            if premises.get(cid) else False
        others = "\n\n".join(premises[c] for c in ids
                             if c != cid and premises.get(c))
        rest = judge.supports(others, hyp,
                              [c for c in ids if c != cid]) if others else False
        irrelevant = (alone is False) and (rest is True)
        out.append({"id": cid, "relevant": not irrelevant,
                    "reason": "irrelevant" if irrelevant else "relevant"})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 6) Aggregation
# ─────────────────────────────────────────────────────────────────────────────

def _mean_sd(vals: list[float]) -> dict:
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0, "mean": None, "sd": None}
    if len(v) == 1:
        return {"n": 1, "mean": v[0], "sd": None}
    return {"n": len(v), "mean": statistics.fmean(v), "sd": statistics.stdev(v)}


def _rp(items: list[dict], include_mixed: bool) -> dict:
    sel = [i for i in items
           if i["cls"] != CLS_GUIDELINE_MIXED or include_mixed]
    denom = [i for i in sel if i["cls"] in
             (CLS_GUIDELINE_PURE, CLS_GUIDELINE_MIXED, CLS_UNCITED_CLAIM)]
    hits = [i for i in denom if i["supported"] is True]
    cites = [c for i in sel for c in i["citations"]]
    rel = [c for c in cites if c["relevant"]]
    return {
        "recall_num": len(hits), "recall_den": len(denom),
        "recall": (len(hits) / len(denom)) if denom else None,
        "precision_num": len(rel), "precision_den": len(cites),
        "precision": (len(rel) / len(cites)) if cites else None,
    }


def aggregate(per_run: list[dict]) -> dict:
    all_items: list[dict] = []
    cls_total = Counter()
    ver_cite = Counter()
    ver_sent = defaultdict(lambda: {"cited": 0, "supported": 0})
    rec_rates, pre_rates = [], []
    n_judge_error = 0
    n_no_premise = 0
    unsupported: list[dict] = []

    for r in per_run:
        run_items = [it for sc in r["sections"].values() for it in sc["items"]]
        all_items.extend(run_items)
        for sc in r["sections"].values():
            cls_total.update(sc["classes"])
            n_judge_error += sc["n_judge_error"]
            n_no_premise += sc.get("n_no_premise", 0)
        m = _rp(run_items, include_mixed=True)
        rec_rates.append(m["recall"])
        pre_rates.append(m["precision"])
        for it in run_items:
            for cid in it["ids"]:
                v = E.guideline_version(cid)
                ver_cite[v] += 1
            for v in {E.guideline_version(c) for c in it["ids"]}:
                ver_sent[v]["cited"] += 1
                if it["supported"] is True:
                    ver_sent[v]["supported"] += 1
            if it["supported"] is not True and it["cls"] != CLS_UNCITED_CLAIM:
                # explicature and failure are carried with each unsupported sentence. To compare the
                # conditions with and without explicature, the list must show what each sentence was
                # restored to. failure separates no_premise (missing chunk) and judge_error from problems of
                # citation quality.
                unsupported.append({**{k: it[k] for k in
                                       ("section", "cls", "ids", "sentence",
                                        "explicature", "failure")},
                                    "run": r["run"]})

    with_mixed = _rp(all_items, include_mixed=True)
    pure_only = _rp(all_items, include_mixed=False)
    return {
        "n_runs": len(per_run),
        "with_mixed": {**with_mixed,
                       "recall_per_run": _mean_sd(rec_rates),
                       "precision_per_run": _mean_sd(pre_rates)},
        "pure_only": pure_only,
        "sentence_classes": dict(cls_total),
        "citations_by_version": dict(ver_cite),
        "sentences_by_version": {k: dict(v) for k, v in ver_sent.items()},
        "n_judge_error": n_judge_error,
        "n_no_premise": n_no_premise,
        "unsupported": unsupported,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7) Report
# ─────────────────────────────────────────────────────────────────────────────

def _pct(x, d=1):
    return "—" if x is None else f"{x*100:.{d}f}%"


def _ms(d: dict) -> str:
    """Format as mean (sd). With a single run the sd is undefined."""
    if d.get("mean") is None:
        return "—"
    sd = "—" if d.get("sd") is None else f"{d['sd']:.3f}"
    return f"{d['mean']:.3f} ({sd})"


def write_summary(agg: dict, out_path: Path, meta: dict) -> None:
    L = ["# 지침 인용 recall / precision (ALCE)\n"]
    if str(meta.get("judge", "")).startswith("estimate"):
        L.append("\n> ⚠ **이 결과는 추정(--estimate) 모드 산출물이다.** 판정기가 "
                 "모든 문장을 '뒷받침됨'으로 반환하므로 recall·precision 은 "
                 "실제 판정 결과가 아니다. 호출 규모 확인용으로만 쓸 것.\n")
    L.append(f"\n판정기: `{meta['judge']}` · explicature: "
             f"{'적용' if meta['explicature'] else '미적용'}"
             f" · 청크 조달 실패 {meta['chunk_missing']}건\n")
    if meta.get("same_family"):
        L.append("\n> ⚠ 판정기가 생성 모델과 같은 계열이다. 자기선호 편향 가능성이 "
                 "있어 이 수치는 잠정값이다.\n")

    L.append("\n## recall / precision\n")
    L.append("| 케이스 | recall | precision | recall mean (sd) | "
             "precision mean (sd) | 혼합문장 제외 recall |")
    L.append("|---|---|---|---|---|---|")
    for case, a in agg.items():
        w, p = a["with_mixed"], a["pure_only"]
        L.append(
            f"| {case} "
            f"| {w['recall_num']}/{w['recall_den']} ({_pct(w['recall'])}) "
            f"| {w['precision_num']}/{w['precision_den']} ({_pct(w['precision'])}) "
            f"| {_ms(w['recall_per_run'])} "
            f"| {_ms(w['precision_per_run'])} "
            f"| {p['recall_num']}/{p['recall_den']} ({_pct(p['recall'])}) |")

    L.append("\n## 문장 분류\n")
    keys = [CLS_GUIDELINE_PURE, CLS_GUIDELINE_MIXED, CLS_NUMERIC, CLS_DATA,
            CLS_UNCITED_CLAIM, CLS_UNCITED_OTHER]
    L.append("| 케이스 | " + " | ".join(keys) + " |")
    L.append("|" + "---|" * (len(keys) + 1))
    for case, a in agg.items():
        c = a["sentence_classes"]
        L.append(f"| {case} | " + " | ".join(str(c.get(k, 0)) for k in keys) + " |")

    L.append("\n## 지침 버전별 인용\n")
    L.append("자매 지침(202105 문화·관광) 인용이 실제로 몇 건 발생하고 그중 몇 건이 "
             "뒷받침되는지. 중력모형 미적용 사유가 근거와 함께 드러났다는 주장의 증거.\n")
    L.append("| 케이스 | 버전 | 인용 건수 | 문장 수 | 뒷받침 |")
    L.append("|---|---|---|---|---|")
    for case, a in agg.items():
        for v in sorted(a["citations_by_version"]):
            s = a["sentences_by_version"].get(v, {})
            L.append(f"| {case} | {v} | {a['citations_by_version'][v]} | "
                     f"{s.get('cited', 0)} | {s.get('supported', 0)} |")

    L.append("\n## 판정 불가\n")
    L.append("원인이 다른 두 실패를 분리해 센다. 청크 조달 실패는 코퍼스 접근 문제이지 "
             "인용 품질 문제가 아니므로 합치면 recall 이 DB 상태를 재게 된다.\n")
    L.append("| 케이스 | 청크 조달 실패 | 판정기 오류 |")
    L.append("|---|---|---|")
    for case, a in agg.items():
        L.append(f"| {case} | {a.get('n_no_premise', 0)} | {a['n_judge_error']} |")
    L.append("\n종전 구현은 판정기 오류 시 '단어 40% 겹치면 뒷받침됨'으로 후퇴했고 "
             "그 사실이 어디에도 남지 않았다. 지금은 대체 판정을 하지 않는다.\n")

    L.append("\n## 미뒷받침 문장\n")
    any_us = False
    for case, a in agg.items():
        for u in a["unsupported"][:40]:
            any_us = True
            L.append(f"- **{case} r{u['run']} [{u['section']}]** {u['ids']}\n"
                     f"  - {u['sentence'][:160]}")
    if not any_us:
        L.append("없음.")
    out_path.write_text("\n".join(L), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 8) Entry point
# ─────────────────────────────────────────────────────────────────────────────

class _PrefetchJudge:
    """Collect the (premise, sentence) pairs without calling the judge.

        Scoring runs twice: the first pass collects the pairs to be judged, the cache is
        filled in parallel, and the second pass aggregates. This object always returns True
        so that the precision path (the alone/rest decisions) is also taken, which collects a
        superset of the pairs actually needed.
    """
    family = "prefetch"

    def __init__(self):
        self.pairs: dict[str, tuple[str, str, list]] = {}

    def supports(self, premise: str, hypothesis: str, ids=None):
        k = hashlib.sha1(f"{premise}\x00{hypothesis}".encode()).hexdigest()
        self.pairs.setdefault(k, (premise, hypothesis, ids or []))
        return True


class _PrefetchRewriter:
    """Collect the rewrite prompts without calling the model.

        Rewrites are collected first and processed in parallel, in the same way as the
        judgments. An empty string is returned, so the Explicator uses the original sentence
        and the pairs of this pass are discarded; the purpose is to fill the rewrite cache.
    """

    family = "prefetch"

    def __init__(self):
        self.prompts: dict[str, str] = {}

    def rewrite(self, prompt: str) -> str:
        self.prompts.setdefault(
            hashlib.sha1(prompt.encode()).hexdigest(), prompt)
        return ""


def prefill_rewrites(cached: "CachedRewriter", inner, prompts: dict,
                     workers: int) -> None:
    """Rewrite the collected prompts in parallel and fill the cache."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    remapped = {}
    for pr in prompts.values():
        k = hashlib.sha1(f"{cached._label}\x00{pr}".encode()).hexdigest()
        remapped[k] = pr
    todo = [(k, v) for k, v in remapped.items() if k not in cached._data]
    if not todo:
        print("[alce_eval] 사전 재작성: 캐시에 모두 있음")
        return
    print(f"[alce_eval] 사전 재작성 {len(todo)}건 · 동시 실행 {workers} "
          f"(모델 {cached._label})")

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(inner.rewrite, pr): k for k, pr in todo}
        for f in as_completed(futs):
            k = futs[f]
            try:
                out = f.result()
            except Exception as e:                          # noqa: BLE001
                print(f"[alce_eval] 재작성 오류: {e}")
                out = ""
            if out:
                cached._data[k] = out
            done += 1
            if done % 100 == 0 or done == len(todo):
                el = time.time() - t0
                rate = done / el if el else 0
                left = (len(todo) - done) / rate if rate else 0
                print(f"  재작성 {done}/{len(todo)} "
                      f"({el/60:.1f}분 경과 · 남은 예상 {left/60:.1f}분)")
                cached.flush()  # intermediate save — the run can resume if interrupted
    cached.flush()
    print(f"[alce_eval] 사전 재작성 완료 {done}건 ({(time.time()-t0)/60:.1f}분)")


def prefill_cache(judge: "CachedJudge", inner, pairs: dict,
                  workers: int) -> None:
    """Judge the collected pairs in parallel and fill the cache.

        Pairs already in the cache are skipped. Failed judgments are not cached, so they
        appear as judge_error in the second pass.
    """
    # The pairs were hashed during collection without the judge label, so they are mapped again here.
    remapped = {judge._key(prem, hyp): (prem, hyp, ids)
                for prem, hyp, ids in pairs.values()}
    todo = [(k, v) for k, v in remapped.items() if k not in judge._data]
    if not todo:
        print("[alce_eval] 사전 판정: 캐시에 모두 있음")
        return
    print(f"[alce_eval] 판정기: {judge._label}")

    # Warm-up — the first pair is run sequentially to settle which arguments the model accepts
    # (temperature, reasoning_effort, response_format) before the threads start.
    k0, (p0, h0, i0) = todo[0]
    v0 = inner.supports(p0, h0, i0)
    if v0 is not None:
        judge._data[k0] = {"supports": v0, "hypothesis": h0,
                           "chunk_ids": i0, "premise_chars": len(p0)}
        todo = todo[1:]
    else:
        print("[alce_eval] ⚠ 워밍업 판정 실패 — 모델명·API 키·요금 한도를 확인할 것")
        return

    print(f"[alce_eval] 사전 판정 {len(todo)}건 · 동시 실행 {workers}")
    lock = threading.Lock()
    done = [0]

    def one(item):
        k, (prem, hyp, ids) = item
        v = inner.supports(prem, hyp, ids)
        with lock:
            done[0] += 1
            if v is not None:
                judge._data[k] = {"supports": v, "hypothesis": hyp,
                                  "chunk_ids": ids,
                                  "premise_chars": len(prem)}
            if done[0] % 200 == 0:
                print(f"    {done[0]}/{len(todo)} …")
                judge.flush()  # intermediate save — the run can resume if interrupted

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    judge.flush()
    n_fail = sum(1 for k, _ in todo if k not in judge._data)
    print(f"[alce_eval] 사전 판정 완료 · 캐시 {len(judge._data)}건"
          + (f" · 실패 {n_fail}건(2차 패스에서 순차 재시도)" if n_fail else ""))
    if n_fail > len(todo) * 0.05:
        print("[alce_eval] ⚠ 실패율이 5%를 넘는다. --workers 를 낮추고 "
              "다시 실행하면 캐시가 이어받는다.")


class _NullJudge:
    """Makes no judgment. For --selftest."""
    family = "none"

    def supports(self, premise: str, hypothesis: str, ids=None):
        return None


class _CountingJudge:
    """Counts the calls without making them. For --estimate.

        Duplicates are removed with the same key as the cache, so the count approximates the
        number of billed calls. For a sentence with a single citation, the 'alone' decision
        for precision has the same arguments as the recall decision and is counted once.
    """
    family = "estimate"

    def __init__(self):
        self.keys: set[str] = set()
        self.n_request = 0
        self.total_chars = 0

    def supports(self, premise: str, hypothesis: str, ids=None):
        self.n_request += 1
        k = hashlib.sha1(f"{premise}\x00{hypothesis}".encode()).hexdigest()
        if k not in self.keys:
            self.keys.add(k)
            self.total_chars += len(premise[:6000]) + len(hypothesis)
        return True  # assumed supported, so that the precision path is also taken


def build_judge(kind: str, model: str, base_url: str | None,
                reasoning_effort: str | None = None):
    if kind == "none":
        return _NullJudge()
    if kind == "estimate":
        return _CountingJudge()
    if kind == "gemini":
        return GeminiJudge(model)
    return OpenAICompatJudge(model, base_url, family=kind,
                             reasoning_effort=reasoning_effort)


def _score_all(paths, judge, store, explicator):
    """Score all outputs and build the list of runs per case."""
    by_case: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        run = E.load_run(p)
        case = E.project_of(run, p)
        rid = E.run_index_of(p)
        sections = {sid: score_section(sid, sec, judge, store, explicator)
                    for sid, sec in E.iter_sections(run)}
        by_case[case].append({"run": rid, "sections": sections})
    return by_case


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="outputs/runs")
    ap.add_argument("--out", default="outputs/eval")
    ap.add_argument("--judge", default="gemini",
                    help="gemini | openai | 그 밖의 OpenAI 호환 계열명 | none")
    ap.add_argument("--judge-model", default="gemini-2.5-flash")
    ap.add_argument("--judge-base-url", default=None)
    ap.add_argument("--chunk-map", default=None,
                    help="chunk_id→본문 json (ChromaDB 없이 실행할 때)")
    ap.add_argument("--explicature", action="store_true")
    ap.add_argument("--explicature-model", default="gemini-2.5-flash")
    ap.add_argument("--cache", default=None, help="판정 캐시 json 경로")
    ap.add_argument("--selftest", action="store_true",
                    help="판정기 없이 파싱·분류·집계만 검증")
    ap.add_argument("--estimate", action="store_true",
                    help="API 호출 없이 필요한 판정 호출 수·프롬프트 분량만 추정")
    ap.add_argument("--workers", type=int, default=8,
                    help="판정 동시 실행 수(사전 판정 단계)")
    ap.add_argument("--reasoning-effort", default="low",
                    help="추론 계열 모델의 추론 강도(low/medium/high). "
                         "미지원 모델이면 자동으로 생략된다")
    args = ap.parse_args()

    kind = "none" if args.selftest else ("estimate" if args.estimate
                                         else args.judge)
    inner = build_judge(kind, args.judge_model, args.judge_base_url,
                        args.reasoning_effort)
    same_family = getattr(inner, "family", "") == GENERATOR_FAMILY
    if same_family:
        print("[alce_eval] ⚠ 판정기가 생성 모델과 같은 계열(gemini)이다. "
              "Panickssery et al.(2024) 의 자기선호 편향 가능성이 있다.")

    cache_path = Path(args.cache) if args.cache else Path(args.out) / "judge_cache.json"
    judge = CachedJudge(inner, cache_path,
                        judge_label=f"{kind}:{args.judge_model}")

    explicator = None
    if args.explicature and kind in ("estimate", "none"):
        # In the estimate mode no rewriting is done; only the number of target sentences is counted.
        explicator = Explicator(_StubRewriter())
        print("[alce_eval] 추정 모드 — explicature 는 대상 건수만 세고 "
              "재작성하지 않는다.")
    elif args.explicature:
        rewriter = CachedRewriter(GeminiRewriter(args.explicature_model),
                                  Path(args.out) / "explicature_cache.json",
                                  label=args.explicature_model)
        if getattr(rewriter, "family", "") == getattr(inner, "family", ""):
            print("[alce_eval] ⚠ explicature 생성기와 판정기가 같은 계열이다. "
                  "두 모델은 서로 다른 계열이어야 한다.")
        explicator = Explicator(rewriter)

    store = ChunkStore(args.chunk_map)
    paths = E.find_runs(args.runs)
    if not paths:
        print(f"[alce_eval] {args.runs} 에서 chapter3_*.json 을 찾지 못함")
        print(E.describe_runs_dir(args.runs))
        return

    # Pass 0 — collect the rewrite prompts and fill the rewrite cache in parallel.
    # Rewriting comes first, because the pairs to be judged are fixed only once the explicatures are.
    if (explicator is not None and kind not in ("none", "estimate")
            and args.workers > 1):
        pre_rw = _PrefetchRewriter()
        _score_all(paths, _PrefetchJudge(), store, Explicator(pre_rw))
        prefill_rewrites(explicator._m, explicator._m._inner,
                         pre_rw.prompts, args.workers)

    # Pass 1 — collect the pairs to be judged and fill the cache in parallel (real judge only)
    if kind not in ("none", "estimate") and args.workers > 1:
        pre = _PrefetchJudge()
        _score_all(paths, pre, store, explicator)
        prefill_cache(judge, inner, pre.pairs, args.workers)

    # Pass 2 — read the cache and aggregate
    by_case = _score_all(paths, judge, store, explicator)
    print(f"[alce_eval] 채점 완료 {len(paths)}개 산출물")

    agg = {c: aggregate(rs) for c, rs in sorted(by_case.items())}
    judge.flush()
    if explicator is not None:
        rw = explicator._m
        if hasattr(rw, "flush"):
            rw.flush()
            print(f"[alce_eval] 재작성 캐시 — 적중 {rw.n_hit} / 호출 {rw.n_call}")
        print(f"[alce_eval] explicature 대상 {explicator.n_target}건 · "
              f"변환 {explicator.n_converted}건")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if explicator is not None and explicator.mapping:
        # All mappings from original sentence to explicature. To check how a resolved sentence was
        # restored, see this file rather than the list of unsupported sentences.
        (out / "explicatures.json").write_text(
            json.dumps({"model": args.explicature_model,
                        "n_converted": explicator.n_converted,
                        "mapping": explicator.mapping},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[alce_eval] explicature 매핑 {len(explicator.mapping)}건 저장: "
              f"{out/'explicatures.json'}")
    meta = {"judge": f"{kind}:{args.judge_model}",
            "explicature": bool(explicator),
            "explicature_converted": explicator.n_converted if explicator else 0,
            "explicature_target": explicator.n_target if explicator else 0,
            "judge_prompt": _prompt_hash(),
            "chunk_missing": store.n_missing,
            "chunk_missing_ids": sorted(store.missing_ids),
            "same_family": same_family,
            "cache_hit": judge.n_hit, "cache_call": judge.n_call,
            "judge_error": judge.n_error}
    (out / "alce_metrics.json").write_text(
        json.dumps({"meta": meta, "aggregate": agg}, ensure_ascii=False,
                   indent=2, default=str), encoding="utf-8")
    write_summary(agg, out / "alce_summary.md", meta)

    if kind == "estimate":
        c = inner
        print(f"[alce_eval] 추정: 판정 요청 {c.n_request}회 · "
              f"중복 제거 후 실호출 {len(c.keys)}회 · "
              f"프롬프트 총 {c.total_chars:,}자 "
              f"(대략 {c.total_chars/2.5:,.0f} 토큰)")
    for case, a in agg.items():
        w = a["with_mixed"]
        print(f"  {case}: recall {w['recall_num']}/{w['recall_den']} "
              f"({_pct(w['recall'])}) · precision {w['precision_num']}/"
              f"{w['precision_den']} ({_pct(w['precision'])}) · "
              f"청크실패 {a.get('n_no_premise', 0)} · "
              f"판정오류 {a['n_judge_error']}")
    print(f"[alce_eval] 캐시 적중 {judge.n_hit} · 호출 {judge.n_call} · "
          f"오류 {judge.n_error} · 청크 조달 실패 {store.n_missing}")
    print(f"[alce_eval] 저장: {out/'alce_metrics.json'} / {out/'alce_summary.md'}")


if __name__ == "__main__":
    main()