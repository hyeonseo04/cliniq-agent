"""두 평가 결과를 나란히 비교한다 (모델 간 비교·ablation용).
리포 경로: scripts/compare_eval.py

행 집합 실행 등가 기준으로 양쪽을 채점하고 지표를 표로 대조한다.
골든셋이 다르면 비교가 성립하지 않으므로 먼저 항목 일치 여부를 확인한다.

사용법:
    uv run python scripts/compare_eval.py eval_A.json eval_B.json
    uv run python scripts/compare_eval.py A.json B.json --label-a 14B --label-b 32B
    uv run python scripts/compare_eval.py A.json B.json --diff    # 판정이 갈린 항목만
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
from eval.dqd_gold_sql import SUPPORTED, build  # noqa: E402
from eval.rowset_compare import fetch_pks, to_rowset_sql  # noqa: E402

EVAL_DIR = config.ROOT / "results" / "eval"
GOLD_SQL = config.DATA_DIR / "golden_set" / "gold_sql.json"
_TAG = re.compile(r"\[지표:\s*([A-Za-z0-9_]+)\]")

DIM_KO = {
    "Conformance": "Conformance (정합성)",
    "Completeness": "Completeness (완전성)",
    "Plausibility": "Plausibility (타당성)",
}
CPLX_KO = {
    "ST_SF": "단일테이블·단일필드",
    "ST_MF": "단일테이블·다중필드",
    "MT_MF": "다중테이블·다중필드",
}


def connect():
    import oracledb

    return oracledb.connect(
        user=config.ORACLE_USER,
        password=config.ORACLE_PASSWORD,
        dsn=config.ORACLE_DSN["integrated"],
    )


def load_gold() -> dict:
    if GOLD_SQL.exists():
        return json.loads(GOLD_SQL.read_text(encoding="utf-8")).get("queries", {})
    return {}


def gold_set(conn, item: dict, cache: dict, gold_sql: dict):
    key = (item["expected_checks"][0], item["table"], item["column"])
    if key in cache:
        return cache[key]

    saved = gold_sql.get(item["id"])
    sql = saved["rows_sql"] if saved else None
    if sql is None:
        cid = item["expected_checks"][0]
        if cid not in SUPPORTED:
            cache[key] = None
            return None
        try:
            m = {"threshold_value": "threshold"}
            kw = {m.get(k, k): v for k, v in item.get("gold_params", {}).items()}
            sql = build(cid, item["table"], item["column"], **kw).rows_sql
        except Exception:  # noqa: BLE001
            cache[key] = None
            return None
    try:
        cache[key] = fetch_pks(conn, sql)
    except Exception:  # noqa: BLE001
        cache[key] = None
    return cache[key]


def judge(conn, data: dict, cache: dict, gold_sql: dict) -> dict:
    """항목별 hit 여부와 보조 지표를 산출."""
    hits: dict[str, bool] = {}
    meta: dict[str, dict] = {}
    tag_hits = tag_present = 0
    rules = sql_try = sql_pass = 0
    exec_total = exec_ok = 0
    elapsed = 0.0
    stage: dict[str, float] = defaultdict(float)

    for r in data["results"]:
        gid = r["id"]
        expected = r["expected_checks"][0]
        s_gold = gold_set(conn, r, cache, gold_sql)

        hit = False
        if s_gold is not None:
            for g in r["generated"]:
                if not g.get("sql"):
                    continue
                try:
                    s_gen = fetch_pks(conn, to_rowset_sql(g["sql"], r["table"]))
                except Exception:  # noqa: BLE001
                    continue
                if s_gen == s_gold:
                    hit = True
                    break
        hits[gid] = hit
        meta[gid] = {
            "check": expected,
            "dim": r["dimension"],
            "sub": r.get("subcategory", ""),
            "cplx": r["complexity"],
            "table": r["table"],
            "column": r["column"],
        }

        tags = {m.group(1) for g in r["generated"] if (m := _TAG.search(g["name"]))}
        if tags:
            tag_present += 1
        tag_hits += expected in tags

        rules += len(r["generated"])
        sql_try += r.get("sql_attempts", 0)
        sql_pass += r.get("sql_passed", 0)
        elapsed += r.get("elapsed_sec", 0)
        for k, v in (r.get("stage_timings") or {}).items():
            stage[k] += v
        for g in r["generated"]:
            if g.get("execution"):
                exec_total += 1
                exec_ok += bool(g["execution"]["success"])

    n = len(data["results"])
    return {
        "hits": hits,
        "meta": meta,
        "n": n,
        "ocr": sum(hits.values()) / n if n else 0,
        "tag_ocr": tag_hits / n if n else 0,
        "tag_rate": tag_present / n if n else 0,
        "rules": rules,
        "sql_try": sql_try,
        "sql_pass": sql_pass,
        "exec_total": exec_total,
        "exec_ok": exec_ok,
        "ct": elapsed / n if n else 0,
        "total_sec": data.get("total_sec", 0),
        "stage": {k: v / n for k, v in stage.items()} if n else {},
        "model": data.get("model", "?"),
    }


def rate(hits: dict, meta: dict, key: str, value: str) -> tuple[int, int]:
    ids = [g for g, m in meta.items() if m[key] == value]
    return sum(hits[g] for g in ids), len(ids)


def fmt(h: int, t: int) -> str:
    return f"{h / t:.3f} ({h}/{t})" if t else "—"


def delta(a: float, b: float) -> str:
    d = b - a
    if abs(d) < 0.001:
        return "  ="
    return f" {d:+.3f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file_a")
    ap.add_argument("file_b")
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    ap.add_argument("--diff", action="store_true", help="판정이 갈린 항목 나열")
    args = ap.parse_args()

    pa = Path(args.file_a)
    pb = Path(args.file_b)
    if not pa.exists():
        pa = EVAL_DIR / args.file_a
    if not pb.exists():
        pb = EVAL_DIR / args.file_b

    da = json.loads(pa.read_text(encoding="utf-8"))
    db = json.loads(pb.read_text(encoding="utf-8"))

    la = args.label_a or da.get("model", "A").split("/")[-1]
    lb = args.label_b or db.get("model", "B").split("/")[-1]

    # 골든셋 동일성 확인
    ids_a = [r["id"] for r in da["results"]]
    ids_b = [r["id"] for r in db["results"]]
    key_a = {(r["id"], r["table"], r["column"]) for r in da["results"]}
    key_b = {(r["id"], r["table"], r["column"]) for r in db["results"]}
    same = key_a == key_b

    print("=" * 66)
    print(f"평가 결과 비교   {la}  vs  {lb}")
    print("=" * 66)
    print(f"  A: {pa.name}  ({len(ids_a)}건, {da.get('model')})")
    print(f"  B: {pb.name}  ({len(ids_b)}건, {db.get('model')})")
    if same:
        print("  ✓ 동일 골든셋 — 비교 가능")
    else:
        only_a, only_b = key_a - key_b, key_b - key_a
        print(f"  ✗ 골든셋 불일치! A만 {len(only_a)}건 / B만 {len(only_b)}건")
        print("    → 항목 구성이 다르므로 직접 비교는 성립하지 않는다")

    conn = connect()
    try:
        cache: dict = {}
        gold_sql = load_gold()
        ra = judge(conn, da, cache, gold_sql)
        rb = judge(conn, db, cache, gold_sql)
    finally:
        conn.close()

    w = 16
    print(f"\n{'지표':<28}{la:>{w}}{lb:>{w}}{'차이':>9}")
    print("-" * 66)
    print(f"{'■ OCR (행 집합 등가)':<26}{ra['ocr']:>{w}.3f}{rb['ocr']:>{w}.3f}"
          f"{delta(ra['ocr'], rb['ocr']):>9}")
    print(f"{'  OCR (태그 기준)':<27}{ra['tag_ocr']:>{w}.3f}{rb['tag_ocr']:>{w}.3f}"
          f"{delta(ra['tag_ocr'], rb['tag_ocr']):>9}")
    print(f"{'  태그 부착률':<29}{ra['tag_rate']:>{w}.3f}{rb['tag_rate']:>{w}.3f}"
          f"{delta(ra['tag_rate'], rb['tag_rate']):>9}")

    print(f"\n{'DCR (Kahn 범주)':<28}{la:>{w}}{lb:>{w}}{'차이':>9}")
    print("-" * 66)
    for d in ("Completeness", "Conformance", "Plausibility"):
        ha, ta = rate(ra["hits"], ra["meta"], "dim", d)
        hb, tb = rate(rb["hits"], rb["meta"], "dim", d)
        va, vb = (ha / ta if ta else 0), (hb / tb if tb else 0)
        print(f"{DIM_KO[d]:<26}{fmt(ha, ta):>{w}}{fmt(hb, tb):>{w}}{delta(va, vb):>9}")

    subs = sorted({m["sub"] for m in ra["meta"].values() if m["sub"]})
    if subs:
        print("\n  하위 분류")
        for s in subs:
            ha, ta = rate(ra["hits"], ra["meta"], "sub", s)
            hb, tb = rate(rb["hits"], rb["meta"], "sub", s)
            va, vb = (ha / ta if ta else 0), (hb / tb if tb else 0)
            print(f"    {s:<22}{fmt(ha, ta):>{w}}{fmt(hb, tb):>{w}}{delta(va, vb):>9}")

    print(f"\n{'HCR (복잡도)':<28}{la:>{w}}{lb:>{w}}{'차이':>9}")
    print("-" * 66)
    for c in ("ST_SF", "ST_MF", "MT_MF"):
        ha, ta = rate(ra["hits"], ra["meta"], "cplx", c)
        hb, tb = rate(rb["hits"], rb["meta"], "cplx", c)
        va, vb = (ha / ta if ta else 0), (hb / tb if tb else 0)
        print(f"{CPLX_KO[c]:<24}{fmt(ha, ta):>{w}}{fmt(hb, tb):>{w}}{delta(va, vb):>9}")

    print(f"\n{'체크 유형별':<28}{la:>{w}}{lb:>{w}}{'차이':>9}")
    print("-" * 66)
    checks = sorted({m["check"] for m in ra["meta"].values()})
    rows = []
    for c in checks:
        ha, ta = rate(ra["hits"], ra["meta"], "check", c)
        hb, tb = rate(rb["hits"], rb["meta"], "check", c)
        va, vb = (ha / ta if ta else 0), (hb / tb if tb else 0)
        rows.append((vb - va, c, ha, ta, hb, tb, va, vb))
    for d, c, ha, ta, hb, tb, va, vb in sorted(rows, key=lambda x: -x[0]):
        mark = "▲" if d > 0.001 else ("▼" if d < -0.001 else " ")
        print(f"{mark} {c:<34}{fmt(ha, ta):>{w - 2}}{fmt(hb, tb):>{w}}{delta(va, vb):>9}")

    print(f"\n{'보조 지표':<28}{la:>{w}}{lb:>{w}}")
    print("-" * 66)
    def line(label, a, b):
        print(f"{label:<28}{a:>{w}}{b:>{w}}")
    line("생성 규칙 총계", f"{ra['rules']}건", f"{rb['rules']}건")
    line("  질의당", f"{ra['rules']/ra['n']:.1f}", f"{rb['rules']/rb['n']:.1f}")
    line("정적검사 통과율",
         f"{ra['sql_pass']/ra['sql_try']:.3f}" if ra["sql_try"] else "—",
         f"{rb['sql_pass']/rb['sql_try']:.3f}" if rb["sql_try"] else "—")
    line("실행 성공률",
         f"{ra['exec_ok']}/{ra['exec_total']}" if ra["exec_total"] else "— (DB 미연결)",
         f"{rb['exec_ok']}/{rb['exec_total']}" if rb["exec_total"] else "— (DB 미연결)")
    line("CT (질의당)", f"{ra['ct']:.1f}초", f"{rb['ct']:.1f}초")
    line("총 소요", f"{ra['total_sec']:,.0f}초", f"{rb['total_sec']:,.0f}초")

    stages = ["input", "rule_generation", "deduplicate", "sql_generation",
              "execution", "refine"]
    ko = {"input": "입력 처리", "rule_generation": "규칙 생성",
          "deduplicate": "중복 제거", "sql_generation": "SQL 생성",
          "execution": "실행 검증", "refine": "복구"}
    print(f"\n{'단계별 시간 (질의당)':<28}{la:>{w}}{lb:>{w}}")
    print("-" * 66)
    for s in stages:
        va, vb = ra["stage"].get(s), rb["stage"].get(s)
        if va is None and vb is None:
            continue
        line(f"  {ko[s]}",
             f"{va:.2f}초" if va is not None else "—",
             f"{vb:.2f}초" if vb is not None else "—")

    if ra["exec_total"] == 0 or rb["exec_total"] == 0:
        print("\n※ 한쪽이 DB 미연결로 수행되어 실행 검증·CT 비교는 조건이 다르다.")

    if args.diff:
        only_a = [g for g in ra["hits"] if ra["hits"][g] and not rb["hits"].get(g)]
        only_b = [g for g in rb["hits"] if rb["hits"][g] and not ra["hits"].get(g)]
        print(f"\n{la}만 정답: {len(only_a)}건")
        for g in only_a[:20]:
            m = ra["meta"][g]
            print(f"  [{g}] {m['check']:32s} {m['table']}.{m['column']}")
        print(f"\n{lb}만 정답: {len(only_b)}건")
        for g in only_b[:20]:
            m = rb["meta"][g]
            print(f"  [{g}] {m['check']:32s} {m['table']}.{m['column']}")


if __name__ == "__main__":
    main()