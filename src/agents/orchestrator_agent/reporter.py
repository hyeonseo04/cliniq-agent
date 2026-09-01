"""reporter: 파이프라인 결과 → AgentAnswer. 리포 경로: src/agents/orchestrator_agent/reporter.py

리포트 문장은 LLM 없이 코드로 조립한다 (결정론적·저비용).
규칙은 SQL만이 아니라 '사람이 읽는 품질 규칙 명세' 전체를 함께 출력한다.
"""
from __future__ import annotations

from models.common import DQDimension, RuleComplexity, Severity
from models.rule import QualityRule
from models.state import AgentAnswer, PipelineState

#: Kahn 범주의 한국어 설명 (OHDSI DQD 분류 체계)
DIMENSION_KO = {
    DQDimension.CONFORMANCE: "형식·관계·표준 적합성",
    DQDimension.COMPLETENESS: "값 존재",
    DQDimension.PLAUSIBILITY: "현실적 타당성",
}
SEVERITY_KO = {Severity.ERROR: "오류", Severity.WARNING: "경고", Severity.INFO: "정보"}
COMPLEXITY_KO = {
    RuleComplexity.ST_SF: "단일테이블·단일필드",
    RuleComplexity.ST_MF: "단일테이블·다중필드",
    RuleComplexity.MT_MF: "다중테이블·다중필드",
}


def render_rule(r: QualityRule, index: int | None = None) -> str:
    """품질 규칙 1건의 사람 가독 명세."""
    head = f"[{r.rule_id}] {r.name}" if index is None else f"{index}. [{r.rule_id}] {r.name}"
    th = (
        f"위반율 {r.threshold.max_value:.1%} 이하"
        if r.threshold.type == "ratio"
        else f"위반 {int(r.threshold.max_value)}건 이하"
    )
    lines = [
        head,
        "─" * 60,
        f"설명      : {r.description}",
        f"품질 차원 : {DIMENSION_KO.get(r.dq_dimension, r.dq_dimension.value)}"
        f" ({r.dq_dimension.value})",
        f"복잡도    : {COMPLEXITY_KO.get(r.complexity, r.complexity.value)}",
        f"심각도    : {SEVERITY_KO.get(r.severity, r.severity.value)}",
        f"대상 테이블: {', '.join(r.target_tables)}",
        f"대상 컬럼 : {', '.join(r.target_columns)}",
        "",
        f"위반 조건 : {r.logic_nl}",
        f"분모(모집단): {r.denominator_nl}",
        f"분자(위반) : {r.numerator_nl}",
        f"합격 기준 : {th}",
        f"상태      : {r.status.value}",
    ]
    if r.sql:
        lines += ["", "SQL:", r.sql]
    return "\n".join(lines)


