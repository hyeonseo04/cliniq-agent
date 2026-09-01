"""Refine 에이전트 — 실행 실패 복구.
리포 경로: src/agents/refine_agent/refine_agent.py

두 역할:
  1) 라우터: 실패 원인을 분류해 어느 단계로 되돌릴지 결정
  2) 디버거: SQL 오류는 자체 수정 후 재검증 (LLM-DQR Algorithm 2)

수정본 채택 조건:
  - 정적검사 재통과
  - 원래 규칙의 대상 컬럼이 유지될 것 (오류 회피용 조건 삭제 방지)
"""
from __future__ import annotations

import logging

from agents.sql_agent.checker import static_check
from core.llm import LLM, get_llm
from core.schema_store import SchemaStore, get_schema_store
from models.rule import QualityRule
from models.schema_link import SchemaLinkResult
from models.sql import ExecutionResult, FailureCause, RefineDecision

from .analyzer import ROUTE, classify
from .debugger import debug_sql

log = logging.getLogger(__name__)


class RefineAgent:
    def __init__(self, llm: LLM | None = None, store: SchemaStore | None = None):
        self.llm = llm or get_llm()
        self.store = store or get_schema_store()

    def run(
        self,
        rule: QualityRule,
        result: ExecutionResult,
        schema_link: SchemaLinkResult,
        attempt: int,
    ) -> RefineDecision:
        cause = classify(result)
        error = result.error_message or "(원인 미상)"

        if cause is not FailureCause.SQL_ERROR:
            return RefineDecision(
                rule_id=rule.rule_id,
                cause=cause,
                route_to=ROUTE[cause],
                feedback=f"실행 실패: {error}",
                attempt=attempt,
            )

        # SQL 오류 → 자체 수정 시도
        try:
            fixed = debug_sql(rule, error, schema_link, self.llm)
        except Exception as e:  # noqa: BLE001
            log.warning("디버거 실패 (%s): %s", rule.rule_id, e)
            fixed = None

        if fixed and self._is_safe(rule, fixed):
            return RefineDecision(
                rule_id=rule.rule_id,
                cause=cause,
                route_to="sql_generation",
                feedback=f"SQL 오류 자체 수정: {error}",
                fixed_sql=fixed,
                attempt=attempt,
            )

        return RefineDecision(
            rule_id=rule.rule_id,
            cause=cause,
            route_to="sql_generation",
            feedback=f"SQL 오류(자체 수정 실패, 재생성 필요): {error}",
            attempt=attempt,
        )

    def _is_safe(self, rule: QualityRule, sql: str) -> bool:
        """수정본 채택 검사: 정적검사 통과 + 대상 컬럼 유지."""
        check = static_check(rule.rule_id, sql, self.store)
        if not check.passed:
            log.warning("수정본 정적검사 실패 (%s): %s", rule.rule_id, check.parse_error)
            return False

        lowered = sql.lower()
        for col in rule.target_columns:
            name = col.split(".")[-1].lower()
            if name not in lowered:
                log.warning(
                    "수정본이 대상 컬럼 %s 를 잃었다 (%s) — 채택 거부", name, rule.rule_id
                )
                return False
        return True