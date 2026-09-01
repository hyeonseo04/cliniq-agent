"""Oracle 연결 · EXPLAIN 검증 진단 스크립트.
리포 경로: scripts/check_oracle.py

사용법:
    uv run python scripts/check_oracle.py              # integrated DB
    uv run python scripts/check_oracle.py --db preprocessed

.env 설정 예시:
    ORACLE_USER=cdm53
    ORACLE_PASSWORD=****
    ORACLE_DSN_INTEGRATED=localhost:1521/FREEPDB1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
from agents.execution_agent import explain  # noqa: E402
from core.db import OracleDB  # noqa: E402
from models.common import TargetDB  # noqa: E402

SAMPLES = [
    (
        "정상 SQL (단일 테이블)",
        "SELECT COUNT(*) AS denominator_count, "
        "SUM(CASE WHEN value_as_number < 0 THEN 1 ELSE 0 END) AS violation_count "
        "FROM measurement WHERE value_as_number IS NOT NULL",
        True,
    ),
    (
        "정상 SQL (JOIN)",
        "SELECT COUNT(*) AS denominator_count, "
        "SUM(CASE WHEN d.death_date < p.birth_datetime THEN 1 ELSE 0 END) AS violation_count "
        "FROM person p JOIN death d ON d.person_id = p.person_id "
        "WHERE p.birth_datetime IS NOT NULL AND d.death_date IS NOT NULL",
        True,
    ),
    (
        "존재하지 않는 테이블 (ORA-00942 기대)",
        "SELECT COUNT(*) AS denominator_count, 0 AS violation_count FROM no_such_table",
        False,
    ),
    (
        "존재하지 않는 컬럼 (ORA-00904 기대)",
        "SELECT COUNT(*) AS denominator_count, "
        "SUM(CASE WHEN no_such_col < 0 THEN 1 ELSE 0 END) AS violation_count FROM person",
        False,
    ),
    (
        "타 방언 문법 (LIMIT — sqlglot이 못 잡는 것)",
        "SELECT COUNT(*) AS denominator_count, 0 AS violation_count FROM person LIMIT 10",
        False,
    ),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db", default="integrated", choices=["integrated", "preprocessed", "linked"]
    )
    args = ap.parse_args()
    target = TargetDB(args.db)

    print(f"[설정] DSN={config.ORACLE_DSN.get(args.db) or '(미설정)'} user={config.ORACLE_USER}")

    try:
        db = OracleDB(target)
    except ValueError as e:
        print(f"✗ {e}")
        return

    print("\n[1] 연결 테스트")
    if not db.ping():
        print("  ✗ 연결 실패 — DSN·계정·서버 상태를 확인하라")
        return
    print("  ✓ 연결 성공")

    print("\n[2] EXPLAIN 검증 (데이터 미조회)")
    passed = 0
    for name, sql, expect_ok in SAMPLES:
        r = explain("CHECK", sql, db)
        mark = "✓" if r.ok == expect_ok else "✗"
        if r.ok == expect_ok:
            passed += 1
        detail = "통과" if r.ok else f"거부 [{r.ora_code or '?'}] cause={r.cause}"
        print(f"  {mark} {name}: {detail}")

    print(f"\n{passed}/{len(SAMPLES)} 예상대로 동작.")
    if passed == len(SAMPLES):
        print("EXPLAIN 검증 준비 완료 — main.py에서 파이프라인에 연결된다.")
    else:
        print("일부 결과가 예상과 다르다. 스키마가 적재되었는지 확인하라.")


if __name__ == "__main__":
    main()