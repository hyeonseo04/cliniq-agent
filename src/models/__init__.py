"""cliniq-agent 공용 Pydantic I/O 모델.

에이전트 간 데이터는 반드시 이 패키지의 모델로만 주고받는다 (자연어 전달 금지).
LLM 출력은 structured output(function calling)으로 이 모델에 맞춰 강제한다.
"""
from .common import (
    DB_KO,
    SUPPORTED_DBS,
    DQDimension,
    Grain,
    RuleComplexity,
    Severity,
    TargetDB,
    assert_supported,
)
from .intent import ASPECT_KO, EvalAspect, Intent, IntentResult, TargetSpec
from .rule import (
    DedupRemoval,
    DedupResult,
    QualityRule,
    RuleStatus,
    RuleValidationResult,
    Threshold,
)
from .schema_link import (
    FieldInfo,
    ForeignKeyInfo,
    JoinEdge,
    SchemaLinkResult,
    SelectedTable,
    TableSchema,
)
from .sql import (
    ExecutionResult,
    FailureCause,
    RefineDecision,
    StaticCheckResult,
)
from .state import (
    MAX_REFINE_ITERATIONS,
    AgentAnswer,
    ExecutionReport,
    PipelineState,
)

__all__ = [
    "AgentAnswer",
    "DedupRemoval",
    "DedupResult",
    "DB_KO",
    "SUPPORTED_DBS",
    "DQDimension",
    "ExecutionReport",
    "ExecutionResult",
    "FailureCause",
    "FieldInfo",
    "ForeignKeyInfo",
    "Grain",
    "ASPECT_KO",
    "EvalAspect",
    "Intent",
    "IntentResult",
    "TargetSpec",
    "JoinEdge",
    "MAX_REFINE_ITERATIONS",
    "PipelineState",
    "QualityRule",
    "RefineDecision",
    "RuleComplexity",
    "RuleStatus",
    "RuleValidationResult",
    "SchemaLinkResult",
    "SelectedTable",
    "Severity",
    "StaticCheckResult",
    "TableSchema",
    "TargetDB",
    "assert_supported",
    "Threshold",
]