"""SQL 생성기. 리포 경로: src/agents/sql_agent/generator.py"""
from __future__ import annotations

from pydantic import BaseModel

from core.llm import LLM
from models.rule import QualityRule
from models.schema_link import SchemaLinkResult
from prompts import sql_generation as P


class _SQLOutput(BaseModel):
    sql: str


def _render_rule(r: QualityRule) -> str:
    return (
        f"name: {r.name}\nlogic: {r.logic_nl}\n"
        f"denominator: {r.denominator_nl}\nnumerator: {r.numerator_nl}\n"
        f"tables: {r.target_tables}\ncolumns: {r.target_columns}"
    )


def generate_sql(
    rule: QualityRule,
    schema_link: SchemaLinkResult,
    llm: LLM,
    feedback: str | None = None,
) -> str:
    join_tables = {e.from_table for e in schema_link.join_path} | {
        e.to_table for e in schema_link.join_path
    }
    schema_text = "\n\n".join(
        f"# {t.table_name}\n" + "\n".join(f"- {f.name} ({f.type})" for f in t.fields)
        for t in schema_link.schema_context
        if t.table_name in rule.target_tables or t.table_name in join_tables
    )
    joins = "\n".join(e.to_sql() for e in schema_link.join_path) or "(단일 테이블)"

    out = llm.structured(
        P.SYSTEM,
        P.USER_TEMPLATE.format(
            rule=_render_rule(rule),
            joins=joins,
            schema=schema_text,
            feedback=P.FEEDBACK_TEMPLATE.format(feedback=feedback) if feedback else "",
        ),
        _SQLOutput,
    )
    return out.sql.strip().rstrip(";")