"""평가 결과 채점 — LLM-DQR 논문의 지표 체계를 따른다.
리포 경로: scripts/score_eval.py

사용법:
    uv run python scripts/score_eval.py                        # 최신 결과 채점
    uv run python scripts/score_eval.py eval_20260817_1900.json
    uv run python scripts/score_eval.py --detail               # 미검출 항목 나열

지표:
    OCR (Overall Coverage Rate)   — 정답 체크를 생성한 비율 (논문 핵심 지표)
    DCR (Dimension Coverage Rate) — 품질 차원별 커버리지
    HCR (Hierarchy Coverage Rate) — 복잡도 계층별 커버리지
    CT  (Construction Time)       — 규칙 1건당 생성 시간
    + 정적검사 1회 통과율, 실행 성공률, extra rules

일치 판정:
    생성 규칙 이름의 `[지표: check_id]` 태그가 정답의 expected_checks와 일치하면 hit.
    태그가 없는 자유 생성 규칙은 extra rule로 별도 집계한다
    (논문도 89건의 extra rule을 성과로 보고했다).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402

EVAL_DIR = config.ROOT / "results" / "eval"
_TAG = re.compile(r"\[지표:\s*([A-Za-z0-9_]+)\]")


def load(name: str | None) -> tuple[Path, dict]:
    pattern = f"eval_{args.tag}_*.json" if args.tag else "eval_*.json"
    files = sorted(EVAL_DIR.glob(pattern), reverse=True)
    if not files:
        raise SystemExit(f"평가 결과가 없다: {EVAL_DIR}")
    path = (EVAL_DIR / name) if name else files[0]
    if not path.exists():
        raise SystemExit(f"파일을 찾을 수 없다: {path}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def tags_of(result: dict) -> set[str]:
    out: set[str] = set()
    for g in result["generated"]:
        m = _TAG.search(g["name"])
        if m:
            out.add(m.group(1))
    return out


def pct(hit: int, total: int) -> str:
    return f"{hit / total:.3f} ({hit}/{total})" if total else "—  (0/0)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="채점할 결과 파일명")
    ap.add_argument("--detail", action="store_true", help="미검출 항목 나열")
    ap.add_argument("--tag", default=None,
                    help="특정 모델 결과만 채점 (예: --tag Qwen3-14B)")
    args = ap.parse_args()

    path, data = load(args.file)
    results = data["results"]
    n = len(results)

    hits = 0
    by_dim: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # [hit, total]
    by_cplx: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_check: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    missed: list[dict] = []

    rules_total = extra_total = 0
    sql_attempts = sql_passed = 0
    exec_total = exec_ok = 0
    aborted = errored = 0
    dedup_removed = refine_used = 0
    elapsed_sum = 0.0

    for r in results:
        expected = set(r["expected_checks"])
        found = tags_of(r)
        hit = bool(expected & found)

        hits += hit
        by_dim[r["dimension"]][1] += 1
        by_dim[r["dimension"]][0] += hit
        by_cplx[r["complexity"]][1] += 1
        by_cplx[r["complexity"]][0] += hit
        for c in expected:
            by_check[c][1] += 1
            by_check[c][0] += hit
        if not hit:
            missed.append(r)

        gen = r["generated"]
        rules_total += len(gen)
        extra_total += sum(1 for g in gen if not _TAG.search(g["name"]))
        sql_attempts += r.get("sql_attempts", 0)
        sql_passed += r.get("sql_passed", 0)
        dedup_removed += r.get("dedup_removed", 0)
        refine_used += 1 if r.get("refine_count") else 0
        elapsed_sum += r.get("elapsed_sec", 0)
        if r.get("error"):
            errored += 1
        elif r.get("aborted"):
            aborted += 1
        for g in gen:
            if g.get("execution"):
                exec_total += 1
                exec_ok += bool(g["execution"]["success"])

    print(f"평가 결과: {path.name}")
    print(f"모델: {data.get('model')}  |  실행: {data.get('run_at')}")
    print(f"골든셋 {n}건  |  총 {data.get('total_sec')}초")
    print("=" * 62)

    print(f"\nOverall Coverage Rate (OCR)   {pct(hits, n)}")

    print("\nDimension Coverage Rate (DCR) — Kahn 범주")
    ko = {"Completeness": "Completeness", "Conformance": "Conformance",
          "Plausibility": "Plausibility"}
    for d, (h, t) in sorted(by_dim.items()):
        print(f"  {ko.get(d, d):8s} {pct(h, t)}")

    print("\nHierarchy Coverage Rate (HCR)")
    label = {"ST_SF": "단일테이블·단일필드", "ST_MF": "단일테이블·다중필드",
             "MT_MF": "다중테이블·다중필드"}
    for c in ("ST_SF", "ST_MF", "MT_MF"):
        if c in by_cplx:
            h, t = by_cplx[c]
            print(f"  {label[c]:18s} {pct(h, t)}")

    print("\n생성·검증")
    print(f"  생성 규칙 총계          {rules_total}건 (질의당 {rules_total / n:.1f})")
    print(f"  표준 지표 외 규칙       {extra_total}건 (extra rules)")
    print(f"  정적검사 1회 통과율     {pct(n, sql_attempts) if sql_attempts else '—'}")
    print(f"  실행 성공률             {pct(exec_ok, exec_total)}")
    print(f"  중복 제거               {dedup_removed}건")
    print(f"  Refine 발동             {refine_used}건")
    print(f"  중단/오류               {aborted}건 / {errored}건")
    print(f"  질의당 평균 시간 (CT)   {elapsed_sum / n:.1f}초")

    # 단계별 소요 시간
    stage_sum: dict[str, float] = defaultdict(float)
    stage_cnt: dict[str, int] = defaultdict(int)
    for r in results:
        for k, v in (r.get("stage_timings") or {}).items():
            stage_sum[k] += v
            stage_cnt[k] += 1
    if stage_sum:
        order = ["input", "rule_generation", "deduplicate", "sql_generation",
                 "execution", "refine"]
        ko = {"input": "입력 처리", "rule_generation": "규칙 생성",
              "deduplicate": "중복 제거", "sql_generation": "SQL 생성",
              "execution": "실행 검증", "refine": "복구"}
        total = sum(stage_sum.values())
        print("\n단계별 소요 시간 (질의당 평균)")
        for k in order:
            if k not in stage_sum:
                continue
            avg = stage_sum[k] / n
            share = stage_sum[k] / total if total else 0
            bar = "█" * max(1, round(share * 20))
            print(f"  {ko[k]:10s} {avg:6.2f}초  {share:5.1%}  {bar}"
                  f"   (발생 {stage_cnt[k]}/{n}건)")

    print("\n체크 유형별 커버리지")
    for c, (h, t) in sorted(by_check.items(), key=lambda x: -x[1][1]):
        bar = "█" * round(10 * h / t) if t else ""
        print(f"  {c:36s} {pct(h, t):14s} {bar}")

    if args.detail and missed:
        print(f"\n미검출 {len(missed)}건")
        for r in missed:
            got = ", ".join(tags_of(r)) or "(태그 없음)"
            reason = r.get("abort_reason") or r.get("error") or ""
            print(f"  [{r['id']}] {r['table']}.{r['column']}")
            print(f"       기대: {r['expected_checks'][0]}  →  생성: {got}")
            if reason:
                print(f"       사유: {reason[:90]}")


if __name__ == "__main__":
    main()