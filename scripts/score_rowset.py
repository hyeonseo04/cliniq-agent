"""행 집합 실행 등가 기반 재채점 — 태그 의존을 제거한 주 채점기.
리포 경로: scripts/score_rowset.py

배경:
    태그(`[지표: X]`) 기반 채점은 두 가지 문제가 있다.
      1) 태그는 우리가 프롬프트로 붙이라고 시킨 인위적 장치다 (자기충족적 채점)
      2) ablation의 −DQD 팔에는 태그가 존재하지 않아 팔 간 비교가 불가능하다
    따라서 hit 판정을 DQD 공식 정의와의 '위반 행 집합 일치'로 조작화한다.

판정:
    hit ⇔ 질의에 대해 생성된 규칙 중 하나라도
          위반 행 PK 집합이 해당 기대 체크의 골드 집합과 일치

사용법:
    uv run python scripts/score_rowset.py                      # 최신 결과 재채점
    uv run python scripts/score_rowset.py eval_20260822.json
    uv run python scripts/score_rowset.py --detail             # 불일치 상세

생성 결과 파일을 재사용하므로 LLM 재실행이 불필요하다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
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
    "Completeness": "Completeness (완전성)",
    "Conformance": "Conformance (정합성)",
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


def load_gold_sql() -> dict:
    """저장된 골드 SQL을 읽는다.

    build_gold_sql.py가 미리 생성해 둔 파일을 사용하므로
    채점 때마다 재생성하지 않는다 (재현성 + 검토 가능성).
    파일이 없으면 즉석 생성으로 폴백한다.
    """
    if GOLD_SQL.exists():
        return json.loads(GOLD_SQL.read_text(encoding="utf-8")).get("queries", {})
    return {}


def gold_rows(
    conn, item: dict, cache: dict, gold_sql: dict
) -> tuple[set | None, str | None]:
    """기대 체크의 골드 위반 행 집합. (집합, 오류메시지)"""
    check_id = item["expected_checks"][0]
    key = (check_id, item["table"], item["column"])
    if key in cache:
        return cache[key]

    # 저장된 골드 SQL 우선 사용
    saved = gold_sql.get(item["id"])
    sql = saved["rows_sql"] if saved else None

    if sql is None:
        if check_id not in SUPPORTED:
            cache[key] = (None, f"미지원 체크: {check_id}")
            return cache[key]
        try:
            sql = build(check_id, item["table"], item["column"],
                        **_to_build_kwargs(item.get("gold_params", {}))).rows_sql
        except Exception as e:  # noqa: BLE001
            cache[key] = (None, f"골드 SQL 생성 실패: {str(e)[:80]}")
            return cache[key]

    try:
        result = (fetch_pks(conn, sql), None)
    except Exception as e:  # noqa: BLE001
        result = (None, f"골드 실행 실패: {str(e)[:80]}")

    cache[key] = result
    return result


def _to_build_kwargs(params: dict) -> dict:
    """골든셋의 gold_params → dqd_gold_sql.build()의 인자명으로 변환."""
    m = {"threshold_value": "threshold"}
    return {m.get(k, k): v for k, v in params.items()}


def gen_rows(conn, rule: dict, table: str) -> tuple[set | None, str | None]:
    """생성 규칙의 위반 행 집합."""
    if not rule.get("sql"):
        return None, "SQL 없음"
    try:
        return fetch_pks(conn, to_rowset_sql(rule["sql"], table)), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:70]}"


def score(data: dict, conn, detail: bool = False) -> dict:
    results = data["results"]
    n = len(results)
    cache: dict = {}
    gold_sql = load_gold_sql()

    hits_row = hits_tag = 0
    tag_present = 0
    by_dim: dict = defaultdict(lambda: [0, 0])
    by_sub: dict = defaultdict(lambda: [0, 0])
    by_cplx: dict = defaultdict(lambda: [0, 0])
    by_check: dict = defaultdict(lambda: [0, 0])
    gold_fail = agree = both_judged = 0
    misses: list = []

    for r in results:
        expected = r["expected_checks"][0]
        table = r["table"]

        # (1) 태그 기반 판정 (비교용)
        tags = {m.group(1) for g in r["generated"]
                if (m := _TAG.search(g["name"]))}
        tag_hit = expected in tags
        if tags:
            tag_present += 1
        hits_tag += tag_hit

        # (2) 행 집합 기반 판정 (주 지표)
        s_gold, err = gold_rows(conn, r, cache, gold_sql)
        row_hit = False
        detail_note = err or ""

        if s_gold is None:
            gold_fail += 1
        else:
            for g in r["generated"]:
                s_gen, gerr = gen_rows(conn, g, table)
                if s_gen is None:
                    detail_note = detail_note or gerr or ""
                    continue
                if s_gen == s_gold:
                    row_hit = True
                    break
                detail_note = (
                    f"골드 {len(s_gold)}건 vs 생성 {len(s_gen)}건 "
                    f"(교집합 {len(s_gold & s_gen)})"
                )

        hits_row += row_hit
        by_dim[r["dimension"]][0] += row_hit
        by_dim[r["dimension"]][1] += 1
        sub = f'{r["dimension"]}/{r.get("subcategory") or "-"}'
        by_sub[sub][0] += row_hit
        by_sub[sub][1] += 1
        by_cplx[r["complexity"]][0] += row_hit
        by_cplx[r["complexity"]][1] += 1
        by_check[expected][0] += row_hit
        by_check[expected][1] += 1

        # 태그 판정과 행 집합 판정의 일치도 (태그 채점기의 신뢰도 추정)
        if s_gold is not None:
            both_judged += 1
            agree += (row_hit == tag_hit)

        if not row_hit:
            misses.append({
                **r,
                "_note": detail_note,
                "_tag_hit": tag_hit,
                "_gold_count": len(s_gold) if s_gold is not None else None,
            })

    return {
        "n": n,
        "hits_row": hits_row,
        "hits_tag": hits_tag,
        "tag_present": tag_present,
        "by_dim": dict(by_dim),
        "by_sub": dict(by_sub),
        "by_cplx": dict(by_cplx),
        "by_check": dict(by_check),
        "gold_fail": gold_fail,
        "agree": agree,
        "both_judged": both_judged,
        "misses": misses,
    }


def pct(h: int, t: int) -> str:
    return f"{h / t:.3f} ({h}/{t})" if t else "—  (0/0)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--tag", default=None,
                    help="특정 모델 결과만 채점 (예: --tag Qwen3-14B)")
    ap.add_argument("--exclude-errors", action="store_true",
                    help="타임아웃 등 실행 오류 항목을 분모에서 제외")
    args = ap.parse_args()

    pattern = f"eval_{args.tag}_*.json" if args.tag else "eval_*.json"
    files = sorted(EVAL_DIR.glob(pattern), reverse=True)
    if not files:
        raise SystemExit(f"평가 결과가 없다: {EVAL_DIR}")
    path = (EVAL_DIR / args.file) if args.file else files[0]
    data = json.loads(path.read_text(encoding="utf-8"))

    if args.exclude_errors:
        before = len(data["results"])
        data["results"] = [r for r in data["results"] if not r.get("error")]
        print(f"실행 오류 {before - len(data['results'])}건 제외\n")

    conn = connect()
    try:
        s = score(data, conn, args.detail)
    finally:
        conn.close()

    n = s["n"]
    src = "저장된 골드 SQL" if GOLD_SQL.exists() else "즉석 생성"
    print(f"평가 결과: {path.name}   (행 집합 실행 등가 기준, 골드: {src})")
    print(f"모델: {data.get('model')}  |  골든셋 {n}건")
    print("=" * 62)

    print(f"\n■ OCR (행 집합 실행 등가)   {pct(s['hits_row'], n)}   ← 주 지표")
    print(f"  OCR (태그 기준, 참고)     {pct(s['hits_tag'], n)}")
    print(f"  태그 부착률               {pct(s['tag_present'], n)}")
    if s["both_judged"]:
        print(f"  두 채점기 판정 일치율     {pct(s['agree'], s['both_judged'])}")
    if s["gold_fail"]:
        print(f"  ※ 골드 실행 불가 {s['gold_fail']}건 (분모에는 포함)")

    print("\nDimension Coverage Rate (DCR) — Kahn 분류")
    for d, (h, t) in sorted(s["by_dim"].items()):
        print(f"  {DIM_KO.get(d, d):24s} {pct(h, t)}")
    print("\n  하위 분류")
    for d, (h, t) in sorted(s["by_sub"].items()):
        print(f"    {d:28s} {pct(h, t)}")

    print("\nHierarchy Coverage Rate (HCR)")
    for c in ("ST_SF", "ST_MF", "MT_MF"):
        if c in s["by_cplx"]:
            h, t = s["by_cplx"][c]
            print(f"  {CPLX_KO[c]:18s} {pct(h, t)}")

    print("\n체크 유형별 커버리지")
    for c, (h, t) in sorted(s["by_check"].items(), key=lambda x: -x[1][1]):
        bar = "█" * round(10 * h / t) if t else ""
        print(f"  {c:36s} {pct(h, t):14s} {bar}")

    if args.detail and s["misses"]:
        print(f"\n불일치 {len(s['misses'])}건")
        for r in s["misses"][:40]:
            mark = "태그○" if r["_tag_hit"] else "태그✗"
            print(f"  [{r['id']}] {r['table']}.{r['column']}  {r['expected_checks'][0]}  [{mark}]")
            if r["_note"]:
                print(f"       {r['_note']}")


if __name__ == "__main__":
    main()