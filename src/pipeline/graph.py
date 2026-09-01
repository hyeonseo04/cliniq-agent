"""creative 분기 파이프라인 (LangGraph state machine).
리포 경로: src/pipeline/graph.py

다이어그램과 1:1 대응:
  input → rule_generation → deduplicate → sql_generation
      → [execution → refine : Phase 4] → END

Rule Validation(LLM 의미 검증) 단계는 제거되었다. 사유:
  - 판정관이 오작동해 정상 규칙을 반복 불합격시키는 사례가 확인됨
  - 불합격이 곧 폐기가 되어 사용자가 결과를 전혀 받지 못하는 문제 발생
  - 규칙의 의미 검증은 실행 검증(Phase 4)과 사용자 확인으로 대체

Phase 3 범위에서는 sql_generation(정적검사 포함)까지 실행되고,
execution/refine 노드는 Phase 4에서 이 그래프에 추가한다 (자리 표시 주석 참조).

상태는 models.state.PipelineState (Pydantic) 하나로 관리한다.
"""
from __future__ import annotations

import logging
import time
from functools import wraps

from langgraph.graph import END, StateGraph

from agents.deduplicate_agent import DeduplicateAgent
from agents.rule_agent import RuleAgent
from agents.execution_agent import ExecutionAgent
from agents.input_agent import InputAgent
from agents.refine_agent import RefineAgent
from agents.sql_agent import SQLAgent
from core.catalog import RuleCatalog
from models.rule import RuleStatus
from models.state import PipelineState

log = logging.getLogger(__name__)


def self_can_refine(state: PipelineState) -> bool:
    return state.can_refine()


def _timed(node_name: str):
    """노드 실행 시간을 state.stage_timings에 누적한다.

    재진입하는 노드(refine 루프의 execution 등)는 시간이 합산되므로,
    총합이 파이프라인 전체 소요와 일치한다.
    """

    def deco(fn):
        @wraps(fn)
        def wrapper(self, state: PipelineState) -> dict:
            t0 = time.perf_counter()
            update = fn(self, state)
            elapsed = time.perf_counter() - t0
            timings = dict(state.stage_timings)
            timings[node_name] = round(timings.get(node_name, 0.0) + elapsed, 3)
            update["stage_timings"] = timings
            return update

        return wrapper

    return deco


