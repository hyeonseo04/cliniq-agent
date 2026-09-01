"""planner: 슬롯 누적형 대화 관리.
리포 경로: src/agents/orchestrator_agent/planner.py

매 턴 (이전 슬롯 + 새 발화) → 갱신된 슬롯을 반환한다.
LLM이 뽑은 테이블·컬럼명은 코드가 스키마와 대조해 검증하고,
틀린 이름은 유사 후보를 제안한다.
"""
from __future__ import annotations

import difflib

from core.llm import LLM
from core.schema_store import SchemaStore
from models.intent import ASPECT_KO, IntentResult, TargetSpec
from prompts import planner as P


def _render_slots(prev: IntentResult | None) -> str:
    if prev is None:
        return "(없음 — 첫 발화)"
    lines = [f"- intent: {prev.intent.value}", f"- target_db: {prev.target_db.value}"]
    if prev.targets:
        for t in prev.targets:
            lines.append(f"- 대상: {t.table} / 컬럼: {t.columns or '(미지정)'}")
    else:
        lines.append("- 대상: (미지정)")
    lines.append(
        "- 관점: "
        + (", ".join(ASPECT_KO.get(a, a.value) for a in prev.aspects) or "(미지정)")
    )
    return "\n".join(lines)


def plan(query: str, llm: LLM, prev: IntentResult | None = None) -> IntentResult:
    return llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(current_slots=_render_slots(prev), query=query),
        IntentResult,
    )


def validate_targets(
    intent: IntentResult, store: SchemaStore
) -> tuple[IntentResult, str | None]:
    """지목된 테이블·컬럼이 실재하는지 검증.

    Returns:
        (검증된 intent, 오류 메시지 or None)
        오류가 있으면 유사 이름 후보를 제안하는 메시지를 반환한다.
    """
    valid: list[TargetSpec] = []
    for t in intent.targets:
        table = t.table.lower()
        if not store.has_table(table):
            cands = difflib.get_close_matches(table, store.table_names(), n=3, cutoff=0.5)
            return intent, P.SUGGEST_NAME.format(
                wrong=t.table,
                scope="OMOP CDM v5.3",
                candidates="\n".join(f"- {c}" for c in cands) or "- (유사한 테이블 없음)",
            )

        known = store.get(table).field_names()
        bad = [c for c in t.columns if c.lower() not in known]
        if bad:
            cands = difflib.get_close_matches(bad[0].lower(), sorted(known), n=3, cutoff=0.5)
            return intent, P.SUGGEST_NAME.format(
                wrong=bad[0],
                scope=f"{table} 테이블",
                candidates="\n".join(f"- {c}" for c in cands) or "- (유사한 컬럼 없음)",
            )

        valid.append(TargetSpec(table=table, columns=[c.lower() for c in t.columns]))

    return intent.model_copy(update={"targets": valid}), None


def next_question(intent: IntentResult, store: SchemaStore) -> str | None:
    """비어 있는 슬롯에 대한 되묻기 문구. 슬롯이 다 차면 None."""
    missing = intent.missing_slots()
    if not missing:
        return None
    if "table" in missing:
        return P.ASK_TABLE
    if "column" in missing:
        t = next(t for t in intent.targets if not t.columns)
        cols = "\n".join(
            f"- {f.name} ({f.type}) {' '.join(f.description.split())[:50]}"
            for f in store.get(t.table).fields
        )
        return P.ASK_COLUMN.format(table=t.table, columns=cols)
    return P.ASK_ASPECT