"""스키마 문서 + 한글 동의어 → Chroma 인덱스 구축 (1회성).

리포 경로: scripts/build_rag_index.py
사용법:
    PYTHONPATH=.:src python scripts/build_rag_index.py            # bge-m3 (실사용)
    PYTHONPATH=.:src python scripts/build_rag_index.py --embedder hashing  # 테스트

입력:
    data/cdm/schema/cdm_schema.json
    data/cdm/information/synonyms.json
출력:
    data/chroma/  (persistent collection: "cdm_schema")

문서 구성 (schema_store.rag_documents 기반):
    - table 청크: "테이블명: 설명" + [동의어 주입] ← 한국어 질의 매칭의 핵심
    - column 청크: "테이블.컬럼 (타입): 설명"
스키마/동의어 수정 시 이 스크립트를 재실행하면 컬렉션이 재생성된다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import chromadb  # noqa: E402

import config  # noqa: E402
from core.embeddings import get_embedder  # noqa: E402
from core.schema_store import get_schema_store  # noqa: E402

COLLECTION = "cdm_schema"
SYNONYMS_PATH = config.DATA_DIR / "cdm" / "information" / "synonyms.json"


def load_synonyms() -> dict[str, list[str]]:
    if not SYNONYMS_PATH.exists():
        return {}
    raw = json.loads(SYNONYMS_PATH.read_text(encoding="utf-8"))
    return {e["table"]: e["synonyms"] for e in raw.get("tables", [])}


def build(embedder_kind: str = "bge") -> int:
    store = get_schema_store()
    synonyms = load_synonyms()
    docs = store.rag_documents()

    # 테이블 청크에 한글 동의어 주입
    for d in docs:
        if d["metadata"]["kind"] == "table":
            syns = synonyms.get(d["metadata"]["table"])
            if syns:
                d["text"] += f"\n한국어 동의어: {', '.join(syns)}"

    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    try:
        client.delete_collection(COLLECTION)  # 재빌드
    except Exception:
        pass
    col = client.create_collection(
        COLLECTION,
        embedding_function=get_embedder(embedder_kind),
        metadata={"hnsw:space": "cosine", "embedder": embedder_kind},
    )

    B = 100
    for i in range(0, len(docs), B):
        batch = docs[i : i + B]
        col.add(
            ids=[d["id"] for d in batch],
            documents=[d["text"] for d in batch],
            metadatas=[d["metadata"] for d in batch],
        )
    return len(docs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", default="bge", choices=["bge", "hashing"])
    args = ap.parse_args()
    n = build(args.embedder)
    print(f"indexed {n} docs → {config.CHROMA_DIR} (collection={COLLECTION}, embedder={args.embedder})")


if __name__ == "__main__":
    main()