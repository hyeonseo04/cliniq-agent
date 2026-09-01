"""Orchestrator 에이전트 — 3분기 라우팅 + 슬롯 누적형 멀티턴.
리포 경로: src/agents/orchestrator_agent/orchestrator_agent.py

대화 상태(누적 슬롯)는 이 객체가 보유한다. handle()을 턴마다 호출하면:
  - 슬롯이 부족하면 되묻는 질문을 반환 (파이프라인 미실행)
  - 슬롯이 다 차면 파이프라인을 실행하고 결과를 반환 후 슬롯 초기화
"""
from __future__ import annotations

import config
from core.catalog import RuleCatalog
from core.llm import LLM, get_llm
from core.rule_store import append_catalog, save_snapshot
from core.schema_store import SchemaStore, get_schema_store
from models.common import DB_KO, SUPPORTED_DBS
from models.intent import Intent, IntentResult
from models.state import AgentAnswer, PipelineState
from pipeline.graph import CreativePipeline

from .planner import next_question, plan, validate_targets
from .reporter import report_creative

OTHERS_SYSTEM = (
    "당신은 OMOP CDM v5.3 데이터 품질 지표 생성 시스템의 안내자다. "
    "한국어로 간결하게 답한다. 시스템은 사용자가 지목한 테이블·컬럼에 대해 "
    "품질 평가 지표와 Oracle SQL을 생성한다."
)


class OrchestratorAgent:
    def __init__(
        self,
        pipeline: CreativePipeline,
        catalog: RuleCatalog | None = None,
        llm: LLM | None = None,
        store: SchemaStore | None = None,
    ):
        self.pipeline = pipeline
        self.catalog = catalog or RuleCatalog()
        self.llm = llm or get_llm()
        self.store = store or get_schema_store()
        self.slots: IntentResult | None = None  # 대화 상태

    def reset(self) -> None:
        self.slots = None

    def handle(self, query: str) -> AgentAnswer:
        intent = plan(query, self.llm, prev=self.slots)

        match intent.intent:
            case Intent.CREATIVE:
                return self._handle_creative(intent)

            case Intent.RETRIEVAL:
                rules = self.catalog.search(target_db=intent.target_db)
                if not rules:
                    return AgentAnswer(
                        answer="저장된 품질 지표가 아직 없습니다. "
                        "새 지표를 만들고 싶으시면 대상 테이블과 컬럼을 알려주세요."
                    )
                lines = [f"저장된 품질 지표 {len(rules)}건:"]
                lines += [
                    f"- [{r.rule_id}] {r.name} ({r.dq_dimension.value}, "
                    f"{','.join(r.target_tables)})"
                    for r in rules
                ]
                return AgentAnswer(answer="\n".join(lines))

            case _:
                return AgentAnswer(answer=self.llm.text(OTHERS_SYSTEM, query))

    def _handle_creative(self, intent: IntentResult) -> AgentAnswer:
        # 0) 지원 DB 검사 — 현재는 통합DB(OMOP CDM v5.3)만 지원
        if intent.target_db not in SUPPORTED_DBS:
            return AgentAnswer(
                answer=(
                    f"{DB_KO.get(intent.target_db, intent.target_db.value)}는 "
                    "아직 지원하지 않습니다.\n"
                    "현재는 OMOP CDM v5.3 구조인 통합DB만 평가할 수 있습니다. "
                    "통합DB로 진행할까요?"
                )
            )

        # 1) 지목된 이름 검증 — 틀리면 유사 후보 제안 (슬롯은 갱신하지 않음)
        intent, err = validate_targets(intent, self.store)
        if err:
            return AgentAnswer(answer=err)

        self.slots = intent

        # 2) 슬롯 부족 → 되묻기
        question = next_question(intent, self.store)
        if question:
            return AgentAnswer(answer=question)

        # 3) 슬롯 충족 → 파이프라인 실행
        state = PipelineState(user_query=intent.request_summary, intent=intent)
        final = self.pipeline.run(state)
        answer = report_creative(final)

        # 4) 생성된 규칙을 파일로 저장
        if config.SAVE_RULES and answer.generated_rules:
            try:
                path = save_snapshot(
                    answer.generated_rules,
                    request=intent.request_summary,
                    executions=final.executions,
                )
                added = append_catalog(answer.generated_rules)
                if path:
                    answer.answer += (
                        f"\n\n저장됨: {path.name} "
                        f"(카탈로그 신규 {added}건)"
                    )
            except Exception as e:  # noqa: BLE001 — 저장 실패가 결과를 막지 않도록
                answer.answer += f"\n\n[저장 실패] {e}"

        self.reset()
        return answer