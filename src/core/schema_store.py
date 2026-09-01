"""cdm_schema.json → models.TableSchema 로더 겸 저장소.

세 DB(통합/전처리/연계)가 모두 OMOP v5.3 동일 구조이므로 인스턴스 하나를 공용으로 쓴다.
스키마 파일이 실DB DDL 기준으로 교체되어도 포맷만 같으면 코드 무변경.

역할:
  - 테이블/컬럼 조회 및 존재 검증 (SQL 정적검사, Rule 구조검사에서 사용)
  - Rule Generation 프롬프트용 9요소 포맷 렌더링
  - RAG 인덱스 구축용 문서 생성 (테이블 단위 + 컬럼 단위)
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from models.schema_link import TableSchema

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "integrated" / "schema" / "cdm_schema.json"
)


class SchemaStore:
    def __init__(self, schema_path: str | Path = DEFAULT_SCHEMA_PATH):
        raw = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        self._tables: dict[str, TableSchema] = {}
        for tname, payload in raw.items():
            # {"person": {"schema": {...}}} / {"person": {...}} 두 형태 모두 수용
            body = payload.get("schema", payload)
            self._tables[tname.lower()] = TableSchema(**body)

    # ---------- 조회 ----------
    def table_names(self) -> list[str]:
        return sorted(self._tables)

    def has_table(self, name: str) -> bool:
        return name.lower() in self._tables

    def get(self, name: str) -> TableSchema:
        key = name.lower()
        if key not in self._tables:
            raise KeyError(f"unknown table: {name}")
        return self._tables[key]

    def has_column(self, table: str, column: str) -> bool:
        return self.has_table(table) and column.lower() in self.get(table).field_names()

    def unknown_columns(self, table: str, columns: list[str]) -> list[str]:
        """SQL/규칙 정적검사용: 존재하지 않는 컬럼 목록 반환."""
        if not self.has_table(table):
            return list(columns)
        known = self.get(table).field_names()
        return [c for c in columns if c.lower() not in known]

    # ---------- 프롬프트 렌더링 ----------
    def render_for_prompt(self, table: str, columns: list[str] | None = None) -> str:
        """LLM-DQR 9요소 포맷의 컴팩트 텍스트. columns 지정 시 축약(PK/FK 유지)."""
        t = self.get(table)
        if columns:
            t = t.subset(columns)
        lines = [f"# TABLE {t.table_name}", t.table_description.strip(), ""]
        for f in t.fields:
            flags = []
            if f.primary_key:
                flags.append("PK")
            if f.foreign_key:
                flags.append(
                    f"FK→{f.foreign_key.reference_table}.{f.foreign_key.reference_field}"
                )
            flags.append("NULLABLE" if f.nullable else "NOT NULL")
            desc = " ".join(f.description.split())  # 줄바꿈 정리
            sample = f"  samples={f.samples}" if f.samples else ""
            lines.append(f"- {f.name} ({f.type}) [{', '.join(flags)}] {desc}{sample}")
        return "\n".join(lines)

    # ---------- RAG 문서 ----------
    def rag_documents(self) -> list[dict]:
        """Chroma 인덱스용 문서. 테이블 단위 + 컬럼 단위 이중 청크.

        반환: [{"id": ..., "text": ..., "metadata": {...}}, ...]
        한글 동의어는 build_rag_index.py에서 synonyms.json으로 주입한다.
        """
        docs: list[dict] = []
        for t in self._tables.values():
            docs.append(
                {
                    "id": f"table::{t.table_name}",
                    "text": f"{t.table_name}: {t.table_description}",
                    "metadata": {"kind": "table", "table": t.table_name},
                }
            )
            for f in t.fields:
                if not f.description:
                    continue
                docs.append(
                    {
                        "id": f"column::{t.table_name}.{f.name}",
                        "text": f"{t.table_name}.{f.name} ({f.type}): {f.description}",
                        "metadata": {
                            "kind": "column",
                            "table": t.table_name,
                            "column": f.name,
                        },
                    }
                )
        return docs


@lru_cache(maxsize=1)
def get_schema_store() -> SchemaStore:
    """기본 경로 저장소 싱글턴."""
    return SchemaStore()