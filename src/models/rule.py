"""품질 규칙(DQR) 모델 + Rule Validation / Deduplication 출력.

QualityRule은 조회 친화적으로 설계 — retrieval 분기("어떤 지표 있어?")가
target_db / dq_dimension / target_tables / status 필드 필터만으로 답할 수 있다.
"""
from enum import Enum

from pydantic import BaseModel, Field

from .common import DQDimension, RuleComplexity, Severity, TargetDB


class RuleStatus(str, Enum):
    DRAFT = "draft"          # 생성 직후
    VALIDATED = "validated"  # Rule Validation 통과
    EXECUTABLE = "executable"  # SQL 실행 검증까지 통과
    REJECTED = "rejected"


class Threshold(BaseModel):
    """합격 판정 기준. 위반율(ratio) 또는 위반건수(count)."""

    type: str = Field(default="ratio", pattern="^(ratio|count)$")
    max_value: float = Field(
        default=0.0, description="이 값 이하이면 합격. ratio는 0.0~1.0"
    )


class QualityRule(BaseModel):
    """품질 규칙 1건. Rule Generation의 출력 단위."""

    rule_id: str = Field(description="예: R-MEAS-001")
    name: str
    description: str = Field(description="규칙을 한 문장으로")
    dq_dimension: DQDimension
    complexity: RuleComplexity
    target_db: TargetDB
    target_tables: list[str]
    target_columns: list[str] = Field(description="'table.column' 형식")
    logic_nl: str = Field(description="위반 조건의 자연어 정의")
    denominator_nl: str = Field(description="분모(평가 모집단)의 자연어 정의")
    numerator_nl: str = Field(description="분자(위반 레코드)의 자연어 정의")
    severity: Severity = Severity.ERROR
    threshold: Threshold = Field(default_factory=Threshold)
    status: RuleStatus = RuleStatus.DRAFT
    sql: str | None = Field(
        default=None, description="SQL Generation이 채움. 생성 전에는 None"
    )


class RuleValidationResult(BaseModel):
    """Rule Validation(LLM-judge)의 규칙 1건에 대한 판정."""

    rule_id: str
    passed: bool
    feedback: str | None = Field(
        default=None,
        description="불합격 사유. 재생성 프롬프트에 그대로 주입되므로 구체적으로",
    )


class DedupRemoval(BaseModel):
    """중복 판정으로 제거된 규칙의 기록 (감사/디버깅용)."""

    removed_rule_id: str
    kept_rule_id: str
    similarity: float = Field(description="1단계 임베딩 cosine similarity")
    inspector_reason: str = Field(description="2단계 LLM Inspector의 동치 판정 근거")


class DedupResult(BaseModel):
    """Deduplication 출력. LLM-DQR Algorithm 1의 결과."""

    unique_rules: list[QualityRule]
    removals: list[DedupRemoval] = Field(default_factory=list)