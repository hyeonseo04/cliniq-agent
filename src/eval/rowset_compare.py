"""행 집합 실행 등가 비교 — 평가의 주 채점기.
리포 경로: src/eval/rowset_compare.py

원리:
    DQD 공식 정의를 이식한 골드 SQL과 시스템이 생성한 SQL을 같은 DB에서 실행하고,
    위반 행의 PK 집합이 같은지 비교한다.

    S_gold == S_gen  →  일치 (표현·태그·SQL 구조와 무관)

건수(N==M)가 아니라 행 집합인 이유:
    - 0건-0건 우연 일치 방지
    - 서로 다른 검사가 우연히 같은 건수를 내는 오탐 방지
    - 실제 사례: isStandardValidConcept은 DQD 정의상 concept_id != 0을 제외하는데
      생성 SQL이 이를 빠뜨리면 건수는 같아도 집합이 갈린다

생성 SQL 변환:
    시스템 SQL은 COUNT(*)/SUM(CASE...) 형태이므로 그대로는 행을 못 뽑는다.
    sqlglot으로 파싱해 SELECT 절을 PK로 바꾸고, 위반 조건을 WHERE에 병합한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from eval.dqd_gold_sql import PK

log = logging.getLogger(__name__)


@dataclass
class RowSetResult:
    """한 규칙에 대한 행 집합 비교 결과."""

    check_id: str
    rule_id: str
    match: bool
    gold_count: int
    gen_count: int
    intersection: int
    only_gold: int          # 골드만 잡은 행 (미검출)
    only_gen: int           # 생성만 잡은 행 (오검출)
    error: str | None = None
    samples: dict = field(default_factory=dict)  # 차이 행 샘플 (디버깅용)

    @property
    def jaccard(self) -> float:
        union = self.gold_count + self.gen_count - self.intersection
        return self.intersection / union if union else 1.0

    def __repr__(self) -> str:
        if self.error:
            return f"RowSet({self.rule_id}, ERROR: {self.error[:50]})"
        return (
            f"RowSet({self.rule_id}, match={self.match}, "
            f"gold={self.gold_count}, gen={self.gen_count}, "
            f"J={self.jaccard:.3f})"
        )


def to_rowset_sql(sql: str, table: str) -> str:
    """생성된 집계 SQL → 위반 행 PK 목록 SQL로 변환.

    SELECT COUNT(*) AS denominator_count,
           SUM(CASE WHEN <위반조건> THEN 1 ELSE 0 END) AS violation_count
    FROM ... WHERE <분모조건>
        ↓
    SELECT <alias>.<pk> AS violating_pk
    FROM ... WHERE (<분모조건>) AND (<위반조건>)
    """
    pk = PK.get(table.lower())
    if pk is None:
        raise ValueError(f"PK 미정의 테이블: {table}")

    tree = sqlglot.parse_one(sql, read="oracle", error_level=sqlglot.ErrorLevel.RAISE)
    if not isinstance(tree, exp.Select):
        raise ValueError("단일 SELECT가 아니다")

    # violation_count 표현식에서 CASE WHEN 조건 추출
    violation = next(
        (
            e
            for e in tree.expressions
            if (e.alias_or_name or "").lower() == "violation_count"
        ),
        None,
    )
    if violation is None:
        raise ValueError("violation_count 컬럼이 없다")

    case = violation.find(exp.Case)
    if case is None or not case.args.get("ifs"):
        raise ValueError("CASE WHEN 위반 조건을 찾을 수 없다")
    cond = case.args["ifs"][0].this  # WHEN <cond> THEN ...

    # 대상 테이블의 별칭 찾기 (PK를 어디서 뽑을지)
    alias = None
    for t in tree.find_all(exp.Table):
        if t.name.lower() == table.lower():
            alias = (t.alias or t.name).lower()
            break
    if alias is None:
        raise ValueError(f"SQL에서 {table} 테이블을 찾을 수 없다")

    new = tree.copy()
    new.set("expressions", [
        exp.column(pk, table=alias).as_("violating_pk")
    ])

    where = new.args.get("where")
    merged = exp.and_(where.this, cond) if where else cond
    new.set("where", exp.Where(this=merged))

    return new.sql(dialect="oracle")


def fetch_pks(conn, sql: str, limit: int | None = None) -> set:
    """SQL을 실행해 PK 집합을 반환."""
    cur = conn.cursor()
    cur.execute(sql if limit is None else f"{sql} FETCH FIRST {limit} ROWS ONLY")
    return {row[0] for row in cur}


def compare(
    conn,
    *,
    check_id: str,
    rule_id: str,
    gold_sql: str,
    gen_sql: str,
    table: str,
    sample_n: int = 5,
) -> RowSetResult:
    """골드와 생성 SQL의 위반 행 집합을 비교한다."""
    try:
        gen_rows_sql = to_rowset_sql(gen_sql, table)
    except Exception as e:  # noqa: BLE001
        return RowSetResult(check_id, rule_id, False, 0, 0, 0, 0, 0,
                            error=f"생성 SQL 변환 실패: {e}")

    try:
        s_gold = fetch_pks(conn, gold_sql)
    except Exception as e:  # noqa: BLE001
        return RowSetResult(check_id, rule_id, False, 0, 0, 0, 0, 0,
                            error=f"골드 SQL 실행 실패: {e}")

    try:
        s_gen = fetch_pks(conn, gen_rows_sql)
    except Exception as e:  # noqa: BLE001
        return RowSetResult(check_id, rule_id, False, len(s_gold), 0, 0,
                            len(s_gold), 0, error=f"생성 SQL 실행 실패: {e}")

    inter = s_gold & s_gen
    og, on = s_gold - s_gen, s_gen - s_gold
    return RowSetResult(
        check_id=check_id,
        rule_id=rule_id,
        match=(s_gold == s_gen),
        gold_count=len(s_gold),
        gen_count=len(s_gen),
        intersection=len(inter),
        only_gold=len(og),
        only_gen=len(on),
        samples={
            "only_gold": sorted(og)[:sample_n],
            "only_gen": sorted(on)[:sample_n],
        },
    )


def compare_injected(
    conn,
    *,
    check_id: str,
    rule_id: str,
    gen_sql: str,
    table: str,
    s_before: set,
    s_injected: set,
) -> RowSetResult:
    """오류 주입 known-answer 판정 (차분 기반).

    SynPUF에는 유기적 위반이 이미 존재하므로 단순 등식은 성립하지 않는다.
        S_after - S_before == S_injected  이면 정확

    s_before는 주입 전 클린 상태에서 같은 SQL로 얻은 집합이어야 한다.
    """
    try:
        gen_rows_sql = to_rowset_sql(gen_sql, table)
        s_after = fetch_pks(conn, gen_rows_sql)
    except Exception as e:  # noqa: BLE001
        return RowSetResult(check_id, rule_id, False, 0, 0, 0, 0, 0, error=str(e))

    delta = s_after - s_before
    inter = delta & s_injected
    return RowSetResult(
        check_id=check_id,
        rule_id=rule_id,
        match=(delta == s_injected),
        gold_count=len(s_injected),
        gen_count=len(delta),
        intersection=len(inter),
        only_gold=len(s_injected - delta),   # 주입했는데 못 잡은 것
        only_gen=len(delta - s_injected),    # 주입 안 했는데 새로 잡은 것
        samples={
            "missed": sorted(s_injected - delta)[:5],
            "extra": sorted(delta - s_injected)[:5],
        },
    )