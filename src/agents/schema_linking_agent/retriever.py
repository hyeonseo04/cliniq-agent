"""RAG 후보 테이블 검색기. 리포 경로: src/agents/schema_linking_agent/retriever.py

Chroma에서 table/column 청크를 검색해 테이블 단위 후보로 집계한다.
- table 청크 히트: 테이블 자체가 관련 (동의어 매칭 포함)
- column 청크 히트: 해당 컬럼이 관련 → matched_columns로 수집
점수 = max(청크 유사도), 유사도 = 1 - cosine distance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import chromadb

import config

COLLECTION = "cdm_schema"


@dataclass
class TableCandidate:
    table: str
    score: float
    matched_columns: list[str] = field(default_factory=list)
    snippets: list[str] = field(default_factory=list)


class SchemaRetriever:
    def __init__(self, embedder, chroma_path: str | None = None):
        client = chromadb.PersistentClient(path=str(chroma_path or config.CHROMA_DIR))
        self.col = client.get_collection(COLLECTION, embedding_function=embedder)

    def search(
        self, query: str, top_k_chunks: int = 20, top_n_tables: int = 6
    ) -> list[TableCandidate]:
        res = self.col.query(
            query_texts=[query],
            n_results=top_k_chunks,
            include=["metadatas", "documents", "distances"],
        )
        agg: dict[str, TableCandidate] = {}
        for meta, doc, dist in zip(
            res["metadatas"][0], res["documents"][0], res["distances"][0]
        ):
            table = meta["table"]
            score = 1.0 - dist
            c = agg.setdefault(table, TableCandidate(table=table, score=score))
            c.score = max(c.score, score)
            if meta["kind"] == "column":
                c.matched_columns.append(meta["column"])
            if len(c.snippets) < 3:
                c.snippets.append(doc.split("\n")[0][:120])

        return sorted(agg.values(), key=lambda c: c.score, reverse=True)[:top_n_tables]