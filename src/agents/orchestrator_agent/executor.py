"""executor: 5분기 라우터. 리포 경로: src/agents/orchestrator_agent/executor.py"""
from __future__ import annotations

from core.catalog import RuleCatalog
from core.llm import LLM
from models.intent import Intent, IntentResult
from models.state import AgentAnswer, PipelineState
from pipeline.graph import CreativePipeline

from .reporter import report_creative

INFORMATIONAL_SYSTEM = (
    "당신은 OMOP CDM v5.3 전문가다. 제공된 스키마 컨텍스트를 근거로 "
    "사용자 질문에 한국어로 간결·정확하게 답한다. 컨텍스트에 없는 내용은 추측하지 않는다."
)


class Executor:
    def __init__(
        self,
        pipeline: CreativePipeline,
        catalog: RuleCatalog,
        llm: LLM,
        retriever=None,  # informational 분기 RAG 컨텍스트용 (schema_linking과 공유)
    ):
        self.pipeline = pipeline
        self.catalog = catalog
        self.llm = llm
        self.retriever = retriever

    def run(self, query: str, intent: IntentResult) -> AgentAnswer:
        if intent.needs_clarification and intent.clarification_question:
            return AgentAnswer(answer=intent.clarification_question)

        match intent.intent:
            case Intent.CREATIVE:
                state = PipelineState(user_query=query, intent=intent)
                return report_creative(self.pipeline.run(state))

            case Intent.RETRIEVAL:
                rules = self.catalog.search(target_db=intent.target_db)
                if not rules:
                    return AgentAnswer(answer="조건에 맞는 저장된 품질 규칙이 없습니다.")
                lines = [f"저장된 품질 규칙 {len(rules)}건:"]
                lines += [
                    f"- [{r.rule_id}] {r.name} ({r.dq_dimension.value}, {','.join(r.target_tables)})"
                    for r in rules
                ]
                return AgentAnswer(answer="\n".join(lines))

            case Intent.INFORMATIONAL:
                context = ""
                if self.retriever:
                    cands = self.retriever.search(intent.cleaned_request, top_n_tables=3)
                    context = "\n".join(s for c in cands for s in c.snippets)
                answer = self.llm.text(
                    INFORMATIONAL_SYSTEM,
                    f"## 스키마 컨텍스트\n{context or '(없음)'}\n\n## 질문\n{intent.cleaned_request}",
                )
                return AgentAnswer(answer=answer)

            case Intent.ANALYTICAL:
                return AgentAnswer(
                    answer="기존 규칙 실행·평가 리포트 기능은 DB 연동(Phase 4) 이후 제공됩니다."
                )

            case _:
                return AgentAnswer(
                    answer=self.llm.text(
                        "당신은 OMOP 데이터 품질 에이전트다. 간결하게 한국어로 답하라.",
                        query,
                    )
                )