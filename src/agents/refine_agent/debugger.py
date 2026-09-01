"""LLM SQL Debugger (LLM-DQR Algorithm 2).
리포 경로: src/agents/refine_agent/debugger.py

원본 SQL + 오류 메시지 + 스키마를 주고 수정된 SQL을 받는다.
수정본은 반드시 정적검사를 다시 통과해야 채택된다.
"""
from __future__ import annotations

from pydantic import BaseModel

from core.llm import LLM
from models.rule import QualityRule
from models.schema_link import SchemaLinkResult
from prompts import refine as P


class _FixedSQL(BaseModel):
    sql: str


def debug_sql(
    rule: QualityRule,
    error: str,
    schema_link: SchemaLinkResult,
    llm: LLM,
) -> str:
    schema_text = "\n\n".join(
        f"# {t.table_name}\n" + "\n".join(f"- {f.name} ({f.type})" for f in t.fields)
        for t in schema_link.schema_context
    )
    joins = "\n".join(e.to_sql() for e in schema_link.join_path) or "(단일 테이블)"
    rule_text = (
        f"name: {rule.name}\nlogic: {rule.logic_nl}\n"
        f"denominator: {rule.denominator_nl}\nnumerator: {rule.numerator_nl}"
    )

    out = llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(
            rule=rule_text,
            sql=rule.sql or "(없음)",
            error=error,
            schema=schema_text,
            joins=joins,
        ),
        _FixedSQL,
    )
    return out.sql.strip().rstrip(";")