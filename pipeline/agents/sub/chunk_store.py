# agents/sub/chunk_store.py
# ─────────────────────────────────────────────────────────────────────────────
# Guideline chunk store. The pipeline only fetches chunks directly by identifier (no semantic search).
#
# Fetching by identifier does not need vector database features, so a JSON dump of the chunks
# (HOS/chunks_dump.json: the 20 chunks of the ChromaDB collection `kdi_guidelines`) can be
# read in the same way when ChromaDB cannot be loaded.
#
# Backend selection: environment variable HOS_CHUNK_BACKEND = chroma | json | auto (default)
# auto = ChromaDB if it can be opened, otherwise JSON. The backend used and a hash of each
# fetched chunk text are recorded in the run metadata, so that runs on either backend can be
# checked afterwards to have had the same input.
# ─────────────────────────────────────────────────────────────────────────────
import hashlib
import json
import os

from paths import CHROMA_PATH, HOS_ROOT

CHUNK_JSON = HOS_ROOT / "chunks_dump.json"
_BACKEND = os.environ.get("HOS_CHUNK_BACKEND", "auto").lower()

_state = {"backend": None, "error": None, "coll": None, "json": None}
_served: dict[str, str] = {}  # chunk id -> text hash, for the chunks fetched in this process


def _open():
    if _state["backend"]:
        return
    if _BACKEND in ("chroma", "auto"):
        try:
            import chromadb
            _state["coll"] = chromadb.PersistentClient(
                path=str(CHROMA_PATH)).get_collection("kdi_guidelines")
            _state["backend"] = "chroma"
            return
        except Exception as e:  # includes a blocked native module (ImportError)
            _state["error"] = f"{type(e).__name__}: {e}"
            if _BACKEND == "chroma":
                raise
    rows = json.loads(CHUNK_JSON.read_text(encoding="utf-8"))
    _state["json"] = {r["chunk_id"]: r for r in rows}
    _state["backend"] = "json"
    msg = f"[ChunkStore] JSON 백엔드 사용: {CHUNK_JSON.name} ({len(rows)}청크)"
    if _state["error"]:
        msg += f" — ChromaDB 열기 실패: {_state['error'][:120]}"
    print(msg)


def get_chunks(ids: list[str]) -> list[dict]:
    """Return [{"chunk_id","text","heading","version"}] in the order of the identifiers (missing ids are skipped)."""
    _open()
    out = []
    if _state["backend"] == "chroma":
        got = _state["coll"].get(ids=ids, include=["documents", "metadatas"])
        by_id = {cid: (doc, meta or {}) for cid, doc, meta in
                 zip(got["ids"], got["documents"], got["metadatas"])}
        for cid in ids:
            if cid in by_id:
                doc, meta = by_id[cid]
                out.append({"chunk_id": cid, "text": doc,
                            "heading": meta.get("heading", ""),
                            "version": meta.get("version", "")})
    else:
        for cid in ids:
            r = _state["json"].get(cid)
            if r:
                out.append({"chunk_id": cid, "text": r["text"],
                            "heading": r.get("heading", ""),
                            "version": r.get("version", "")})
    for c in out:
        _served[c["chunk_id"]] = hashlib.sha256(c["text"].encode("utf-8")).hexdigest()[:12]
    return out


def provenance() -> dict:
    """For the run metadata: backend, reason ChromaDB failed, and the text hash of each fetched chunk."""
    return {"backend": _state["backend"], "chroma_error": _state["error"],
            "chunk_text_sha256_12": dict(sorted(_served.items()))}
