"""공통 enum·타입 정의 — 모든 에이전트 I/O 모델이 공유."""
from enum import Enum


class TargetDB(str, Enum):
    """평가 대상 DB.

    현재 **통합DB만 OMOP CDM v5.3 구조**이며 지원 대상이다.
    전처리DB·연계DB는 스키마 구조가 확정되지 않았으므로 열거값만 예약해 두고,
    실제 사용 시에는 SUPPORTED_DBS 검사에서 차단된다.
    (해당 DB의 스키마가 확정되면 전용 schema JSON을 추가하고 여기에 편입한다)
    """

    INTEGRATED = "integrated"      # 통합DB — OMOP CDM v5.3 (지원)
    PREPROCESSED = "preprocessed"  # 전처리DB — 구조 미확정 (예약)
    LINKED = "linked"              # 연계DB — 구조 미확정 (예약)


#: 현재 지원되는 DB. 스키마(cdm_schema.json)가 이 DB 기준으로 작성되어 있다.
SUPPORTED_DBS = frozenset({TargetDB.INTEGRATED})

DB_KO = {
    TargetDB.INTEGRATED: "통합DB",
    TargetDB.PREPROCESSED: "전처리DB",
    TargetDB.LINKED: "연계DB",
}


def assert_supported(db: TargetDB) -> None:
    """미지원 DB 사용 시 명확한 오류를 낸다."""
    if db not in SUPPORTED_DBS:
        raise ValueError(
            f"{DB_KO.get(db, db.value)}는 아직 지원하지 않는다. "
            f"현재는 OMOP CDM v5.3 구조인 통합DB만 평가 가능하다."
        )


class DQDimension(str, Enum):
    """품질 차원 — Kahn 프레임워크 3범주.

    OHDSI DQD가 채택한 분류 체계를 그대로 따른다.
    골드(평가 기준)가 DQD 공식 정의이므로 차원 체계도 일치시켜야 집계가 정합하다.
    """

    CONFORMANCE = "Conformance"      # 값이 정의된 형식·관계·표준에 맞는가
    COMPLETENESS = "Completeness"    # 값이 존재하는가
    PLAUSIBILITY = "Plausibility"    # 값이 현실적으로 타당한가


class Grain(str, Enum):
    """규칙 평가의 분모·분자 단위.

    v1에서는 사용하지 않는다 — 모든 규칙을 레코드 단위로 고정한다.
    (환자/방문 단위 규칙은 향후 확장 시 재도입)
    """
    PERSON = "person"
    VISIT = "visit"
    RECORD = "record"


class RuleComplexity(str, Enum):
    """LLM-DQR 논문의 규칙 복잡도 계층. 평가·라우팅에 사용."""
    ST_SF = "single_table_single_field"
    ST_MF = "single_table_multi_field"
    MT_MF = "multi_table_multi_field"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"