class CreativePipeline:
    """에이전트 주입 → LangGraph 컴파일. run(state) 한 번으로 전체 실행."""

    def __init__(
        self,
        input_agent: InputAgent,
        rule: RuleAgent,
        dedup: DeduplicateAgent,
        sql: SQLAgent,
        execution: ExecutionAgent | None = None,
        refine: RefineAgent | None = None,
        catalog: RuleCatalog | None = None,
    ):
        self.input_agent = input_agent
        self.rule = rule
        self.dedup = dedup
        self.sql = sql
        self.execution = execution
        self.refine = refine
        self.catalog = catalog or RuleCatalog()
        self.graph = self._build()

    # ---------- 노드 ----------
    @_timed("input")
    def _node_input(self, state: PipelineState) -> dict:
        try:
            return {"schema_link": self.input_agent.run(state.intent)}
        except (ValueError, KeyError) as e:
            return {"aborted": True, "abort_reason": f"input: {e}"}

    @_timed("rule_generation")
    def _node_rule_generation(self, state: PipelineState) -> dict:
        rules, rejected = self.rule.run(state.intent, state.schema_link)
        if not rules:
            return {
                "aborted": True,
                "abort_reason": f"rule_generation: 유효 규칙 없음 ({'; '.join(rejected) or '생성 실패'})",
            }
        return {
            "generated_rules": rules,
            "trace_generated": rules,
            "trace_rejected": rejected,
        }

    @_timed("deduplicate")
    def _node_deduplicate(self, state: PipelineState) -> dict:
        existing = self.catalog.search(target_db=state.intent.target_db)
        result = self.dedup.run(state.generated_rules, existing_rules=existing)
        if not result.unique_rules:
            return {
                "dedup": result,
                "aborted": True,
                "abort_reason": "deduplicate: 모든 규칙이 기존 규칙과 중복",
            }
        # 중복 제거 결과를 이후 단계에 반영한다
        # (generated_rules를 갱신하지 않으면 제거된 규칙이 SQL 생성·리포트까지 살아남는다)
        return {"dedup": result, "generated_rules": result.unique_rules}

    @_timed("sql_generation")
    def _node_sql_generation(self, state: PipelineState) -> dict:
        ok_rules, checks = self.sql.run(state.generated_rules, state.schema_link)
        update: dict = {"static_checks": checks}
        if not ok_rules:
            update["aborted"] = True
            update["abort_reason"] = "sql_generation: 정적검사 통과 SQL 없음"
        else:
            update["generated_rules"] = ok_rules
        return update

    @_timed("execution")
    def _node_execution(self, state: PipelineState) -> dict:
        """DB 검증 — EXPLAIN 또는 실제 실행. execution 미주입 시 배선되지 않는다."""
        results = self.execution.run(state.generated_rules)
        ok_ids = {r.rule_id for r in results if r.success}
        update: dict = {"executions": state.executions + results}

        if ok_ids:
            # 통과한 규칙만 남기고 EXECUTABLE로 승격.
            # 일부만 통과해도 그 결과는 사용자에게 유효하므로 중단하지 않는다.
            update["generated_rules"] = [
                r.model_copy(update={"status": RuleStatus.EXECUTABLE})
                for r in state.generated_rules
                if r.rule_id in ok_ids
            ]
        elif self.refine is None or not state.can_refine():
            update["aborted"] = True
            update["abort_reason"] = "execution: DB 검증을 통과한 SQL 없음"
        return update

    @_timed("refine")
    def _node_refine(self, state: PipelineState) -> dict:
        """실패한 규칙에 대해 원인 분류 + SQL 자체 수정 (LLM-DQR Algorithm 2)."""
        failed = {e.rule_id: e for e in state.executions if not e.success}
        if not failed:
            return {}

        by_id = {r.rule_id: r for r in state.generated_rules}
        decisions: list = []
        repaired: list = []

        for rule_id, exec_result in failed.items():
            rule = by_id.get(rule_id)
            if rule is None:
                continue
            d = self.refine.run(
                rule, exec_result, state.schema_link, state.refine_count + 1
            )
            decisions.append(d)
            if d.fixed_sql:
                repaired.append(rule.model_copy(update={"sql": d.fixed_sql}))

        update: dict = {
            "refine_decisions": state.refine_decisions + decisions,
            "refine_count": state.refine_count + 1,
        }
        if repaired:
            update["generated_rules"] = repaired
            return update

        update["aborted"] = True
        # 분모 0은 SQL·규칙이 아니라 '데이터에 해당 값이 없다'는 뜻이므로
        # 재생성해도 결과가 같다. 사용자에게 원인을 안내한다.
        if any("분모가 0" in (e.error_message or "") for e in failed.values()):
            cols = ", ".join(sorted({
                c for r in by_id.values() for c in r.target_columns
            }))
            update["abort_reason"] = (
                f"평가 대상 데이터가 없습니다. 지목한 컬럼({cols})의 값이 "
                "모두 비어 있거나, JOIN 조건을 만족하는 레코드가 없습니다. "
                "다른 컬럼을 지정하거나 데이터 적재 상태를 확인하세요."
            )
        else:
            update["abort_reason"] = (
                "자동 수정 가능한 오류가 없음 — "
                + "; ".join(d.feedback for d in decisions)[:200]
            )
        return update

    # Phase 4-2에서 추가:
    #   _node_execution (execution_agent) → _node_refine (refine_agent)
    #   refine의 route_to에 따라 schema_linking/rule_generation/sql_generation으로 회귀

    # ---------- 분기 ----------
    @staticmethod
    def _after_execution(state: PipelineState) -> str:
        """복구가 필요한 경우에만 refine으로 간다.

        일부 규칙만 실패했다면 성공분을 결과로 내보내는 것이 사용자에게 유용하므로
        refine을 돌리지 않는다. 전부 실패했을 때만 복구를 시도한다.
        """
        if state.aborted:
            return "end"
        if state.generated_rules:  # 실행 검증을 통과한 규칙이 남아 있다
            return "end"
        failed = [e for e in state.executions if not e.success]
        if failed and self_can_refine(state):
            return "refine"
        return "end"

    @staticmethod
    def _abort_or(next_node: str):
        def cond(state: PipelineState) -> str:
            return "end" if state.aborted else next_node
        return cond

    # ---------- 그래프 조립 ----------
    def _build(self):
        g = StateGraph(PipelineState)
        g.add_node("input", self._node_input)
        g.add_node("rule_generation", self._node_rule_generation)
        g.add_node("deduplicate", self._node_deduplicate)
        g.add_node("sql_generation", self._node_sql_generation)
        g.set_entry_point("input")
        g.add_conditional_edges("input", self._abort_or("rule_generation"),
                                {"rule_generation": "rule_generation", "end": END})
        g.add_conditional_edges("rule_generation", self._abort_or("deduplicate"),
                                {"deduplicate": "deduplicate", "end": END})
        g.add_conditional_edges("deduplicate", self._abort_or("sql_generation"),
                                {"sql_generation": "sql_generation", "end": END})
        if self.execution is not None:
            g.add_node("execution", self._node_execution)
            g.add_conditional_edges("sql_generation", self._abort_or("execution"),
                                    {"execution": "execution", "end": END})
            if self.refine is not None:
                g.add_node("refine", self._node_refine)
                g.add_conditional_edges("execution", self._after_execution,
                                        {"refine": "refine", "end": END})
                g.add_conditional_edges("refine", self._abort_or("execution"),
                                        {"execution": "execution", "end": END})
            else:
                g.add_edge("execution", END)
        else:
            g.add_edge("sql_generation", END)
        return g.compile()

    # ---------- 실행 ----------
    def run(self, state: PipelineState) -> PipelineState:
        out = self.graph.invoke(state)
        return PipelineState.model_validate(out)