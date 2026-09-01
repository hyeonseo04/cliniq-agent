"""골든셋 항목별 오류 주입 — 판정 불능(0건-0건) 문제 해결.
리포 경로: scripts/inject_per_item.py

배경:
    SynPUF는 OHDSI가 ETL로 정제한 데이터라 결측·참조 위반이 거의 없다.
    그 결과 골든셋 200건 중 139건(69.5%)에서 골드와 생성 SQL이 **둘 다 위반 0건**을
    반환해, 서로 다른 검사를 만들어도 "일치"로 판정되는 문제가 발생했다.

    실제 사례 (observation.observation_id / isPrimaryKey):
        골드:   중복 PK 검출        → 0건
        생성A:  중복 PK 검출        → 0건   ← 정답
        생성B:  observation_id IS NULL 검출 → 0건   ← 오답인데 구분 불가

해결:
    각 골든셋 항목의 대상 컬럼에 **해당 검사가 잡아야 할 위반**을 의도적으로 심는다.
    그러면 골드는 N건을 잡고, 엉뚱한 SQL은 0건을 잡아 구분된다.

    주입 전: 골드 0 / 생성 0  → 판정 불능
    주입 후: 골드 3 / 생성 3  → 진짜 일치
             골드 3 / 생성 0  → 불일치 (엉뚱한 SQL이 걸러짐)

판정 (차분):
    S_after − S_before == S_injected

사용법:
    uv run python scripts/inject_per_item.py --plan        # 주입 계획만 출력
    uv run python scripts/inject_per_item.py --apply G001  # 특정 항목 주입
    uv run python scripts/inject_per_item.py --restore     # 전체 원복

    실제 채점은 score_injected.py가 [주입 → 비교 → 원복]을 항목별로 반복한다.

주의:
    검증 환경 전용. 운영 DB에는 절대 사용하지 마라.
    주입은 항상 트랜잭션으로 감싸고 판정 후 롤백한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
from eval.dqd_gold_sql import PK  # noqa: E402

GOLDEN = config.DATA_DIR / "golden_set" / "golden_set.json"

#: 주입할 위반 행 수 (적을수록 원복이 안전)
N_INJECT = 3


def _pk(table: str) -> str:
    return PK[table.lower()]


#: IN 절에 넣을 수 있는 최대 개수 (Oracle 제한 1000)
_MAX_IN = 900


def _exclude_clause(pk: str, exclude_pks: set | None) -> str:
    """이미 위반 상태인 행을 제외하는 WHERE 절."""
    if not exclude_pks:
        return ""
    vals = sorted(exclude_pks)[:_MAX_IN]
    return f" WHERE {pk} NOT IN ({', '.join(str(v) for v in vals)})"


def build_injection(item: dict, exclude_pks: set | None = None) -> dict | None:
    """골든셋 항목 → 주입 SQL 계획.

    반환: {"sql": [...], "why": 설명} 또는 None(주입 불가)

    exclude_pks: 주입 전 이미 위반 상태인 행의 PK 집합.
        이 행에 주입하면 차분(S_after − S_before)이 0이 되어 판정이 불가능하다.
        SynPUF는 concept 매핑이 부실해 *_concept_id가 이미 0인 행이 많으므로
        반드시 제외해야 한다.
    """
    check = item["expected_checks"][0]
    table = item["table"]
    col = item["column"]
    pk = _pk(table)
    params = item.get("gold_params", {})
    ctype = (params.get("col_type") or "").lower()

    # 주입 대상 행 선택 — 이미 위반인 행은 제외 (차분이 0이 되는 것을 방지)
    not_violated = _exclude_clause(pk, exclude_pks)
    target = (
        f"{pk} IN (SELECT {pk} FROM (SELECT {pk} FROM {table}"
        f"{not_violated} ORDER BY {pk}) WHERE ROWNUM <= {N_INJECT})"
    )

    def upd(expr: str, why: str, extra_where: str = "") -> dict:
        w = f"{target}{' AND ' + extra_where if extra_where else ''}"
        return {"sql": [f"UPDATE {table} SET {col} = {expr} WHERE {w}"], "why": why}

    # ---- 결측 계열: NULL로 만든다 ----
    if check in ("measureValueCompleteness", "isRequired", "sourceValueCompleteness"):
        return upd("NULL", f"{col}을 NULL로 → 결측 위반")

    # ---- 개념 매핑 실패: 0으로 만든다 ----
    if check in ("standardConceptRecordCompleteness",
                 "sourceConceptRecordCompleteness"):
        return upd("0", f"{col}을 0으로 → 표준 개념 매핑 실패")

    # ---- 참조 무결성: 존재하지 않는 값 ----
    if check == "isForeignKey":
        return upd("-999999", f"{col}을 존재하지 않는 참조값으로")

    # ---- 표준·유효 개념 아님: 비표준 concept으로 ----
    if check in ("isStandardValidConcept", "fkDomain", "fkClass"):
        return {
            "sql": [
                f"UPDATE {table} SET {col} = "
                f"(SELECT MIN(concept_id) FROM concept "
                f"WHERE standard_concept IS NULL AND concept_id > 0) "
                f"WHERE {target}"
            ],
            "why": f"{col}을 비표준 concept으로",
        }

    # ---- PK 중복: 제약을 잠시 풀고 중복 행 삽입 ----
    if check == "isPrimaryKey":
        return {
            "sql": [
                f"ALTER TABLE {table} DISABLE CONSTRAINT xpk_{table}",
                f"INSERT INTO {table} ({pk}) "
                f"SELECT {pk} FROM (SELECT {pk} FROM {table} ORDER BY {pk}) "
                f"WHERE ROWNUM <= {N_INJECT}",
            ],
            "restore": [
                f"DELETE FROM {table} WHERE {pk} IN "
                f"(SELECT {pk} FROM {table} GROUP BY {pk} HAVING COUNT(*) > 1) "
                f"AND ROWID NOT IN (SELECT MIN(ROWID) FROM {table} GROUP BY {pk})",
                f"ALTER TABLE {table} ENABLE CONSTRAINT xpk_{table}",
            ],
            "why": f"{pk} 중복 행 {N_INJECT}건 삽입 (PK 제약 일시 해제)",
        }

    # ---- 값 범위: 임계값 밖으로 ----
    if check == "plausibleValueLow":
        th = params.get("threshold_value", "0")
        expr = f"{th} - 1" if "date" not in ctype else f"{th} - 1"
        return upd(expr, f"{col}을 하한({th}) 미만으로")

    if check == "plausibleValueHigh":
        th = params.get("threshold_value", "0")
        expr = f"{th} + 1"
        return upd(expr, f"{col}을 상한({th}) 초과로")

    # ---- 시간 타당성 ----
    if check == "plausibleStartBeforeEnd":
        pair = params.get("pair_field")
        if not pair:
            return None
        return upd(f"{pair} + 30", f"{col}을 {pair}보다 30일 뒤로")

    if check == "plausibleAfterBirth":
        return {
            "sql": [
                f"UPDATE {table} SET {col} = "
                f"(SELECT TO_DATE(TO_CHAR(p.year_of_birth - 5) || '0101','YYYYMMDD') "
                f"FROM person p WHERE p.person_id = {table}.person_id) "
                f"WHERE {target} AND person_id IN (SELECT person_id FROM person)"
            ],
            "why": f"{col}을 출생 5년 전으로",
        }

    if check in ("plausibleBeforeDeath", "plausibleDuringLife"):
        # 사망 기록이 있는 환자의 행만 대상 (+60일 허용 오차를 넘겨야 위반)
        return {
            "sql": [
                f"UPDATE {table} SET {col} = "
                f"(SELECT d.death_date + 200 FROM death d "
                f"WHERE d.person_id = {table}.person_id) "
                f"WHERE {pk} IN (SELECT {pk} FROM (SELECT t.{pk} FROM {table} t "
                f"JOIN death d ON t.person_id = d.person_id"
                f"{_exclude_clause('t.' + pk, exclude_pks)} ORDER BY t.{pk}) "
                f"WHERE ROWNUM <= {N_INJECT})"
            ],
            "why": f"{col}을 사망일 +200일로 (허용 오차 60일 초과)",
        }

    if check == "plausibleTemporalAfter":
        pair = params.get("pair_field")
        ref = (params.get("ref_table") or table).lower()
        if not pair:
            return None
        if ref == table.lower():
            return upd(f"{pair} - 30", f"{col}을 {pair}보다 30일 앞으로")
        return None  # 다른 테이블 참조는 주입이 복잡해 제외

    if check == "withinVisitDates":
        return {
            "sql": [
                f"UPDATE {table} SET {col} = "
                f"(SELECT vo.visit_end_date + 60 FROM visit_occurrence vo "
                f"WHERE vo.visit_occurrence_id = {table}.visit_occurrence_id) "
                f"WHERE {pk} IN (SELECT {pk} FROM (SELECT t.{pk} FROM {table} t "
                f"JOIN visit_occurrence vo "
                f"ON t.visit_occurrence_id = vo.visit_occurrence_id"
                f"{_exclude_clause('t.' + pk, exclude_pks)} "
                f"ORDER BY t.{pk}) WHERE ROWNUM <= {N_INJECT})"
            ],
            "why": f"{col}을 방문 종료일 +60일로 (허용 오차 7일 초과)",
        }

    # ---- 임상 타당성 ----
    if check in ("plausibleGender", "plausibleGenderUseDescendants"):
        cid = params.get("concept_id")
        gender = (params.get("gender") or "").lower()
        if not cid:
            return None
        # 타당한 성별과 반대인 환자에게 해당 concept을 심는다
        wrong = 8532 if gender == "male" else 8507
        return {
            "sql": [
                f"UPDATE {table} SET {col} = {cid} "
                f"WHERE {pk} IN (SELECT {pk} FROM (SELECT t.{pk} FROM {table} t "
                f"JOIN person p ON t.person_id = p.person_id "
                f"WHERE p.gender_concept_id = {wrong}"
                + (f" AND t.{pk} NOT IN ({', '.join(str(v) for v in sorted(exclude_pks)[:_MAX_IN])})" if exclude_pks else "")
                + f" ORDER BY t.{pk}) "
                f"WHERE ROWNUM <= {N_INJECT})"
            ],
            "why": f"{col}={cid}을 반대 성별({wrong}) 환자에게 기록",
        }

    if check == "plausibleUnitConceptIds":
        cid = params.get("concept_id")
        if not cid:
            return None
        return {
            "sql": [
                f"UPDATE {table} SET {col} = {cid}, unit_concept_id = -999999 "
                f"WHERE {target}"
            ],
            "why": f"{col}={cid}에 타당하지 않은 단위(-999999) 기록",
        }

    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="주입 계획만 출력")
    ap.add_argument("--check", help="특정 체크 유형만")
    args = ap.parse_args()

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    items = golden["items"]
    if args.check:
        items = [i for i in items if i["expected_checks"][0] == args.check]

    ok = skip = 0
    by_check: dict[str, list[int]] = {}
    for item in items:
        plan = build_injection(item)
        c = item["expected_checks"][0]
        by_check.setdefault(c, [0, 0])
        by_check[c][1] += 1
        if plan:
            ok += 1
            by_check[c][0] += 1
            if args.plan and by_check[c][0] <= 1:
                print(f"\n[{item['id']}] {c} — {item['table']}.{item['column']}")
                print(f"  {plan['why']}")
                for sql in plan["sql"]:
                    print(f"    {sql[:150]}")
        else:
            skip += 1

    print(f"\n주입 가능: {ok}/{len(items)}건")
    print("\n체크별 주입 가능 여부:")
    for c, (o, t) in sorted(by_check.items(), key=lambda x: -x[1][1]):
        mark = "✓" if o == t else ("△" if o else "✗")
        print(f"  {mark} {c:36s} {o}/{t}")


if __name__ == "__main__":
    main()