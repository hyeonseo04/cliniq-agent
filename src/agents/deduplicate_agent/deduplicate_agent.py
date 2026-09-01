"""Deduplicate 에이전트 진입 클래스 (LLM-DQR Algorithm 1).
리포 경로: src/agents/deduplicate_agent/deduplicate_agent.py

흐름: screener(임베딩 τ 초과 쌍) → inspector(LLM 동치 판정) → 동치면 신규 쪽 제거.
existing_rules는 규칙 카탈로그(타 파트) 조회 결과 — 없으면 빈 리스트.
"""
from __future__ import annotations

from core.llm import LLM, get_llm
from models.rule import DedupRemoval, DedupResult, QualityRule

from .inspector import check_equivalence
from .screener import screen


class DeduplicateAgent:
    def __init__(self, embedder, llm: LLM | None = None):
        self.embedder = embedder
        self.llm = llm or get_llm()

    def run(
        self,
        new_rules: list[QualityRule],
        existing_rules: list[QualityRule] | None = None,
    ) -> DedupResult:
        existing = existing_rules or []
        pairs = screen(new_rules, existing, self.embedder)

        removed_ids: set[str] = set()
        removals: list[DedupRemoval] = []
        for p in pairs:
            if p.rule_b.rule_id in removed_ids or p.rule_a.rule_id in removed_ids:
                continue
            judge = check_equivalence(p.rule_a, p.rule_b, self.llm)
            if judge.is_equivalent:
                removed_ids.add(p.rule_b.rule_id)  # 항상 신규 쪽 제거
                removals.append(
                    DedupRemoval(
                        removed_rule_id=p.rule_b.rule_id,
                        kept_rule_id=p.rule_a.rule_id,
                        similarity=round(p.similarity, 4),
                        inspector_reason=judge.reason,
                    )
                )

        unique = [r for r in new_rules if r.rule_id not in removed_ids]
        return DedupResult(unique_rules=unique, removals=removals)