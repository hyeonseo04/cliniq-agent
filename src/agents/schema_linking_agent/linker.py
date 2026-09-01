"""LLM 후보 정제 + 결정론적 검증. 리포 경로: src/agents/schema_linking_agent/linker.py

흐름:
  1) 후보 테이블의 축약 스키마를 렌더링해 LLM에 제시 → 필요한 테이블/컬럼 선택
     (LinkerLLMOutput — 이 에이전트 내부 전용 모델, 에이전트 간 I/O 아님)
  2) 코드 검증: 존재하지 않는 테이블/컬럼 제거 (LLM 환각 방어)
  3) FK 그래프 connect_tables()로 JOIN 경로·브리지 결정론적 확정
  4) SchemaLinkResult 조립 (브리지 테이블도 schema_context에 포함)
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from core.fk_graph import FKGraph
from core.llm import LLM
from core.schema_store import SchemaStore
from models.common import TargetDB
from models.schema_link import SchemaLinkResult, SelectedTable
from prompts import schema_linking as P

from .retriever import TableCandidate


class _SelectedTableLLM(BaseModel):
    name: str
    reason: str
    columns: list[str] = Field(default_factory=list)


class LinkerLLMOutput(BaseModel):
    """linker 내부 전용 — LLM structured output 스키마."""

    tables: list[_SelectedTableLLM]
    filters_hint: list[str] = Field(default_factory=list)


def render_candidates(store: SchemaStore, candidates: list[TableCandidate]) -> str:
    """후보별 축약 스키마 텍스트. matched_columns가 있으면 해당 컬럼 위주로 축약."""
    blocks = []
    for c in candidates:
        cols = c.matched_columns or None
        try:
            block = store.render_for_prompt(c.table, cols)
        except KeyError:
            continue
        blocks.append(f"[score={c.score:.2f}]\n{block}")
    return "\n\n".join(blocks)


def refine_and_link(
    *,
    request: str,
    dimensions: list[str],
    candidates: list[TableCandidate],
    target_db: TargetDB,
    store: SchemaStore,
    graph: FKGraph,
    llm: LLM,
) -> SchemaLinkResult:
    # 1) LLM 후보 정제
    out = llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(
            request=request,
            dimensions=", ".join(dimensions) or "(미지정)",
            candidates=render_candidates(store, candidates),
        ),
        LinkerLLMOutput,
    )

    # 2) 코드 검증 — 환각 테이블/컬럼 제거
    selected: list[SelectedTable] = []
    for t in out.tables:
        name = t.name.lower()
        if not store.has_table(name):
            continue
        known = store.get(name).field_names()
        cols = [c.lower() for c in t.columns if c.lower() in known]
        selected.append(SelectedTable(name=name, reason=t.reason, columns=cols))

    if not selected:
        raise ValueError(
            f"schema linking failed: no valid table selected for request={request!r}"
        )

    # 3) JOIN 경로 결정론적 확정 + 브리지 추가
    table_names = [t.name for t in selected]
    edges, bridges, is_connected = graph.connect_tables(table_names)
    for b in bridges:
        selected.append(
            SelectedTable(name=b, reason="JOIN 경로 연결용 브리지 테이블", columns=[])
        )

    # 4) schema_context 조립 (subset이 PK/FK는 항상 유지)
    schema_context = [store.get(t.name).subset(t.columns) for t in selected]

    return SchemaLinkResult(
        target_db=target_db,
        tables=selected,
        join_path=edges,
        filters_hint=out.filters_hint,
        schema_context=schema_context,
        is_connected=is_connected,
    )