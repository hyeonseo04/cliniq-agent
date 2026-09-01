"""Schema Linking 에이전트 진입 클래스.
리포 경로: src/agents/schema_linking_agent/schema_linking_agent.py

입력: IntentResult (Orchestrator planner 출력)
출력: SchemaLinkResult (Rule Generation 입력)

의존성은 전부 생성자 주입 → mock으로 단독 테스트 가능 (설계 원칙 §5-1).
"""
from __future__ import annotations

from core.fk_graph import FKGraph, get_fk_graph
from core.llm import LLM, get_llm
from core.schema_store import SchemaStore, get_schema_store
from models.common import TargetDB
from models.intent import IntentResult
from models.schema_link import SchemaLinkResult

from .linker import refine_and_link
from .retriever import SchemaRetriever


class SchemaLinkingAgent:
    def __init__(
        self,
        retriever: SchemaRetriever,
        store: SchemaStore | None = None,
        graph: FKGraph | None = None,
        llm: LLM | None = None,
    ):
        self.retriever = retriever
        self.store = store or get_schema_store()
        self.graph = graph or get_fk_graph()
        self.llm = llm or get_llm()

    def run(self, intent: IntentResult) -> SchemaLinkResult:
        # 검색 쿼리 = 재작성된 요청 + 도메인 힌트 (동의어 매칭 강화)
        query = intent.cleaned_request
        if intent.domain_hints:
            query += " " + " ".join(intent.domain_hints)

        candidates = self.retriever.search(query)
        if not candidates:
            raise ValueError(f"no candidate tables retrieved for: {query!r}")

        return refine_and_link(
            request=intent.cleaned_request,
            dimensions=[d.value for d in intent.dq_dimensions],
            candidates=candidates,
            target_db=intent.target_db or TargetDB.INTEGRATED,
            store=self.store,
            graph=self.graph,
            llm=self.llm,
        )