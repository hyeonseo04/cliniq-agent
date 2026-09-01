"""입력 처리 에이전트 (기존 Schema Linking 대체).
리포 경로: src/agents/input_agent/input_agent.py

사용자가 테이블·컬럼을 직접 지목하므로 RAG 검색과 LLM 후보 정제가 불필요하다.
이 에이전트는 전부 결정론적 코드로 동작한다 (LLM 호출 없음):
  1) 지목된 테이블·컬럼의 스키마를 논문 9요소 포맷으로 조회
  2) 표준 지표가 요구하는 참조 테이블 자동 추가
     (예: procedure_date + 시간 타당성 → plausibleBeforeDeath가 death를 요구)
  3) 테이블이 2개 이상이면 FK 그래프로 JOIN 경로 확정 (브리지 자동 추가)
  4) 축약 스키마 조립 (지목 컬럼 + PK/FK 유지)
"""
from __future__ import annotations

from core.dqd_catalog import DQDCatalog, get_dqd_catalog
from core.fk_graph import FKGraph, get_fk_graph
from core.schema_store import SchemaStore, get_schema_store
from models.common import assert_supported
from models.intent import IntentResult
from models.schema_link import SchemaLinkResult, SelectedTable


class InputAgent:
    def __init__(
        self,
        store: SchemaStore | None = None,
        graph: FKGraph | None = None,
        catalog: DQDCatalog | None = None,
    ):
        self.store = store or get_schema_store()
        self.graph = graph or get_fk_graph()
        self.catalog = catalog or get_dqd_catalog()

    def run(self, intent: IntentResult) -> SchemaLinkResult:
        assert_supported(intent.target_db)
        selected = [
            SelectedTable(
                name=t.table,
                reason="사용자 지목",
                columns=t.columns,
            )
            for t in intent.targets
        ]

        # 표준 지표가 요구하는 참조 테이블 추가 (death, person, visit_occurrence 등)
        # 이것이 없으면 plausibleBeforeDeath 계열이 아예 생성되지 못한다.
        targets = [(t.table, t.columns) for t in intent.targets]
        aspects = [a.value for a in intent.aspects]
        for ref in self.catalog.required_tables(targets, aspects):
            if not self.store.has_table(ref):
                continue
            selected.append(
                SelectedTable(
                    name=ref,
                    reason="표준 지표가 요구하는 참조 테이블",
                    columns=[],
                )
            )

        # JOIN 경로 확정 + 브리지 자동 추가
        edges, bridges, is_connected = self.graph.connect_tables(
            [t.name for t in selected]
        )
        for b in bridges:
            selected.append(
                SelectedTable(name=b, reason="JOIN 경로 연결용 브리지 테이블", columns=[])
            )

        # 참조 테이블(사용자가 컬럼을 지목하지 않은 것)은 전체 컬럼을 유지한다.
        # subset([])은 PK/FK만 남기므로 death.death_date 같은 비교 대상 컬럼이 사라진다.
        schema_context = [
            self.store.get(t.name).subset(t.columns)
            if t.columns
            else self.store.get(t.name)
            for t in selected
        ]

        return SchemaLinkResult(
            target_db=intent.target_db,
            tables=selected,
            join_path=edges,
            filters_hint=[intent.aspect_detail] if intent.aspect_detail else [],
            schema_context=schema_context,
            is_connected=is_connected,
        )