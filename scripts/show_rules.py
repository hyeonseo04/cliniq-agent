"""저장된 품질 규칙 조회·내보내기.
리포 경로: scripts/show_rules.py

파이프라인이 생성한 규칙은 두 곳에 저장된다:
  results/rules/<timestamp>_<id>.json  — 실행 단위 스냅샷 (실행 결과 포함)
  data/rule/catalog.json               — 누적 카탈로그

사용법:
    uv run python scripts/show_rules.py                    # 카탈로그 목록
    uv run python scripts/show_rules.py --detail R-PERS-001  # 규칙 상세
    uv run python scripts/show_rules.py --table person     # 테이블 필터
    uv run python scripts/show_rules.py --sql              # SQL만 출력
    uv run python scripts/show_rules.py --snapshots        # 실행 이력
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

CATALOG = config.DATA_DIR / "rule" / "catalog.json"
SNAPSHOTS = config.ROOT / "results" / "rules"

DIM_KO = {
    "uniqueness": "유일성",
    "completeness": "완전성",
    "conformity": "정합성",
    "consistency": "일관성",
}


def load_catalog() -> list[dict]:
    if not CATALOG.exists():
        return []
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", help="규칙 ID 상세 출력")
    ap.add_argument("--table", help="대상 테이블로 필터")
    ap.add_argument("--dimension", help="품질 차원으로 필터")
    ap.add_argument("--sql", action="store_true", help="SQL만 출력")
    ap.add_argument("--snapshots", action="store_true", help="실행 이력 목록")
    args = ap.parse_args()

    if args.snapshots:
        files = sorted(SNAPSHOTS.glob("*.json"), reverse=True)
        if not files:
            print("저장된 실행 이력이 없다.")
            return
        print(f"실행 이력 {len(files)}건:\n")
        for f in files[:20]:
            d = json.loads(f.read_text(encoding="utf-8"))
            execs = [r.get("execution") for r in d["rules"] if r.get("execution")]
            ok = sum(1 for e in execs if e and e.get("success"))
            tail = f" / 실행검증 {ok}건" if execs else ""
            print(f"  {d['generated_at']}  규칙 {d['rule_count']}건{tail}")
            print(f"    요청: {d['request']}")
            print(f"    파일: {f.name}")
        return

    rules = load_catalog()
    if not rules:
        print("저장된 규칙이 없다. main.py로 규칙을 생성하면 자동 저장된다.")
        return

    if args.detail:
        r = next((x for x in rules if x["rule_id"] == args.detail), None)
        if not r:
            print(f"규칙을 찾을 수 없다: {args.detail}")
            return
        print(f"[{r['rule_id']}] {r['name']}")
        print("─" * 60)
        print(f"설명      : {r['description']}")
        print(f"품질 차원 : {DIM_KO.get(r['dq_dimension'], r['dq_dimension'])}")
        print(f"복잡도    : {r['complexity']}")
        print(f"대상      : {', '.join(r['target_columns'])}")
        print(f"위반 조건 : {r['logic_nl']}")
        print(f"분모      : {r['denominator_nl']}")
        print(f"분자      : {r['numerator_nl']}")
        print(f"상태      : {r['status']}")
        if r.get("sql"):
            print(f"\nSQL:\n{r['sql']}")
        return

    if args.table:
        rules = [r for r in rules if args.table.lower() in r["target_tables"]]
    if args.dimension:
        rules = [r for r in rules if r["dq_dimension"] == args.dimension]

    if args.sql:
        for r in rules:
            if r.get("sql"):
                print(f"-- [{r['rule_id']}] {r['name']}")
                print(f"{r['sql']};\n")
        return

    print(f"저장된 품질 규칙 {len(rules)}건:\n")
    for r in rules:
        dim = DIM_KO.get(r["dq_dimension"], r["dq_dimension"])
        print(f"  [{r['rule_id']}] {r['name']}")
        print(
            f"     {dim} | {', '.join(r['target_tables'])} | "
            f"{r['status']} | SQL {'있음' if r.get('sql') else '없음'}"
        )


if __name__ == "__main__":
    main()