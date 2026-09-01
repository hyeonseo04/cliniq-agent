"""오류 주입 기반 채점 — 판정 불능을 제거한 실질 커버리지 측정.
리포 경로: scripts/score_injected.py

문제:
    SynPUF는 정제된 데이터라 골든셋 200건 중 139건(69.5%)에서
    골드와 생성 SQL이 **둘 다 위반 0건**을 반환한다.
    이 경우 서로 다른 검사를 만들어도 집합이 같아 "일치"로 판정된다.

해결:
    항목별로 해당 검사가 잡아야 할 위반을 심고, 차분으로 판정한다.

        S_before = 주입 전 위반 행 집합
        S_after  = 주입 후 위반 행 집합
        detected = S_after − S_before

        골드의 detected == 생성의 detected  →  일치

    엉뚱한 SQL은 주입한 위반을 못 잡으므로 detected가 비어 구분된다.

절차 (항목마다 반복):
    1. 주입 전 골드·생성 SQL 실행 → S_before
    2. 오류 주입
    3. 주입 후 골드·생성 SQL 실행 → S_after
    4. 차분 비교로 판정
    5. 롤백 (다음 항목에 영향 없도록)

사용법:
    uv run python scripts/score_injected.py eval_Qwen3-32B_20260829.json
    uv run python scripts/score_injected.py --limit 20      # 일부만 (점검용)
    uv run python scripts/score_injected.py --detail

주의:
    검증 환경 전용. 모든 주입은 롤백된다(PK 제약 해제 케이스는 명시적 복원).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
from eval.rowset_compare import to_rowset_sql  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inject_per_item import build_injection  # noqa: E402

EVAL_DIR = config.ROOT / "results" / "eval"
GOLD_SQL = config.DATA_DIR / "golden_set" / "gold_sql.json"
GOLDEN = config.DATA_DIR / "golden_set" / "golden_set.json"

DIM_KO = {"Conformance": "Conformance", "Completeness": "Completeness",
          "Plausibility": "Plausibility"}
CPLX_KO = {"ST_SF": "단일테이블·단일필드", "ST_MF": "단일테이블·다중필드",
           "MT_MF": "다중테이블·다중필드"}


def connect():
    import oracledb

    return oracledb.connect(
        user=config.ORACLE_USER, password=config.ORACLE_PASSWORD,
        dsn=config.ORACLE_DSN["integrated"],
    )


def pks(cur, sql: str) -> set:
    cur.execute(sql)
    return {r[0] for r in cur}


def judge_item(cur, conn, item: dict, gold_sql: str, gen_sqls: list[str]) -> dict:
    """한 항목 판정. 주입 → 비교 → 롤백."""
    out = {
        "id": item["id"], "check": item["expected_checks"][0],
        "hit": False, "injectable": True,
        "gold_detected": 0, "note": "",
    }

    # ---------- 주입 전 상태 ----------
    try:
        gold_before = pks(cur, gold_sql)
    except Exception as e:  # noqa: BLE001
        out["note"] = f"골드 실행 실패: {str(e)[:60]}"
        return out

    # 이미 위반인 행을 제외하고 주입 계획을 세운다
    plan = build_injection(item, exclude_pks=gold_before)
    out["injectable"] = plan is not None

    gen_before: dict[int, set] = {}
    for i, gs in enumerate(gen_sqls):
        try:
            gen_before[i] = pks(cur, gs)
        except Exception:  # noqa: BLE001
            pass

    if plan is None:
        # 주입 불가 → 기존 방식(정적 비교)으로 판정하되 표시를 남긴다
        out["note"] = "주입 불가 — 정적 비교"
        out["hit"] = any(s == gold_before for s in gen_before.values())
        out["gold_detected"] = len(gold_before)
        return out

    # ---------- 주입 ----------
    try:
        for sql in plan["sql"]:
            cur.execute(sql)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        out["note"] = f"주입 실패: {str(e)[:60]}"
        out["hit"] = any(s == gold_before for s in gen_before.values())
        return out

    try:
        gold_after = pks(cur, gold_sql)
        gold_delta = gold_after - gold_before
        out["gold_detected"] = len(gold_delta)

        if not gold_delta:
            # 주입했는데 골드가 못 잡음 → 주입 패턴이 부적절
            out["note"] = "주입분을 골드가 검출하지 못함"
        else:
            for i, gs in enumerate(gen_sqls):
                if i not in gen_before:
                    continue
                try:
                    delta = pks(cur, gs) - gen_before[i]
                except Exception:  # noqa: BLE001
                    continue
                if delta == gold_delta:
                    out["hit"] = True
                    break
                if delta:
                    out["note"] = (
                        f"골드 {len(gold_delta)}건 vs 생성 {len(delta)}건"
                    )
            if not out["hit"] and not out["note"]:
                out["note"] = f"생성 SQL이 주입분({len(gold_delta)}건)을 검출 못함"
    finally:
        # ---------- 원복 ----------
        conn.rollback()
        for sql in plan.get("restore", []):
            try:
                cur.execute(sql)
                conn.commit()
            except Exception:  # noqa: BLE001
                pass

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    files = sorted(EVAL_DIR.glob("eval_*.json"), reverse=True)
    if not files:
        raise SystemExit(f"평가 결과가 없다: {EVAL_DIR}")
    path = (EVAL_DIR / args.file) if args.file else files[0]
    data = json.loads(path.read_text(encoding="utf-8"))

    gold_map = json.loads(GOLD_SQL.read_text(encoding="utf-8"))["queries"]
    golden = {i["id"]: i for i in
              json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]}

    results = data["results"][: args.limit] if args.limit else data["results"]
    conn = connect()
    cur = conn.cursor()

    rows: list[dict] = []
    try:
        for k, r in enumerate(results, 1):
            item = golden.get(r["id"])
            g = gold_map.get(r["id"])
            if not item or not g:
                continue
            gen_sqls = []
            for rule in r["generated"]:
                if rule.get("sql"):
                    try:
                        gen_sqls.append(to_rowset_sql(rule["sql"], r["table"]))
                    except Exception:  # noqa: BLE001
                        pass
            out = judge_item(cur, conn, item, g["rows_sql"], gen_sqls)
            out.update(dimension=item["dimension"], complexity=item["complexity"])
            rows.append(out)
            if k % 20 == 0:
                print(f"  ... {k}/{len(results)}")
    finally:
        conn.close()

    n = len(rows)
    hits = sum(r["hit"] for r in rows)
    injected = sum(r["injectable"] and r["gold_detected"] > 0 for r in rows)
    no_inject = sum(not r["injectable"] for r in rows)
    inject_fail = sum(r["injectable"] and r["gold_detected"] == 0 for r in rows)

    print(f"\n평가 결과: {path.name}   (오류 주입 기반)")
    print(f"모델: {data.get('model')}  |  {n}건")
    print("=" * 62)
    print(f"\n■ OCR (주입 검출 일치)   {hits/n:.3f} ({hits}/{n})   ← 주 지표")
    print(f"  주입 성공 항목          {injected}건")
    print(f"  주입 불가 항목          {no_inject}건 (정적 비교로 판정)")
    print(f"  주입했으나 골드 미검출   {inject_fail}건")

    for label, key, ko in (("Dimension Coverage Rate (DCR)", "dimension", DIM_KO),
                           ("Hierarchy Coverage Rate (HCR)", "complexity", CPLX_KO)):
        agg: dict = defaultdict(lambda: [0, 0])
        for r in rows:
            agg[r[key]][0] += r["hit"]
            agg[r[key]][1] += 1
        print(f"\n{label}")
        for k2, (h, t) in sorted(agg.items()):
            print(f"  {ko.get(k2, k2):18s} {h/t:.3f} ({h}/{t})")

    agg: dict = defaultdict(lambda: [0, 0])
    for r in rows:
        agg[r["check"]][0] += r["hit"]
        agg[r["check"]][1] += 1
    print("\n체크 유형별 커버리지")
    for c, (h, t) in sorted(agg.items(), key=lambda x: -x[1][1]):
        bar = "█" * round(10 * h / t)
        print(f"  {c:36s} {h/t:.3f} ({h}/{t}) {bar}")

    if args.detail:
        miss = [r for r in rows if not r["hit"]]
        print(f"\n불일치 {len(miss)}건")
        for r in miss[:40]:
            print(f"  [{r['id']}] {r['check']:32s} {r['note'][:60]}")


if __name__ == "__main__":
    main()