"""Execution & Validation 에이전트.
리포 경로: src/agents/execution_agent/execution_agent.py

Phase 4-1 (현재): EXPLAIN PLAN 검증만 수행 — DDL만 적재된 빈 스키마에서 동작
Phase 4-2 (예정): 실제 실행 + 위반율 산출 + 신뢰성 검사

LLM을 사용하지 않는 결정론적 에이전트다.
"""
from __future__ import annotations

import logging
import time

from core.db import OracleDB
from models.rule import QualityRule
from models.sql import ExecutionResult

from .explain import explain

log = logging.getLogger(__name__)


class ExecutionAgent:
    def __init__(self, db: OracleDB | None = None, explain_only: bool = True):
        self.db = db
        self.explain_only = explain_only

    def run(self, rules: list[QualityRule]) -> list[ExecutionResult]:
        if self.db is None:
            return [
                ExecutionResult(
                    rule_id=r.rule_id, success=False, error_message="DB 미연결"
                )
                for r in rules
            ]

        results: list[ExecutionResult] = []
        for rule in rules:
            if not rule.sql:
                results.append(
                    ExecutionResult(
                        rule_id=rule.rule_id, success=False, error_message="SQL 없음"
                    )
                )
                continue

            t0 = time.perf_counter()
            ex = explain(rule.rule_id, rule.sql, self.db)
            elapsed = int((time.perf_counter() - t0) * 1000)

            if not ex.ok:
                results.append(
                    ExecutionResult(
                        rule_id=rule.rule_id,
                        success=False,
                        error_message=ex.error,
                        elapsed_ms=elapsed,
                    )
                )
                continue

            if self.explain_only:
                # EXPLAIN 통과 = "DB가 받아들이는 SQL"임을 확인한 상태
                results.append(
                    ExecutionResult(
                        rule_id=rule.rule_id, success=True, elapsed_ms=elapsed
                    )
                )
                continue

            results.append(self._execute(rule, elapsed))
        return results

    def _execute(self, rule: QualityRule, explain_ms: int) -> ExecutionResult:
        """실제 실행 + 위반율 산출 (Phase 4-2)."""
        t0 = time.perf_counter()
        try:
            with self.db.connect() as conn:
                cur = conn.cursor()
                cur.execute(rule.sql.rstrip(";"))
                row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            return ExecutionResult(
                rule_id=rule.rule_id, success=False, error_message=str(e)
            )

        elapsed = explain_ms + int((time.perf_counter() - t0) * 1000)
        if row is None or len(row) < 2:
            return ExecutionResult(
                rule_id=rule.rule_id,
                success=False,
                error_message="결과 형식 위반: 두 컬럼이 반환되지 않았다",
                elapsed_ms=elapsed,
            )

        den, num = int(row[0] or 0), int(row[1] or 0)

        # 신뢰성 검사
        if den == 0:
            return ExecutionResult(
                rule_id=rule.rule_id,
                success=False,
                error_message="분모가 0이다 (평가 모집단이 비어 있음)",
                denominator_count=0,
                violation_count=num,
                elapsed_ms=elapsed,
            )
        if not (0 <= num <= den):
            return ExecutionResult(
                rule_id=rule.rule_id,
                success=False,
                error_message=f"범위 위반: 위반 건수({num})가 분모({den}) 범위를 벗어남",
                denominator_count=den,
                violation_count=num,
                elapsed_ms=elapsed,
            )

        ratio = num / den
        passed = (
            ratio <= rule.threshold.max_value
            if rule.threshold.type == "ratio"
            else num <= rule.threshold.max_value
        )
        return ExecutionResult(
            rule_id=rule.rule_id,
            success=True,
            denominator_count=den,
            violation_count=num,
            violation_ratio=round(ratio, 6),
            passed_threshold=passed,
            elapsed_ms=elapsed,
        )