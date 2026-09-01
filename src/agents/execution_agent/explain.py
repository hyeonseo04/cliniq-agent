"""EXPLAIN PLAN 검증. 리포 경로: src/agents/execution_agent/explain.py

DB가 SQL을 실제로 받아들이는지 확인한다. 데이터를 읽지 않으므로:
  - 빈 스키마(DDL만 적재)에서도 동작
  - 실행 비용이 사실상 0
  - 운영 DB에 영향 없음

sqlglot 정적검사가 못 잡는 것을 잡는다:
  - Oracle 방언 위반 (LIMIT, ILIKE 등)
  - 실제 테이블·컬럼 부재 (ORA-00942, ORA-00904)
  - 타입 불일치, 함수 인자 오류
"""
from __future__ import annotations

import logging
import re

from core.db import OracleDB, assert_read_only

log = logging.getLogger(__name__)

# 오류 원인 분류용 Oracle 에러 코드
ORA_TABLE_MISSING = "ORA-00942"   # table or view does not exist
ORA_COLUMN_INVALID = "ORA-00904"  # invalid identifier
_ORA_CODE = re.compile(r"(ORA-\d{5})")


class ExplainResult:
    def __init__(
        self, rule_id: str, ok: bool, error: str | None = None, ora_code: str | None = None
    ):
        self.rule_id = rule_id
        self.ok = ok
        self.error = error
        self.ora_code = ora_code

    @property
    def cause(self) -> str | None:
        """실패 원인 분류 — Refine 라우팅에 사용."""
        if self.ok:
            return None
        if self.ora_code in (ORA_TABLE_MISSING, ORA_COLUMN_INVALID):
            return "schema_error"
        return "sql_error"

    def __repr__(self) -> str:
        return f"ExplainResult({self.rule_id}, ok={self.ok}, {self.ora_code or ''})"


def explain(rule_id: str, sql: str, db: OracleDB) -> ExplainResult:
    """EXPLAIN PLAN FOR <sql> 실행. 성공하면 ok=True."""
    try:
        assert_read_only(sql)
    except Exception as e:  # noqa: BLE001
        return ExplainResult(rule_id, False, f"unsafe sql: {e}")

    stmt = f"EXPLAIN PLAN SET STATEMENT_ID = '{rule_id[:29]}' FOR {sql.rstrip(';')}"
    try:
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(stmt)
            conn.rollback()  # PLAN_TABLE 기록 정리
        return ExplainResult(rule_id, True)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        m = _ORA_CODE.search(msg)
        log.warning("EXPLAIN 실패 (%s): %s", rule_id, msg[:120])
        return ExplainResult(rule_id, False, msg, m.group(1) if m else None)