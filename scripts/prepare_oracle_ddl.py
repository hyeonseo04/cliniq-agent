"""OMOP CDM v5.3 Oracle DDL 다운로드 + 스키마명 치환.
리포 경로: scripts/prepare_oracle_ddl.py

OHDSI 공식 DDL은 @cdmDatabaseSchema 자리표시자를 쓰므로 실제 스키마명으로 치환한다.
EXPLAIN 검증(Phase 4-1)은 데이터가 없어도 되므로 DDL만 적재하면 된다.

사용법:
    uv run python scripts/prepare_oracle_ddl.py --schema CDM53
    → data/ddl/omop_v53_oracle.sql 생성

    # 적재 (sqlplus 또는 SQL Developer)
    sqlplus cdm53/password@localhost:1521/FREEPDB1 @data/ddl/omop_v53_oracle.sql
"""
from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/OHDSI/CommonDataModel/main/inst/ddl/5.3/oracle"
FILES = [
    "OMOPCDM_oracle_5.3_ddl.sql",
    "OMOPCDM_oracle_5.3_primary_keys.sql",
    # constraints/indices는 EXPLAIN 검증에 불필요 — 적재 시간만 늘어남
]

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ddl" / "omop_v53_oracle.sql"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="CDM53", help="대상 Oracle 스키마(사용자)명")
    ap.add_argument(
        "--with-constraints", action="store_true", help="FK 제약·인덱스도 포함"
    )
    args = ap.parse_args()

    files = list(FILES)
    if args.with_constraints:
        files += [
            "OMOPCDM_oracle_5.3_constraints.sql",
            "OMOPCDM_oracle_5.3_indices.sql",
        ]

    parts = [
        "-- OMOP CDM v5.3 Oracle DDL (OHDSI 공식)",
        f"-- 대상 스키마: {args.schema}",
        "",
    ]
    for name in files:
        with urllib.request.urlopen(f"{BASE}/{name}", timeout=30) as r:
            sql = r.read().decode("utf-8")
        # @cdmDatabaseSchema.person → CDM53.person
        sql = sql.replace("@cdmDatabaseSchema.", f"{args.schema}.")
        sql = sql.replace("@cdmDatabaseSchema", args.schema)
        parts.append(f"-- ===== {name} =====")
        parts.append(sql)
        parts.append("")

    parts.append("COMMIT;")
    parts.append("EXIT;")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"→ {OUT}  ({len(files)} files, schema={args.schema})")


if __name__ == "__main__":
    main()