"""임베딩 함수 — Chroma 인덱스 구축/검색과 Dedup 1단계 스크리닝이 공유.

기본: sentence-transformers의 bge-m3 (다국어 — 한국어 질의 ↔ 영어 스키마 매칭).
  연구실 서버에서 `pip install sentence-transformers` 후 최초 1회 모델 자동 다운로드.
테스트: 네트워크/GPU 없는 환경에서는 HashingEmbedder(문자 n-gram 해싱)를 주입해
  파이프라인 로직만 검증한다. 검색 '품질'은 실제 모델에서만 유효.

리포 경로: src/core/embeddings.py
"""
from __future__ import annotations

import hashlib
import math

from chromadb.api.types import EmbeddingFunction

import config


class BgeM3Embedder(EmbeddingFunction):
    """bge-m3 임베더 (chromadb EmbeddingFunction 상속 — embed_query 등 기본 구현 획득)."""

    def __init__(self, model_name: str = config.EMBED_MODEL):
        from sentence_transformers import SentenceTransformer  # 지연 import

        self._model_name = model_name
        self._model = SentenceTransformer(model_name)

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._model.encode(input, normalize_embeddings=True).tolist()

    def name(self) -> str:  # chromadb가 컬렉션 메타에 기록
        return f"bge::{self._model_name}"


class HashingEmbedder(EmbeddingFunction):
    """문자 3-gram 해싱 bag-of-words 임베더 (테스트 전용, 모델 다운로드 불필요).

    토큰 표면 일치 기반이므로 동의어 사전이 문서에 주입되어 있으면
    한국어 질의도 어느 정도 매칭된다 — 인덱스/검색 '배관' 검증 용도."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        t = text.lower()
        for i in range(len(t) - 2):
            gram = t[i : i + 3]
            h = int(hashlib.md5(gram.encode()).hexdigest(), 16) % self.dim
            v[h] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in input]

    def name(self) -> str:
        return f"hashing::{self.dim}"


def get_embedder(kind: str = "bge"):
    if kind == "hashing":
        return HashingEmbedder()
    return BgeM3Embedder()