"""SQL Generation 에이전트 진입 클래스.
리포 경로: src/agents/sql_agent/sql_agent.py

규칙마다: SQL 생성 → 정적검사 → 실패 시 오류를 피드백으로 재생성 (max_retries).
성공 시 rule.sql 채워서 반환. (EXPLAIN/실행 검증은 Phase 4 execution_agent)
"""
from __future__ import annotations

from core.llm import LLM, get_llm
from core.schema_store import SchemaStore, get_schema_store
from models.rule import QualityRule
from models.schema_link import SchemaLinkResult
from models.sql import StaticCheckResult

from .checker import static_check
from .generator import generate_sql


class SQLAgent:
    def __init__(self, llm: LLM | None = None, store: SchemaStore | None = None):
        self.llm = llm or get_llm()
        self.store = store or get_schema_store()

    def run(
        self,
        rules: list[QualityRule],
        schema_link: SchemaLinkResult,
        max_retries: int = 2,
    ) -> tuple[list[QualityRule], list[StaticCheckResult]]:
        """Returns: (sql 채워진 규칙들 — 정적검사 통과분만, 전체 검사 결과)"""
        ok_rules: list[QualityRule] = []
        all_checks: list[StaticCheckResult] = []

        for rule in rules:
            feedback = None
            prev_feedback = None
            for _ in range(max_retries + 1):
                sql = generate_sql(rule, schema_link, self.llm, feedback=feedback)
                check = static_check(rule.rule_id, sql, self.store)
                all_checks.append(check)  # 실패 시도도 모두 기록 (리포트·디버깅용)
                if check.passed:
                    ok_rules.append(rule.model_copy(update={"sql": sql}))
                    break
                feedback = (
                    f"parse_error={check.parse_error}, "
                    f"unknown_tables={check.unknown_tables}, "
                    f"unknown_columns={check.unknown_columns}"
                )
                # 같은 오류가 반복되면 재시도해도 달라지지 않는다 (조기 중단)
                if feedback == prev_feedback:
                    break
                prev_feedback = feedback
        return ok_rules, all_checks