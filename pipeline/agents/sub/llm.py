# agents/sub/llm.py — common wrapper for LLM calls that expect a JSON response
# Transport errors (dropped connection, SSL EOF, 503, etc.) are handled separately from
# response format errors. On a transport error the same call is sent again after a wait
# (LLM_TRANSPORT_ATTEMPTS); when the attempts are used up, TransportError is raised and the
# run-level retry in run_batch takes over. A failure unrelated to the content of the judgment
# must not turn into a missing input in the output.
# ─────────────────────────────────────────────────────────────────────────────
# The special-purpose ruling, the base-period decision, place-name conversion, and the Critic
# all call the model through this wrapper. A response format error is unrelated to the content
# of the judgment, so the model is called again with the same input.
# The raw responses and errors of every attempt are returned and kept in the run record.
# Generation settings (temperature, etc.) are not specified (defaults).
# ─────────────────────────────────────────────────────────────────────────────
import json
import os
import re
import time
from collections import Counter

from dotenv import load_dotenv
from google import genai

from paths import GEN_MODEL, LLM_MAX_ATTEMPTS, LLM_TRANSPORT_ATTEMPTS, LLM_TRANSPORT_BACKOFF

load_dotenv()
_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


# Record of the model that actually responded. If only the requested model name were recorded,
# the metadata would show that name even when another model answered. The model_version of
# each response is counted, and the whole batch stops if it differs from the requested model
# (SystemExit is not caught by the retry except-clause in run_batch).
_SERVED: Counter = Counter()


def reset_served() -> None:
    _SERVED.clear()


def served_models() -> dict:
    return dict(_SERVED)


def _check_served(resp) -> None:
    mv = (getattr(resp, "model_version", None) or "").removeprefix("models/")
    _SERVED[mv or "(응답에 표기 없음)"] += 1
    if mv and not mv.startswith(GEN_MODEL):
        raise SystemExit(f"[llm] 요청 모델 {GEN_MODEL} 과 응답 모델 {mv} 이 다름 — "
                         f"배치 중단(자동 라우팅 의심). 산출물을 쓰지 말고 확인할 것.")


class TransportError(RuntimeError):
    """A transport-layer failure that remains after retries. Handled by the run-level retry."""


_TRANSPORT_NAMES = {
    "RemoteProtocolError", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "WriteTimeout", "PoolTimeout", "ReadError", "WriteError", "NetworkError",
    "ServerError", "ServiceUnavailable", "SSLError", "TimeoutError",
}


def _is_transport(e: Exception) -> bool:
    """Decide whether an error is a temporary transport or server-side error, as opposed to a problem with the response content (format).
        Covers google-genai ServerError (5xx), httpx connection errors, and 429 (too many requests).
    """
    name = type(e).__name__
    if name in _TRANSPORT_NAMES:
        return True
    mod = type(e).__module__ or ""
    if mod.startswith(("httpx", "httpcore", "ssl")):
        return True
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code in (429, 500, 502, 503, 504)


def generate_text(contents: str, system: str | None = None) -> str:
    config = {"system_instruction": system} if system else None
    kw = {"model": GEN_MODEL, "contents": contents}
    if config:
        kw["config"] = config
    errs = []
    for att in range(1, LLM_TRANSPORT_ATTEMPTS + 1):
        try:
            resp = _client.models.generate_content(**kw)
            _check_served(resp)
            return (resp.text or "").strip()
        except Exception as e:                      # noqa: BLE001
            if not _is_transport(e):
                raise
            errs.append(f"{type(e).__name__}: {e}")
            if att < LLM_TRANSPORT_ATTEMPTS:
                wait = LLM_TRANSPORT_BACKOFF * (2 ** (att - 1))
                print(f"[llm] 전송 오류({type(e).__name__}) — {wait:.0f}초 후 재호출 "
                      f"({att}/{LLM_TRANSPORT_ATTEMPTS})")
                time.sleep(wait)
    raise TransportError(" | ".join(errs))


def call_json(contents: str, system: str | None = None,
              validate=None, max_attempts: int = LLM_MAX_ATTEMPTS):
    """Call that expects a JSON object. On a parsing or validation failure, call again with the same input.

        validate: obj -> str|None (None means pass; a string is the reason for failure)
        Returns: (obj | None, log)
          log = {"attempts": n, "errors": [...], "raw": [raw responses...]}
    """
    log = {"attempts": 0, "errors": [], "raw": []}
    for _ in range(max_attempts):
        log["attempts"] += 1
        try:
            text = generate_text(contents, system)
        except TransportError:
            raise  # handed over to the run-level retry
        except Exception as e:  # errors other than transport, e.g. a rejected request
            log["errors"].append(f"{type(e).__name__}: {e}")
            continue
        log["raw"].append(text)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            log["errors"].append("JSON 객체 없음")
            continue
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError as e:
            log["errors"].append(f"JSONDecodeError: {e}")
            continue
        if validate:
            why = validate(obj)
            if why:
                log["errors"].append(f"검증 실패: {why}")
                continue
        return obj, log
    return None, log
