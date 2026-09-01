"""골든셋의 정답(골드) SQL 생성·저장.
리포 경로: scripts/build_gold_sql.py

배경:
    지금까지 골드 SQL은 채점할 때마다 즉석 생성해 실행하고 버렸다.
    결정론적이라 재현은 되지만 **검토·감사가 불가능**했다.
    이 스크립트는 정답 SQL을 파일로 남겨 다음을 가능하게 한다.
      - 사람이 정답 SQL을 직접 읽고 검증
      - 채점 시 재생성 없이 그대로 사용 (재현성 보장)
      - 문서 첨부 및 제3자 재현

사용법:
    uv run python scripts/build_gold_sql.py                 # 생성 + 저장
    uv run python scripts/build_gold_sql.py --verify        # DB에서 실행까지 확인

출력:
    data/golden_set/gold_sql.json   골든셋 id → {rows_sql, count_sql}
    results/gold_sql/gold_queries.sql   사람이 읽는 SQL 스크립트

주의:
    스키마나 골든셋이 바뀌면 반드시 재생성해야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
from eval.dqd_gold_sql import SUPPORTED, build  # noqa: E402

GOLDEN = config.DATA_DIR / "golden_set" / "golden_set.json"
OUT_JSON = config.DATA_DIR / "golden_set" / "gold_sql.json"
OUT_SQL = config.ROOT / "results" / "gold_sql" / "gold_queries.sql"

#: 골든셋의 gold_params 키 → dqd_gold_sql.build() 인자명
PARAM_MAP = {"threshold_value": "threshold"}


def to_kwargs(params: dict) -> dict:
    return {PARAM_MAP.get(k, k): v for k, v in params.items()}


def generate() -> tuple[dict, list[str]]:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    items = golden["items"]

    result: dict[str, dict] = {}
    errors: list[str] = []

    for item in items:
        check_id = item["expected_checks"][0]
        if check_id not in SUPPORTED:
            errors.append(f"{item['id']}: 미지원 체크 {check_id}")
            continue
        try:
            g = build(
                check_id, item["table"], item["column"],
                **to_kwargs(item.get("gold_params", {})),
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{item['id']}: {check_id} — {e}")
            continue

        result[item["id"]] = {
            "check_id": check_id,
            "table": item["table"],
            "column": item["column"],
            "dimension": item["dimension"],
            "subcategory": item.get("subcategory", ""),
            "complexity": item["complexity"],
            "gold_params": item.get("gold_params", {}),
            "rows_sql": g.rows_sql,     # 위반 행 PK 목록 (채점용)
            "count_sql": g.count_sql,   # 위반 건수 (참고용)
            "note": g.note,
        }
    return result, errors


def verify(gold: dict) -> tuple[int, list[str]]:
    """DB에서 실제 실행해 확인. (성공 수, 실패 목록)"""
    import oracledb

    conn = oracledb.connect(
        user=config.ORACLE_USER,
        password=config.ORACLE_PASSWORD,
        dsn=config.ORACLE_DSN["integrated"],
    )
    ok = 0
    fails: list[str] = []
    try:
        cur = conn.cursor()
        for gid, g in gold.items():
            try:
                cur.execute(g["count_sql"])
                g["violation_count"] = int(cur.fetchone()[0])
                ok += 1
            except Exception as e:  # noqa: BLE001
                fails.append(f"{gid}: {g['check_id']} — {str(e)[:70]}")
                g["violation_count"] = None
    finally:
        conn.close()
    return ok, fails


def write_sql_script(gold: dict) -> None:
    """사람이 읽고 그대로 실행할 수 있는 SQL 스크립트."""
    lines = [
        "-- 골든셋 정답(골드) SQL",
        "-- 출처: OHDSI DataQualityDashboard 공식 정의의 Oracle 이식",
        f"-- 생성: {datetime.now().isoformat(timespec='seconds')}",
        f"-- 항목: {len(gold)}건",
        "--",
        "-- 각 쿼리는 해당 검사의 '위반 행 PK 목록'을 반환한다.",
        "-- 채점은 이 결과와 시스템 생성 SQL의 결과를 집합 비교하여 수행한다.",
        "",
    ]
    by_check: dict[str, list] = {}
    for gid, g in gold.items():
        by_check.setdefault(g["check_id"], []).append((gid, g))

    for check_id in sorted(by_check):
        entries = by_check[check_id]
        lines.append("")
        lines.append("-- " + "=" * 68)
        lines.append(f"-- {check_id}  ({len(entries)}건)")
        if entries[0][1].get("note"):
            lines.append(f"-- 주의: {entries[0][1]['note']}")
        lines.append("-- " + "=" * 68)
        for gid, g in entries:
            vc = g.get("violation_count")
            tail = f"  → 위반 {vc:,}건" if isinstance(vc, int) else ""
            lines.append("")
            lines.append(
                f"-- [{gid}] {g['table']}.{g['column']} "
                f"({g['dimension']}/{g.get('subcategory') or '-'}, {g['complexity']}){tail}"
            )
            if g["gold_params"]:
                lines.append(f"--   파라미터: {g['gold_params']}")
            lines.append(g["rows_sql"] + ";")

    OUT_SQL.parent.mkdir(parents=True, exist_ok=True)
    OUT_SQL.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="DB에서 실행까지 확인")
    args = ap.parse_args()

    if not GOLDEN.exists():
        raise SystemExit(f"골든셋이 없다: {GOLDEN}")

    gold, errors = generate()
    print(f"골드 SQL 생성: {len(gold)}건")
    for e in errors[:10]:
        print(f"  ✗ {e}")
    if len(errors) > 10:
        print(f"  ... 외 {len(errors) - 10}건")

    if args.verify:
        print("\nDB 실행 확인 중...")
        ok, fails = verify(gold)
        print(f"  실행 성공: {ok}/{len(gold)}")
        for f in fails[:10]:
            print(f"  ✗ {f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source": "OHDSI DataQualityDashboard OMOP_CDMv5.3 (Oracle 이식)",
                "count": len(gold),
                "verified": bool(args.verify),
                "queries": gold,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_sql_script(gold)

    print(f"\n→ {OUT_JSON}")
    print(f"→ {OUT_SQL}")

    from collections import Counter

    print("\n체크 유형별:")
    for c, n in Counter(g["check_id"] for g in gold.values()).most_common():
        print(f"  {c:36s} {n:3d}")


if __name__ == "__main__":
    main()