def render_pipeline_trace(state: PipelineState) -> str:
    """파이프라인 단계별 중간 결과 (Rule Gen → Dedup → Validation → SQL)."""
    L = ["", "=" * 62, "파이프라인 단계별 결과", "=" * 62]

    # 0) Schema Linking
    if state.schema_link:
        sl = state.schema_link
        L.append(f"\n[0] 입력 처리 — 대상 DB: {sl.target_db.value}")
        for t in sl.tables:
            L.append(f"    · {t.name}: {', '.join(t.columns) or '(브리지)'}  ← {t.reason}")
        if sl.join_path:
            L.append("    JOIN 경로:")
            L += [f"      {e.to_sql()}" for e in sl.join_path]

    # 1) Rule Generation
    L.append(f"\n[1] Rule Generation — 생성 {len(state.trace_generated)}건"
             + (f", 구조검사 탈락 {len(state.trace_rejected)}건" if state.trace_rejected else ""))
    for r in state.trace_generated:
        L.append(f"    · [{r.rule_id}] {r.name} ({r.dq_dimension.value})")
    for x in state.trace_rejected:
        L.append(f"    ✗ 탈락: {x}")

    # 2) Deduplicate
    if state.dedup:
        d = state.dedup
        L.append(f"\n[2] Deduplicate — 유지 {len(d.unique_rules)}건, 제거 {len(d.removals)}건")
        for r in d.unique_rules:
            L.append(f"    · [{r.rule_id}] {r.name}")
        for m in d.removals:
            L.append(f"    ✗ {m.removed_rule_id} ≈ {m.kept_rule_id} (유사도 {m.similarity:.3f}): {m.inspector_reason}")

    # 3) SQL Generation
    if state.static_checks:
        ok = [c for c in state.static_checks if c.passed]
        L.append(f"\n[3] SQL Generation — 정적검사 통과 {len(ok)}건 / 시도 {len(state.static_checks)}회")
        for c in state.static_checks:
            if c.passed:
                L.append(f"    ✓ {c.rule_id} 통과")
            else:
                detail = c.parse_error or f"unknown_tables={c.unknown_tables}, unknown_columns={c.unknown_columns}"
                L.append(f"    ✗ {c.rule_id} 실패 → 재생성: {detail}")

    # 4) Execution (DB 검증)
    if state.executions:
        ok = [e for e in state.executions if e.success]
        has_ratio = any(e.violation_ratio is not None for e in state.executions)
        label = "실행 검증" if has_ratio else "DB 검증 (EXPLAIN)"
        L.append(f"\n[4] {label} — 통과 {len(ok)}건 / {len(state.executions)}건")
        for e in state.executions:
            if e.success:
                extra = ""
                if e.violation_ratio is not None:
                    extra = (f"  위반 {e.violation_count}/{e.denominator_count}"
                             f" ({e.violation_ratio:.2%})")
                L.append(f"    ✓ {e.rule_id} 통과 ({e.elapsed_ms}ms){extra}")
            else:
                L.append(f"    ✗ {e.rule_id} 실패: {(e.error_message or '')[:100]}")

    # 5) Refine
    if state.refine_decisions:
        L.append(f"\n[5] Refine — 복구 시도 {state.refine_count}회")
        for d in state.refine_decisions:
            tag = "자체 수정" if d.fixed_sql else f"회귀 → {d.route_to}"
            L.append(f"    · {d.rule_id} [{d.cause.value}] {tag}: {d.feedback[:90]}")

    # 단계별 소요 시간
    if state.stage_timings:
        order = ["input", "rule_generation", "deduplicate", "sql_generation",
                 "execution", "refine"]
        ko = {"input": "입력 처리", "rule_generation": "규칙 생성",
              "deduplicate": "중복 제거", "sql_generation": "SQL 생성",
              "execution": "실행 검증", "refine": "복구"}
        total = sum(state.stage_timings.values())
        L.append(f"\n[소요 시간] 총 {total:.1f}초")
        for k in order:
            v = state.stage_timings.get(k)
            if v is None:
                continue
            share = v / total if total else 0
            L.append(f"    {ko[k]:10s} {v:6.2f}초  {'▏' * max(1, round(share * 24))}")

    L.append("=" * 62)
    return "\n".join(L)


def report_creative(state: PipelineState) -> AgentAnswer:
    if state.aborted:
        lines = [f"규칙 생성에 실패했습니다.", f"사유: {state.abort_reason}"]
        if state.dedup and state.dedup.removals:
            lines.append("")
            lines.append("중복 제거 내역:")
            lines += [
                f"- {m.removed_rule_id} → 기존 {m.kept_rule_id} 와 동치"
                f" (유사도 {m.similarity:.3f}): {m.inspector_reason}"
                for m in state.dedup.removals
            ]
        lines.append(render_pipeline_trace(state))
        return AgentAnswer(answer="\n".join(lines))

    rules = state.generated_rules
    execs = {e.rule_id: e for e in state.executions}
    verified = any(e.violation_ratio is not None for e in state.executions)
    head = "SQL 정적검사 및 실행 검증 통과" if verified else "SQL 정적검사 통과"
    lines = [f"품질 규칙 {len(rules)}건을 생성했습니다. ({head})", ""]
    for i, r in enumerate(rules, start=1):
        lines.append(render_rule(r, index=i))
        e = execs.get(r.rule_id)
        if e and e.violation_ratio is not None:
            verdict = "합격" if e.passed_threshold else "불합격"
            lines.append(
                f"\n실행 결과 : 위반 {e.violation_count:,}건 / "
                f"모집단 {e.denominator_count:,}건 "
                f"= {e.violation_ratio:.2%}  → {verdict}"
            )
        lines.append("")

    notes = []
    if state.dedup and state.dedup.removals:
        notes.append(
            f"중복 제거 {len(state.dedup.removals)}건: "
            + ", ".join(
                f"{m.removed_rule_id}(≈{m.kept_rule_id})" for m in state.dedup.removals
            )
        )
    retried_sql = [c for c in state.static_checks if not c.passed]
    if retried_sql:
        notes.append(f"SQL 정적검사 재시도 {len(retried_sql)}건")
    # 실행 검증에서 탈락한 규칙도 사유와 함께 알린다 (부분 성공 시)
    kept = {r.rule_id for r in rules}
    dropped = [e for e in state.executions if not e.success and e.rule_id not in kept]
    if dropped:
        notes.append(
            f"실행 검증 탈락 {len(dropped)}건: "
            + ", ".join(f"{e.rule_id}({(e.error_message or '')[:30]})" for e in dropped)
        )

    if notes:
        lines.append("― 처리 내역 ―")
        lines += [f"· {n}" for n in notes]

    lines.append(render_pipeline_trace(state))
    return AgentAnswer(answer="\n".join(lines).rstrip(), generated_rules=rules)