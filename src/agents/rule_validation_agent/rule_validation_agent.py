"""Rule Validation 에이전트 진입 클래스.
리포 경로: src/agents/rule_validation_agent/rule_validation_agent.py

합격 규칙은 status를 VALIDATED로 승격, 불합격은 feedback과 함께 반환
(pipeline이 feedback을 모아 Rule Generation 재시도에 주입).
"""
from __future__ import annotations

from core.llm import LLM, get_llm
from models.rule import QualityRule, RuleStatus, RuleValidationResult

from .judge import judge_rule


class RuleValidationAgent:
    def __init__(self, llm: LLM | None = None):
        self.llm = llm or get_llm()

    def run(
        self, rules: list[QualityRule], request: str
    ) -> tuple[list[QualityRule], list[RuleValidationResult]]:
        """Returns: (합격 규칙[VALIDATED 승격], 전체 판정 결과)"""
        results = [judge_rule(r, request, self.llm) for r in rules]
        passed_ids = {v.rule_id for v in results if v.passed}
        passed = []
        for r in rules:
            if r.rule_id in passed_ids:
                passed.append(r.model_copy(update={"status": RuleStatus.VALIDATED}))
        return passed, results