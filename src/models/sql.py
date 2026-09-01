"""SQL 정적검사 / 실행검증 / Refine 라우팅 모델."""
from enum import Enum

from pydantic import BaseModel, Field


class StaticCheckResult(BaseModel):
    """SQL Generation 직후의 무료 검사 (DB 불필요).
    1) sqlglot 파싱(dialect=oracle)  2) 참조 컬럼 존재 검사"""

    rule_id: str
    parse_ok: bool
    parse_error: str | None = None
    unknown_tables: list[str] = Field(default_factory=list)
    unknown_columns: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.parse_ok and not self.unknown_tables and not self.unknown_columns


class ExecutionResult(BaseModel):
    """Oracle 샌드박스/실DB 실행 결과 (Phase 4 이후 사용)."""

    rule_id: str
    success: bool
    error_message: str | None = None
    denominator_count: int | None = None
    violation_count: int | None = None
    violation_ratio: float | None = None
    passed_threshold: bool | None = Field(
        default=None, description="threshold 기준 합격 여부. 실행 실패 시 None"
    )
    elapsed_ms: int | None = None


class FailureCause(str, Enum):
    """Refine이 판정하는 실패 원인 → 회귀할 단계 결정."""

    SCHEMA_ERROR = "schema_error"  # ORA-00942 등 → Schema Linking으로
    LOGIC_ERROR = "logic_error"    # 분모 0, 의미 불일치 → Rule Generation으로
    SQL_ERROR = "sql_error"        # 문법/방언 오류 → SQL Generation으로


class RefineDecision(BaseModel):
    """Refine 에이전트 출력 (LLM-DQR Algorithm 2의 Debugger 역할 + 라우팅).

    단순 SQL 오류는 route_to='sql_generation'으로 보내되 fixed_sql을
    함께 제안할 수 있다 (논문의 LLM Debugger 방식)."""

    rule_id: str
    cause: FailureCause
    route_to: str = Field(
        description="회귀 대상 노드: schema_linking | rule_generation | sql_generation"
    )
    feedback: str = Field(description="회귀 대상에 주입할 실패 원인 설명")
    fixed_sql: str | None = Field(
        default=None,
        description="cause=sql_error일 때 Debugger가 직접 수정한 SQL 제안",
    )
    attempt: int = Field(description="이 규칙의 누적 재시도 횟수")
