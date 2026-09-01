"""검출력 검증 — 데이터에 의도적 오류를 주입하고 복원한다.
리포 경로: scripts/inject_errors.py

배경:
    SynPUF는 이미 정제된 데이터여서 논리 위반이 없다(위반율 0%).
    이는 "정상 데이터를 잘못 잡지 않는다"(false positive 없음)는 확인은 되지만,
    "진짜 위반을 잡아낸다"(true positive)는 검증이 되지 않는다.
    이 스크립트로 알려진 개수의 오류를 심어 검출 정확도를 확인한다.

사용법:
    uv run python scripts/inject_errors.py --inject     # 오류 주입
    uv run python scripts/inject_errors.py --verify     # 실제 위반 건수 확인
    uv run python scripts/inject_errors.py --restore    # 원복

주의:
    검증 환경 전용. 운영 DB에는 절대 사용하지 마라.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

# (설명, 주입 SQL, 검증 SQL, 기대 건수, 원복 SQL)
CASES = [
    (
        "방문 종료일이 시작일보다 앞섬 (visit_occurrence)",
        """UPDATE visit_occurrence SET visit_end_date = visit_start_date - 5
           WHERE visit_occurrence_id IN (
             SELECT visit_occurrence_id FROM (
               SELECT visit_occurrence_id FROM visit_occurrence ORDER BY visit_occurrence_id
             ) WHERE ROWNUM <= 3)""",
        "SELECT COUNT(*) FROM visit_occurrence WHERE visit_end_date < visit_start_date",
        3,
        """UPDATE visit_occurrence SET visit_end_date = visit_start_date
           WHERE visit_end_date < visit_start_date""",
    ),
    (
        "출생연도가 타당 하한(1850) 미만 (person)",
        """UPDATE person SET year_of_birth = 1700
           WHERE person_id IN (
             SELECT person_id FROM (
               SELECT person_id FROM person ORDER BY person_id
             ) WHERE ROWNUM <= 2)""",
        "SELECT COUNT(*) FROM person WHERE year_of_birth < 1850",
        2,
        "UPDATE person SET year_of_birth = 1950 WHERE year_of_birth < 1850",
    ),
    (
        "진단일이 방문 종료일 이후 (condition_occurrence)",
        """UPDATE condition_occurrence SET condition_start_date = condition_start_date + 400
           WHERE condition_occurrence_id IN (
             SELECT condition_occurrence_id FROM (
               SELECT condition_occurrence_id FROM condition_occurrence
               WHERE visit_occurrence_id IS NOT NULL ORDER BY condition_occurrence_id
             ) WHERE ROWNUM <= 5)""",
        """SELECT COUNT(*) FROM condition_occurrence co
           JOIN visit_occurrence vo ON co.visit_occurrence_id = vo.visit_occurrence_id
           WHERE co.condition_start_date > vo.visit_end_date""",
        5,
        """UPDATE condition_occurrence SET condition_start_date = condition_start_date - 400
           WHERE condition_occurrence_id IN (
             SELECT co.condition_occurrence_id FROM condition_occurrence co
             JOIN visit_occurrence vo ON co.visit_occurrence_id = vo.visit_occurrence_id
             WHERE co.condition_start_date > vo.visit_end_date)""",
    ),
]


def _connect():
    import oracledb

    return oracledb.connect(
        user=config.ORACLE_USER,
        password=config.ORACLE_PASSWORD,
        dsn=config.ORACLE_DSN["integrated"],
    )


def _run(conn, sql: str) -> int:
    cur = conn.cursor()
    cur.execute(sql)
    return cur.rowcount


def _count(conn, sql: str) -> int:
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    return int(row[0]) if row else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inject", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    conn = _connect()
    try:
        if args.inject:
            print("오류 주입:")
            for desc, inject_sql, verify_sql, expected, _ in CASES:
                _run(conn, inject_sql)
                conn.commit()
                actual = _count(conn, verify_sql)
                mark = "✓" if actual >= expected else "✗"
                print(f"  {mark} {desc}: {actual}건 (기대 {expected})")
            print("\n에이전트로 같은 컬럼을 평가해 이 건수가 검출되는지 확인하라.")

        elif args.verify:
            print("현재 위반 건수:")
            for desc, _, verify_sql, expected, _ in CASES:
                print(f"  {desc}: {_count(conn, verify_sql)}건")

        else:
            print("원복:")
            for desc, _, verify_sql, _, restore_sql in CASES:
                _run(conn, restore_sql)
                conn.commit()
                print(f"  {desc}: 남은 위반 {_count(conn, verify_sql)}건")
    finally:
        conn.close()


if __name__ == "__main__":
    main()