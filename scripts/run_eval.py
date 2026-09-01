"""골든셋 배치 실행 — 각 항목을 에이전트에 던지고 결과를 수집한다.
리포 경로: scripts/run_eval.py

사용법:
    uv run python scripts/run_eval.py                 # 전체 실행
    uv run python scripts/run_eval.py --limit 10      # 앞 10건만 (사전 점검용)
    uv run python scripts/run_eval.py --no-db         # DB 검증 없이 (빠름)

# 태그 직접 지정
uv run python scripts/run_eval.py --tag 14B-test

출력:
    results/eval/eval_<timestamp>.json
    → scripts/score_eval.py 로 채점

주의:
    100건이면 LLM 호출이 200회 이상이다. 먼저 --limit 5 로 동작을 확인하라.
    실행 중 규칙 저장(SAVE_RULES)은 자동으로 꺼진다 — 카탈로그 오염 방지.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ["SAVE_RULES"] = "false"  # 평가 중에는 카탈로그에 쌓지 않는다

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING)

import config  # noqa: E402
from agents.deduplicate_agent import DeduplicateAgent  # noqa: E402
from agents.execution_agent import ExecutionAgent  # noqa: E402
from agents.input_agent import InputAgent  # noqa: E402
from agents.orchestrator_agent import OrchestratorAgent  # noqa: E402
from agents.refine_agent import RefineAgent  # noqa: E402
from agents.rule_agent import RuleAgent  # noqa: E402
from agents.sql_agent import SQLAgent  # noqa: E402
from core.catalog import RuleCatalog  # noqa: E402
from core.db import OracleDB  # noqa: E402
from core.embeddings import get_embedder  # noqa: E402
from core.llm import get_llm  # noqa: E402
from models.intent import EvalAspect, Intent, IntentResult, TargetSpec  # noqa: E402
from models.state import PipelineState  # noqa: E402
from pipeline.graph import CreativePipeline  # noqa: E402

GOLDEN = config.DATA_DIR / "golden_set" / "golden_set.json"
OUT_DIR = config.ROOT / "results" / "eval"

ASPECT_MAP = {
    "결측": EvalAspect.MISSING,
    "필수·참조 무결성": EvalAspect.REFERENTIAL,
    "표준 개념": EvalAspect.STANDARD,
    "값 범위": EvalAspect.VALUE_RANGE,
    "시간 타당성": EvalAspect.TEMPORAL,
    "임상 타당성": EvalAspect.CLINICAL,
}


def build_pipeline(use_db: bool):
    llm = get_llm()
    execution = refine = None
    if use_db and config.ORACLE_DSN.get("integrated"):
        db = OracleDB()
        if db.ping():
            execution = ExecutionAgent(db=db, explain_only=config.EXPLAIN_ONLY)
            refine = RefineAgent(llm=llm)
            print(f"Oracle 연결됨 (explain_only={config.EXPLAIN_ONLY})")
    return CreativePipeline(
        input_agent=InputAgent(),
        rule=RuleAgent(llm=llm),
        dedup=DeduplicateAgent(embedder=get_embedder("hashing"), llm=llm),
        sql=SQLAgent(llm=llm),
        execution=execution,
        refine=refine,
        catalog=RuleCatalog(),
    )


def run_one(pipeline: CreativePipeline, item: dict) -> dict:
    """골든셋 항목 1건 실행. planner를 건너뛰고 슬롯을 직접 구성한다
    (평가 대상은 규칙 생성 품질이지 의도 분류가 아니므로)."""
    intent = IntentResult(
        intent=Intent.CREATIVE,
        request_summary=item["query"],
        targets=[TargetSpec(table=item["table"], columns=[item["column"]])],
        aspects=[
            EvalAspect(item["aspect_code"])
            if item.get("aspect_code")
            else ASPECT_MAP.get(item["aspect"], EvalAspect.OTHER)
        ],
    )

    t0 = time.perf_counter()
    try:
        state = pipeline.run(PipelineState(user_query=item["query"], intent=intent))
        elapsed = time.perf_counter() - t0
    except Exception as e:  # noqa: BLE001
        return {
            **item,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "generated": [],
        }

    execs = {e.rule_id: e for e in state.executions}
    return {
        **item,
        "elapsed_sec": round(elapsed, 2),
        "aborted": state.aborted,
        "abort_reason": state.abort_reason,
        "sql_attempts": len(state.static_checks),
        "sql_passed": sum(1 for c in state.static_checks if c.passed),
        "dedup_removed": len(state.dedup.removals) if state.dedup else 0,
        "refine_count": state.refine_count,
        "stage_timings": state.stage_timings,
        "generated": [
            {
                "rule_id": r.rule_id,
                "name": r.name,
                "dimension": r.dq_dimension.value,
                "complexity": r.complexity.value,
                "logic_nl": r.logic_nl,
                "sql": r.sql,
                "status": r.status.value,
                "execution": (
                    {
                        "success": execs[r.rule_id].success,
                        "violation_count": execs[r.rule_id].violation_count,
                        "denominator_count": execs[r.rule_id].denominator_count,
                        "violation_ratio": execs[r.rule_id].violation_ratio,
                        "error": execs[r.rule_id].error_message,
                    }
                    if r.rule_id in execs
                    else None
                ),
            }
            for r in state.generated_rules
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="앞 N건만 실행")
    ap.add_argument("--start", type=int, default=0, help="N번째부터 실행 (이어하기)")
    ap.add_argument("--no-db", action="store_true", help="DB 검증 없이 실행")
    ap.add_argument("--retries", type=int, default=2,
                    help="항목별 재시도 횟수 (타임아웃 대응)")
    ap.add_argument("--tag", default=None,
                    help="결과 파일명에 붙일 식별자 (미지정 시 모델명에서 자동 생성)")
    args = ap.parse_args()

    if not GOLDEN.exists():
        print(f"골든셋이 없다: {GOLDEN}\n먼저 build_golden_set.py 를 실행하라.")
        return

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    items = golden["items"][args.start :]
    if args.limit:
        items = items[: args.limit]
    print(f"골든셋 {len(items)}건 실행 시작\n")

    pipeline = build_pipeline(not args.no_db)
    results = []
    t_start = time.perf_counter()

    for i, item in enumerate(items, 1):
        # 타임아웃은 서버 일시적 문제인 경우가 많아 재시도한다
        # (2시간 연속 실행 시 마지막 구간이 연쇄 실패하는 현상 대응)
        for attempt in range(args.retries + 1):
            r = run_one(pipeline, item)
            err = r.get("error") or ""
            if "Timeout" not in err and "timed out" not in err.lower():
                break
            if attempt < args.retries:
                wait = 10 * (attempt + 1)
                print(f"      ↻ 타임아웃 — {wait}초 후 재시도 ({attempt + 1}/{args.retries})")
                time.sleep(wait)
        results.append(r)

        # 중간 체크포인트 (긴 실행이 끊겨도 결과 보존)
        if i % 10 == 0:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / f"_checkpoint_{args.tag or 'run'}.json").write_text(
                json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8"
            )
        n = len(r["generated"])
        mark = "✗" if r.get("error") or r.get("aborted") else "✓"
        print(
            f"  [{i:3d}/{len(items)}] {mark} {item['table']}.{item['column']:32s} "
            f"{item['expected_checks'][0]:34s} 규칙 {n}건 ({r['elapsed_sec']}s)"
        )

    total = time.perf_counter() - t_start
    # 결과 파일명에 모델 식별자를 넣는다.
    # 같은 골든셋을 여러 모델로 돌릴 때 결과가 섞이지 않도록.
    tag = args.tag or config.VLLM_MODEL.split("/")[-1].replace(".", "-")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"eval_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "run_at": datetime.now().isoformat(timespec="seconds"),
                "model": config.VLLM_MODEL,
                "golden_size": len(items),
                "total_sec": round(total, 1),
                "db_verified": not args.no_db,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n총 {total:.1f}초 (건당 평균 {total / len(items):.1f}초)")
    print(f"→ {path}")
    print(f"\n채점: uv run python scripts/score_rowset.py {path.name}")


if __name__ == "__main__":
    main()