"""Dedup 1단계: 임베딩 유사도 스크리닝. 리포 경로: src/agents/deduplicate_agent/screener.py

LLM-DQR Algorithm 1의 전반부 — 모든 규칙 쌍의 cosine similarity를 계산해
τ(기본 0.95, config.DEDUP_SIMILARITY_THRESHOLD) 초과 쌍만 후보로 넘긴다.
목적: 2단계 LLM Inspector 호출을 후보 쌍에만 한정 (비용 절감).

규칙 시그니처 = SQL이 있으면 SQL, 없으면 "logic_nl | tables | columns".
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import config
from models.rule import QualityRule


_INDICATOR = re.compile(r"\[지표:\s*([A-Za-z0-9_]+)\]")


def _structural_key(r: QualityRule) -> tuple:
    """구조 시그니처 — 텍스트 표현과 무관한 동일성 판정용.

    같은 대상·같은 차원·같은 표준 지표라면 표현이 달라도 같은 검사일 가능성이 높다.
    (예: "start > end"와 "end < start"는 자연어·SQL 표현이 다르지만 동일한 검사)
    컬럼은 정렬해 비교하므로 나열 순서 차이도 흡수한다.
    """
    m = _INDICATOR.search(r.name)
    return (
        r.target_db.value,
        tuple(sorted(t.lower() for t in r.target_tables)),
        tuple(sorted(c.lower() for c in r.target_columns)),
        r.dq_dimension.value,
        m.group(1) if m else "",
    )


def _signature(r: QualityRule) -> str:
    """임베딩 유사도 계산용 텍스트 시그니처."""
    if r.sql:
        return " ".join(r.sql.lower().split())
    return (
        f"{r.logic_nl} | tables={','.join(sorted(r.target_tables))}"
        f" | cols={','.join(sorted(r.target_columns))}"
    )


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


@dataclass
class CandidatePair:
    rule_a: QualityRule  # 유지 우선 (먼저 생성/기존 규칙)
    rule_b: QualityRule  # 제거 후보
    similarity: float


def screen(
    new_rules: list[QualityRule],
    existing_rules: list[QualityRule],
    embedder,
    threshold: float = config.DEDUP_SIMILARITY_THRESHOLD,
) -> list[CandidatePair]:
    """비교 대상: (신규 × 신규) + (기존 × 신규). 기존끼리는 비교하지 않는다."""
    all_rules = existing_rules + new_rules
    if len(all_rules) < 2 or not new_rules:
        return []

    vecs = embedder([_signature(r) for r in all_rules])
    n_exist = len(existing_rules)

    keys = [_structural_key(r) for r in all_rules]

    pairs: list[CandidatePair] = []
    for i in range(len(all_rules)):
        for j in range(max(i + 1, n_exist), len(all_rules)):  # j는 항상 신규
            sim = _cosine(vecs[i], vecs[j])
            # 구조가 완전히 같으면 텍스트 유사도와 무관하게 후보로 올린다.
            # (동일 검사를 부등호 방향만 바꿔 표현한 경우 임베딩 유사도가
            #  임계값에 미달해 놓치는 문제를 방지)
            if sim > threshold or keys[i] == keys[j]:
                pairs.append(
                    CandidatePair(rule_a=all_rules[i], rule_b=all_rules[j], similarity=sim)
                )
    return pairs