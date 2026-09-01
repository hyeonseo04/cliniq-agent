"""SQL 정적검사 (DB 불필요). 리포 경로: src/agents/sql_agent/checker.py

1) sqlglot 파싱 (dialect=oracle) — 문법 오류 검출
2) 참조 테이블/컬럼이 SchemaStore에 실재하는지 대조 (alias 해석 포함)
3) 출력 계약 검사: denominator_count / violation_count 두 컬럼
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from core.schema_store import SchemaStore
from models.sql import StaticCheckResult

REQUIRED_OUTPUT = {"denominator_count", "violation_count"}


def _unit_error(tree: exp.Select) -> str | None:
    """집계 단위 위반 검출 (레코드 단위 강제).

    v1은 모든 규칙을 레코드 단위로 고정하므로 COUNT(DISTINCT ...) 사용 자체를 금지한다.
    (분모 COUNT(DISTINCT person_id) + 분자 SUM(CASE...) 조합이 실제로 발생해
     위반율이 1을 초과할 수 있는 버그를 만들었다)
    """
    exprs = {(e.alias_or_name or "").lower(): e for e in tree.expressions}
    den, num = exprs.get("denominator_count"), exprs.get("violation_count")
    if den is None or num is None:
        return None

    def has_distinct(node) -> bool:
        return any(isinstance(d, exp.Distinct) for d in node.find_all(exp.Distinct))

    if has_distinct(den) or has_distinct(num):
        return (
            "unit violation: 모든 규칙은 레코드 단위로 집계해야 한다. "
            "COUNT(DISTINCT ...) 대신 COUNT(*) 와 "
            "SUM(CASE WHEN ... THEN 1 ELSE 0 END) 형태를 사용하라."
        )
    return None


def _self_contradiction_error(tree: exp.Select) -> str | None:
    """결측 검사 자기모순 검출.

    실제 발생 사례:
        SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END)  -- 분자: NULL을 셈
        WHERE col IS NOT NULL                          -- 분모: NULL을 제외
    → 위반 건수가 항상 0이 되는 무의미한 규칙. DB 없이 검출 가능하다.
    """
    where = tree.args.get("where")
    if where is None:
        return None

    # WHERE 절에서 "col IS NOT NULL"로 제외된 컬럼 수집
    excluded: set[str] = set()
    for isnull in where.find_all(exp.Is):
        if not isinstance(isnull.expression, exp.Null):
            continue
        col = isnull.this
        if not isinstance(col, exp.Column):
            continue
        # NOT(col IS NULL) 형태인지 확인
        if isinstance(isnull.parent, exp.Not):
            excluded.add(col.name.lower())

    if not excluded:
        return None

    # 분자(violation_count) 안에서 "col IS NULL"을 세는지 확인
    num = next(
        (e for e in tree.expressions
         if (e.alias_or_name or "").lower() == "violation_count"),
        None,
    )
    if num is None:
        return None

    for isnull in num.find_all(exp.Is):
        if not isinstance(isnull.expression, exp.Null):
            continue
        if isinstance(isnull.parent, exp.Not):
            continue
        col = isnull.this
        if isinstance(col, exp.Column) and col.name.lower() in excluded:
            return (
                f"self-contradiction: 분자는 {col.name} IS NULL 인 행을 세는데 "
                f"분모(WHERE)에서 {col.name} IS NOT NULL 로 제외하고 있다. "
                "결측 검사는 분모에서 NULL을 제외하면 안 된다 "
                "(위반 건수가 항상 0이 됨). WHERE 절에서 해당 조건을 제거하라."
            )
    return None


def static_check(rule_id: str, sql: str, store: SchemaStore) -> StaticCheckResult:
    try:
        tree = sqlglot.parse_one(
            sql, read="oracle", error_level=sqlglot.ErrorLevel.RAISE
        )
    except Exception as e:
        return StaticCheckResult(rule_id=rule_id, parse_ok=False, parse_error=str(e))

    if not isinstance(tree, exp.Select):
        return StaticCheckResult(
            rule_id=rule_id,
            parse_ok=False,
            parse_error=f"not a single SELECT statement (got {type(tree).__name__})",
        )
    # 중복 검사(isPrimaryKey)는 GROUP BY ... HAVING 서브쿼리가 필요하므로 허용한다.
    # 최상위 SELECT가 두 컬럼 계약을 지키면 Execution·집계 검사는 그대로 동작한다.

    # alias → 실제 테이블 매핑 (서브쿼리 내부 테이블도 포함)
    alias_map: dict[str, str] = {}
    tables: set[str] = set()
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        tables.add(name)
        alias = (t.alias or name).lower()
        alias_map[alias] = name

    unknown_tables = sorted(t for t in tables if not store.has_table(t))

    unknown_columns: list[str] = []
    known_all: set[str] = set()
    for t in tables:
        if store.has_table(t):
            known_all |= store.get(t).field_names()

    for c in tree.find_all(exp.Column):
        col = c.name.lower()
        if col in REQUIRED_OUTPUT:  # 출력 alias 참조는 스키마 컬럼이 아님
            continue
        qual = (c.table or "").lower()
        if qual:
            real = alias_map.get(qual, qual)
            if store.has_table(real) and col not in store.get(real).field_names():
                unknown_columns.append(f"{real}.{col}")
        elif col not in known_all:
            unknown_columns.append(col)

    # 출력 계약 검사 (최상위 SELECT alias)
    parse_error = None
    selects = [(s.alias_or_name or "").lower() for s in tree.expressions]
    if not REQUIRED_OUTPUT.issubset(set(selects)):
        parse_error = (
            f"output contract violated: expected {sorted(REQUIRED_OUTPUT)}, got {selects}"
        )
    else:
        parse_error = _unit_error(tree) or _self_contradiction_error(tree)

    return StaticCheckResult(
        rule_id=rule_id,
        parse_ok=parse_error is None,
        parse_error=parse_error,
        unknown_tables=unknown_tables,
        unknown_columns=sorted(set(unknown_columns)),
    )