"""스키마 표현 모델 + Schema Linking 에이전트 출력.

FieldInfo/TableSchema는 data/schema/cdm_schema.json 포맷과 1:1 대응하도록
설계했다 (nullable/primary_key가 "Yes"/"No" 문자열인 것까지 그대로 수용 후
bool로 정규화). 따라서 표준 OMOP v5.3 JSON이든 실DB DDL에서 뽑은 JSON이든
파일만 교체하면 코드 변경 없이 로드된다.
"""
from pydantic import BaseModel, Field, field_validator

from .common import TargetDB


def _yn_to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"yes", "y", "true", "1"}


class ForeignKeyInfo(BaseModel):
    reference_table: str
    reference_field: str
    domain_info: str | None = None  # 예: "[domain.domain_name] = Gender"


class FieldInfo(BaseModel):
    """컬럼 하나. LLM-DQR 논문의 9요소 중 필드 레벨 요소를 담는다."""

    name: str
    type: str
    description: str = ""
    nullable: bool = True
    primary_key: bool = False
    foreign_key: ForeignKeyInfo | None = None
    samples: list = Field(
        default_factory=list,
        description="대표값 예시. 논문에서 강조한 Sample Data 요소 — "
        "실DB 연동 전에는 OMOP 예시값으로 채운다",
    )

    # cdm_schema.json의 "Yes"/"No" 문자열을 bool로 정규화
    _v_nullable = field_validator("nullable", mode="before")(_yn_to_bool)
    _v_pk = field_validator("primary_key", mode="before")(_yn_to_bool)


class TableSchema(BaseModel):
    """테이블 하나의 전체 스키마 (논문 9요소 포맷)."""

    table_name: str
    table_description: str = ""
    fields: list[FieldInfo]

    def field_names(self) -> set[str]:
        return {f.name for f in self.fields}

    def subset(self, columns: list[str]) -> "TableSchema":
        """선택된 컬럼만 남긴 축약 스키마.

        항상 함께 유지되는 것:
          - PK/FK 컬럼 (JOIN·유일성 검사에 필요)
          - 선택 컬럼의 '짝' 컬럼: start↔end, date↔datetime
            (기간 검사·시간 타당성 규칙이 짝 컬럼을 참조하므로 잘라내면 안 된다)
        """
        keep = set(columns)
        for c in list(columns):
            for a, b in (("_start_", "_end_"), ("_end_", "_start_")):
                if a in c:
                    keep.add(c.replace(a, b))
            if c.endswith("_date"):
                keep.add(c + "time")
            elif c.endswith("_datetime"):
                keep.add(c[: -len("time")])
        fields = [
            f for f in self.fields
            if f.name in keep or f.primary_key or f.foreign_key is not None
        ]
        return TableSchema(
            table_name=self.table_name,
            table_description=self.table_description,
            fields=fields,
        )


class JoinEdge(BaseModel):
    """FK 그래프의 간선 하나 = JOIN 조건 하나."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str

    def to_sql(self) -> str:
        return (
            f"{self.from_table}.{self.from_column}"
            f" = {self.to_table}.{self.to_column}"
        )


class SelectedTable(BaseModel):
    """Schema Linking이 선택한 테이블 + 선택 근거."""

    name: str
    reason: str = Field(description="이 테이블이 필요한 이유 (한 문장)")
    columns: list[str] = Field(description="요청과 관련된 컬럼명 목록")


class SchemaLinkResult(BaseModel):
    """Schema Linking 에이전트의 최종 출력.

    - tables/join_path: LLM 정제 + FK그래프 결정론적 탐색의 결과
    - grain(평가 단위)은 여기서 판정하지 않는다 — 규칙 의미를 아는 Rule Generation이 결정
    - schema_context: Rule Generation 프롬프트에 그대로 넣을 축약 스키마
      (선택 테이블만, 9요소 포맷 유지)
    """

    target_db: TargetDB
    tables: list[SelectedTable]
    join_path: list[JoinEdge] = Field(
        default_factory=list,
        description="FK 그래프 최단경로 탐색 결과. 단일 테이블이면 빈 리스트",
    )
    filters_hint: list[str] = Field(
        default_factory=list,
        description="요청에서 읽어낸 조건 힌트. 예: ['value_as_number < 0']",
    )
    schema_context: list[TableSchema] = Field(
        default_factory=list,
        description="선택 테이블의 축약 스키마 — Rule Generation 입력용",
    )
    is_connected: bool = Field(
        default=True,
        description="선택 테이블들이 FK 그래프에서 연결되는지. "
        "False면 브리지 테이블 자동 추가 실패 → 재검토 필요",
    )