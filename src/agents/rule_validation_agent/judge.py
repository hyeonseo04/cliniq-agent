"""LLM-judge 의미 검증. 리포 경로: src/agents/rule_validation_agent/judge.py"""
from __future__ import annotations

from pydantic import BaseModel

from core.llm import LLM
from models.rule import QualityRule, RuleValidationResult
from prompts import rule_validation as P


class _JudgeOutput(BaseModel):
    passed: bool
    feedback: str | None = None


def _render(r: QualityRule) -> str:
    return (
        f"name: {r.name}\ndescription: {r.description}\n"
        f"dq_dimension: {r.dq_dimension.value}\n"
        f"complexity: {r.complexity.value}\n"
        f"severity: {r.severity.value}\n"
        f"tables: {r.target_tables}\ncolumns: {r.target_columns}\n"
        f"logic: {r.logic_nl}\n"
        f"denominator: {r.denominator_nl}\nnumerator: {r.numerator_nl}\n"
        f"threshold: {r.threshold.type} <= {r.threshold.max_value}"
    )


def judge_rule(rule: QualityRule, request: str, llm: LLM) -> RuleValidationResult:
    out = llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(request=request, rule=_render(rule)),
        _JudgeOutput,
        thinking=True,  # 의미 검증은 추론 필요
    )
    return RuleValidationResult(
        rule_id=rule.rule_id,
        passed=out.passed,
        feedback=None if out.passed else (out.feedback or "사유 미기재"),
    )