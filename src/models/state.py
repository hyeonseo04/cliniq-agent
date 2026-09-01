"""LangGraph 파이프라인 상태 + 최종 사용자 응답 모델.

PipelineState는 creative 분기의 모든 노드가 읽고 쓰는 단일 상태 객체.
LangGraph의 state schema로 그대로 사용한다.
"""
from pydantic import BaseModel, Field

from .intent import IntentResult
from .rule import DedupResult, QualityRule, RuleValidationResult
from .schema_link import SchemaLinkResult
from .sql import ExecutionResult, RefineDecision, StaticCheckResult

MAX_REFINE_ITERATIONS = 5  # LLM-DQR 논문과 동일


class PipelineState(BaseModel):
    """creative 파이프라인 전체 상태."""

    # 입력 (Orchestrator가 채움)
    user_query: str
    intent: IntentResult

    # 각 노드의 출력 (진행하며 채워짐)
    schema_link: SchemaLinkResult | None = None
    generated_rules: list[QualityRule] = Field(default_factory=list)
    # 단계별 스냅샷 (덮어쓰이는 generated_rules와 별도로 추적 — 리포트·디버깅용)
    trace_generated: list[QualityRule] = Field(default_factory=list)
    trace_rejected: list[str] = Field(default_factory=list)
    trace_validated: list[QualityRule] = Field(default_factory=list)
    dedup: DedupResult | None = None
    rule_validations: list[RuleValidationResult] = Field(default_factory=list)
    static_checks: list[StaticCheckResult] = Field(default_factory=list)
    executions: list[ExecutionResult] = Field(default_factory=list)
    refine_decisions: list[RefineDecision] = Field(default_factory=list)

    # 단계별 소요 시간 (초) — 노드명 → 누적 시간
    stage_timings: dict[str, float] = Field(default_factory=dict)

    # 루프 제어
    refine_count: int = 0
    rule_regen_count: int = 0

    # 에러/중단 사유
    aborted: bool = False
    abort_reason: str | None = None

    def can_refine(self) -> bool:
        return self.refine_count < MAX_REFINE_ITERATIONS


class ExecutionReport(BaseModel):
    """리포트에 넣을 규칙별 실행 요약."""

    rule_id: str
    rule_name: str
    violation_count: int | None
    violation_ratio: float | None
    passed: bool | None


class AgentAnswer(BaseModel):
    """모든 분기가 공유하는 최종 사용자 응답 (Output 규격).

    - informational/retrieval/other: answer만 채워짐
    - creative: generated_rules까지, Phase 4 이후 execution_report까지
    """

    answer: str
    generated_rules: list[QualityRule] = Field(default_factory=list)
    execution_report: list[ExecutionReport] = Field(default_factory=list)