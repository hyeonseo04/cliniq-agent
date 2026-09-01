"""Dedup 2단계: LLM 논리 동치 판정. 리포 경로: src/agents/deduplicate_agent/inspector.py"""
from __future__ import annotations

from pydantic import BaseModel

from core.llm import LLM
from models.rule import QualityRule
from prompts import dedup_inspector as P


class EquivalenceJudgement(BaseModel):
    is_equivalent: bool
    reason: str


def _render(r: QualityRule) -> str:
    return (
        f"id: {r.rule_id}\n"
        f"logic: {r.logic_nl}\n"
        f"denominator: {r.denominator_nl}\n"
        f"numerator: {r.numerator_nl}\n"
        f"tables: {r.target_tables}\ncolumns: {r.target_columns}\n"
        f"sql: {r.sql or '(미생성)'}"
    )


def check_equivalence(a: QualityRule, b: QualityRule, llm: LLM) -> EquivalenceJudgement:
    return llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(rule_a=_render(a), rule_b=_render(b)),
        EquivalenceJudgement,
    )