"""Orchestrator planner의 출력 모델. 리포 경로: src/models/intent.py

planner는 대화 턴마다 호출되며, 지금까지 모인 슬롯 + 새 발화를 받아
갱신된 슬롯을 반환한다 (슬롯 누적형 멀티턴).
슬롯이 모두 차면 executor가 파이프라인을 시작한다.
"""
from enum import Enum

from pydantic import BaseModel, Field

from .common import TargetDB


class Intent(str, Enum):
    RETRIEVAL = "retrieval"  # 저장된 규칙(지표) 목록 조회 — 향후 구현
    CREATIVE = "creative"    # 새로운 품질 규칙 생성 — 현재 구현
    OTHERS = "others"        # 위 둘에 해당하지 않음 → 일반 답변


class EvalAspect(str, Enum):
    """평가 관점. 사용자가 '무엇을 볼지'를 지정하는 슬롯.

    OHDSI DQD의 Kahn 분류(+하위 범주)에 1:1 대응한다:
      MISSING     → Completeness
      REFERENTIAL → Conformance / Relational  (NOT NULL 위반, FK 미존재)
      STANDARD    → Conformance / Value       (표준·유효 concept, 도메인 일치)
      VALUE_RANGE → Plausibility / Atemporal  (타당 하한·상한 이탈)
      TEMPORAL    → Plausibility / Temporal   (날짜 선후, 기간 포함)
    """

    MISSING = "missing"
    REFERENTIAL = "referential"
    STANDARD = "standard"
    VALUE_RANGE = "value_range"
    TEMPORAL = "temporal"
    CLINICAL = "clinical"    # 임상 타당성 — 성별·단위 등 도메인 지식 기반
    OTHER = "other"


ASPECT_KO = {
    EvalAspect.MISSING: "결측",
    EvalAspect.REFERENTIAL: "필수·참조 무결성",
    EvalAspect.STANDARD: "표준 개념",
    EvalAspect.VALUE_RANGE: "값 범위",
    EvalAspect.TEMPORAL: "시간 타당성",
    EvalAspect.CLINICAL: "임상 타당성",
    EvalAspect.OTHER: "기타",
}


class TargetSpec(BaseModel):
    """사용자가 지목한 평가 대상 (테이블 + 컬럼)."""

    table: str = Field(description="테이블명 (소문자)")
    columns: list[str] = Field(
        default_factory=list, description="컬럼명 목록 (소문자). 비어 있으면 미지정"
    )


class IntentResult(BaseModel):
    """planner 출력. 매 턴 갱신되는 누적 슬롯."""

    intent: Intent
    request_summary: str = Field(
        description="지금까지의 대화를 반영한 요청 요약문 (한국어)"
    )
    targets: list[TargetSpec] = Field(
        default_factory=list, description="지목된 테이블·컬럼. 슬롯"
    )
    aspects: list[EvalAspect] = Field(
        default_factory=list, description="평가 관점. 슬롯"
    )
    aspect_detail: str | None = Field(
        default=None, description="관점에 대한 사용자의 구체적 서술 (있으면)"
    )
    target_db: TargetDB = Field(
        default=TargetDB.INTEGRATED, description="대상 DB. 미지정 시 통합DB"
    )

    # ---------- 슬롯 충족 검사 ----------
    def missing_slots(self) -> list[str]:
        """비어 있는 필수 슬롯 목록. creative 전용."""
        missing = []
        if not self.targets:
            missing.append("table")
        elif any(not t.columns for t in self.targets):
            missing.append("column")
        if not self.aspects:
            missing.append("aspect")
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.missing_slots()

    @property
    def mode(self) -> str:
        """생성 모드 — 기초(단일 테이블) / 심화(다중 테이블).
        프롬프트 분기에만 사용하며 규칙 데이터에는 저장하지 않는다."""
        return "basic" if len(self.targets) <= 1 else "advanced"