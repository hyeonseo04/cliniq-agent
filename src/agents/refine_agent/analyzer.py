"""실패 원인 분류 → 회귀 대상 결정.
리포 경로: src/agents/refine_agent/analyzer.py

원인별 처리 방침:
  schema_error → Input(테이블 재선택)  : 근본 원인이 앞 단계에 있음
  logic_error  → Rule Generation      : 규칙 정의 자체가 잘못됨
  sql_error    → Refine 자체 수정      : 에러 메시지에 정답이 들어 있음
"""
from __future__ import annotations

import re

from models.sql import ExecutionResult, FailureCause

_ORA = re.compile(r"ORA-(\d{5})")

# 스키마 문제 (테이블·컬럼 부재)
_SCHEMA_CODES = {"00942", "00904"}
# 논리 문제 (결과가 의미 없음)
_LOGIC_MARKERS = ("분모가 0", "범위 위반", "결과 형식 위반")


def classify(result: ExecutionResult) -> FailureCause:
    msg = result.error_message or ""

    if any(m in msg for m in _LOGIC_MARKERS):
        return FailureCause.LOGIC_ERROR

    m = _ORA.search(msg)
    if m and m.group(1) in _SCHEMA_CODES:
        return FailureCause.SCHEMA_ERROR

    return FailureCause.SQL_ERROR


ROUTE = {
    FailureCause.SCHEMA_ERROR: "input",
    FailureCause.LOGIC_ERROR: "rule_generation",
    FailureCause.SQL_ERROR: "sql_generation",  # 실제로는 Refine이 자체 수정
}