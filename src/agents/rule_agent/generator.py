"""규칙 생성기. 리포 경로: src/agents/rule_agent/generator.py

SchemaLinkResult → list[QualityRule].
LLM은 규칙의 '내용'만 생성하고, 다음은 코드가 결정한다 (원칙: 사실은 코드가):
  - rule_id 부여, complexity 산정, target_db 주입
  - v1은 모든 규칙을 레코드 단위로 고정 (grain 개념 미사용)
  - 구조 검사: 참조 테이블/컬럼이 schema_context에 실제 존재하는지 대조
    → 위반 규칙은 제외하고 사유를 rejected에 기록 (재생성 피드백으로 사용 가능)
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from core.llm import LLM
from models.common import DQDimension, RuleComplexity, Severity
from models.intent import ASPECT_KO, IntentResult
from models.rule import QualityRule, RuleStatus, Threshold
from models.schema_link import SchemaLinkResult
from prompts import rule_generation as P
from core.dqd_catalog import DQDCatalog, get_dqd_catalog
from prompts.dqd_reference import DQD_REFERENCE

log = logging.getLogger(__name__)

MODE_KO = {
    "basic": "기초 지표 (단일 테이블 — 결측·값 범위·형식·코드 도메인·중복 중심)",
    "advanced": "심화 지표 (다중 테이블 — 테이블 간 날짜 선후·참조 정합·논리 모순 중심)",
}


class _GenRule(BaseModel):
    """LLM 출력 전용 — 코드가 채우는 필드(rule_id 등)는 제외."""

    name: str
    description: str
    dq_dimension: DQDimension
    target_tables: list[str]
    target_columns: list[str] = Field(description='"table.column" 형식')
    logic_nl: str
    denominator_nl: str
    numerator_nl: str
    severity: Severity = Severity.ERROR
    threshold: Threshold | None = None


class GenOutput(BaseModel):
    rules: list[_GenRule]


def _complexity(tables: list[str], columns: list[str]) -> RuleComplexity:
    if len(set(tables)) > 1:
        return RuleComplexity.MT_MF
    if len(set(columns)) > 1:
        return RuleComplexity.ST_MF
    return RuleComplexity.ST_SF


def generate_rules(
    *,
    intent: IntentResult,
    schema_link: SchemaLinkResult,
    llm: LLM,
    feedback: str | None = None,
    id_prefix: str = "R",
    catalog: DQDCatalog | None = None,
) -> tuple[list[QualityRule], list[str]]:
    """Returns: (valid_rules, rejected_reasons)

    catalog가 있으면 지목된 컬럼에 적용 가능한 표준 지표(임계값 포함)를 주입한다.
    조회 결과가 없으면 일반 체크 유형 정의(DQD_REFERENCE)로 폴백한다.
    """
    catalog = catalog or get_dqd_catalog()
    targets = [(t.table, t.columns) for t in intent.targets]
    aspects = [a.value for a in intent.aspects]
    dqd_text = catalog.render_for_prompt(targets, aspects) or DQD_REFERENCE
    schema_text = "\n\n".join(
        _render_table(t) for t in schema_link.schema_context
    )
    joins = "\n".join(e.to_sql() for e in schema_link.join_path) or "(단일 테이블)"

    out = llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(
            request=intent.request_summary,
            aspects=", ".join(ASPECT_KO.get(a, a.value) for a in intent.aspects) or "(미지정)",
            mode=MODE_KO[intent.mode],
            filters="\n".join(schema_link.filters_hint) or "(없음)",
            dqd_reference=dqd_text,
            schema=schema_text,
            joins=joins,
            feedback=P.FEEDBACK_TEMPLATE.format(feedback=feedback) if feedback else "",
        ),
        GenOutput,
        thinking=True,  # 규칙 도출은 추론이 도움됨 (Qwen3 thinking on)
    )

    known: dict[str, set[str]] = {
        t.table_name: t.field_names() for t in schema_link.schema_context
    }

    rules: list[QualityRule] = []
    rejected: list[str] = []
    for i, r in enumerate(out.rules, start=1):
        _normalize_columns(r, known)  # "col" → "table.col" 자동 보정
        err = _structure_check(r, known)
        if err:
            rejected.append(f"{r.name}: {err}")
            log.warning("rule rejected (structure): %s — %s", r.name, err)
            continue

        table_key = r.target_tables[0].upper()[:4]
        rules.append(
            QualityRule(
                rule_id=f"{id_prefix}-{table_key}-{i:03d}",
                name=r.name,
                description=r.description,
                dq_dimension=r.dq_dimension,
                complexity=_complexity(r.target_tables, r.target_columns),
                target_db=schema_link.target_db,
                target_tables=[t.lower() for t in r.target_tables],
                target_columns=[c.lower() for c in r.target_columns],
                logic_nl=r.logic_nl,
                denominator_nl=r.denominator_nl,
                numerator_nl=r.numerator_nl,
                severity=r.severity,
                threshold=r.threshold or Threshold(),
                status=RuleStatus.DRAFT,
            )
        )
    return rules, rejected


def _normalize_columns(r: _GenRule, known: dict[str, set[str]]) -> None:
    """target_columns를 'table.column' 형식으로 정규화.

    LLM이 테이블 접두사를 빠뜨리는 경우가 있다. 컬럼명만으로 소속 테이블을
    유일하게 특정할 수 있으면 코드가 보정한다 (형식 위반으로 규칙을 버리지 않도록).
    """
    fixed: list[str] = []
    for c in r.target_columns:
        c = c.strip()
        if "." in c:
            fixed.append(c.lower())
            continue
        col = c.lower()
        owners = [t for t, cols in known.items() if col in cols]
        if len(owners) == 1:
            fixed.append(f"{owners[0]}.{col}")
        elif len(owners) > 1 and r.target_tables:
            # 여러 테이블에 있으면 규칙의 대상 테이블 중에서 고른다
            for t in r.target_tables:
                if t.lower() in owners:
                    fixed.append(f"{t.lower()}.{col}")
                    break
            else:
                fixed.append(c)
        else:
            fixed.append(c)
    r.target_columns = fixed


def _structure_check(r: _GenRule, known: dict[str, set[str]]) -> str | None:
    """참조 무결성 검사. 통과 시 None, 실패 시 사유."""
    for t in r.target_tables:
        if t.lower() not in known:
            return f"unknown table '{t}'"
    for c in r.target_columns:
        if "." not in c:
            return f"column '{c}' must be 'table.column' format"
        t, col = c.lower().split(".", 1)
        if t not in known:
            return f"unknown table in column ref '{c}'"
        if col not in known[t]:
            return f"unknown column '{c}'"
    if not r.target_tables or not r.target_columns:
        return "empty target_tables/columns"
    # 이름이 영어 식별자 형태면 거부 (한국어 서술형 요구)
    if re.fullmatch(r"[A-Za-z0-9_]+", r.name.strip()):
        return f"name '{r.name}' must be a Korean descriptive phrase, not an identifier"
    return None


def _render_table(t) -> str:
    lines = [f"# TABLE {t.table_name}", t.table_description.strip()]
    for f in t.fields:
        flags = []
        if f.primary_key:
            flags.append("PK")
        if f.foreign_key:
            flags.append(f"FK→{f.foreign_key.reference_table}")
        flags.append("NULLABLE" if f.nullable else "NOT NULL")
        lines.append(f"- {f.name} ({f.type}) [{', '.join(flags)}]")
    return "\n".join(lines)