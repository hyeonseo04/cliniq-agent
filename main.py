"""cliniq-agent 진입점 (대화 루프). 리포 경로: main.py

사전 준비:
  1) uv sync
  2) uv run python scripts/build_rag_index.py   # 최초 1회 (bge-m3 다운로드)
  3) vLLM 서버 실행:
     VLLM_WORKER_MULTIPROC_METHOD=spawn uv run vllm serve Qwen/Qwen3-32B \
         --max-model-len 16384 --tensor-parallel-size 2 --gpu-memory-utilization 0.90
실행:
  uv run python scripts/check_llm.py   # 서버 점검 (권장)
  uv run python main.py
"""
from __future__ import annotations

import importlib.util
import readline  # noqa: F401 — 입력 시 화살표키가 ^[[D 로 들어가는 문제 방지
import sys
from pathlib import Path

# --- torchcodec 감지 차단 (transformers가 없는 torchcodec을 import 시도해 나는 에러 방지) ---
_orig_find_spec = importlib.util.find_spec


def _patched_find_spec(name, package=None):
    if name == "torchcodec" or name.startswith("torchcodec."):
        return None  # transformers에 torchcodec이 없다고 응답
    return _orig_find_spec(name, package)


importlib.util.find_spec = _patched_find_spec
sys.modules["torchcodec"] = None  # 직접 import 시도 시 안전하게 ImportError 발생
# ---------------------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import logging  # noqa: E402

import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
# httpx 요청 로그가 대화 흐름을 가려서 낮춤 (디버깅 시 INFO로 올릴 것)
logging.getLogger("httpx").setLevel(logging.WARNING)

from agents.deduplicate_agent import DeduplicateAgent  # noqa: E402
from agents.orchestrator_agent import OrchestratorAgent  # noqa: E402
from agents.rule_agent import RuleAgent  # noqa: E402
from agents.execution_agent import ExecutionAgent  # noqa: E402
from agents.input_agent import InputAgent  # noqa: E402
from agents.refine_agent import RefineAgent  # noqa: E402
from agents.sql_agent import SQLAgent  # noqa: E402
from core.catalog import RuleCatalog  # noqa: E402
from core.embeddings import get_embedder  # noqa: E402
from core.db import OracleDB  # noqa: E402
from core.llm import get_llm  # noqa: E402
from pipeline.graph import CreativePipeline  # noqa: E402


def build_app() -> OrchestratorAgent:
    llm = get_llm()
    embedder = get_embedder("hashing")  # Deduplicate 유사도 계산용
    catalog = RuleCatalog()

    # Oracle DSN이 설정되어 있으면 EXPLAIN 검증 단계를 파이프라인에 추가
    execution = None
    refine = None
    if config.ORACLE_DSN.get("integrated"):
        try:
            db = OracleDB()
            if db.ping():
                execution = ExecutionAgent(db=db, explain_only=config.EXPLAIN_ONLY)
                mode = "EXPLAIN 검증" if config.EXPLAIN_ONLY else "실행 검증(위반율 산출)"
                refine = RefineAgent(llm=llm)
                print(f"Oracle 연결됨 — {mode} 활성화 (Refine 포함)")
            else:
                print("Oracle 연결 실패 — EXPLAIN 검증 없이 진행")
        except Exception as e:  # noqa: BLE001
            print(f"Oracle 초기화 실패({e}) — EXPLAIN 검증 없이 진행")

    pipeline = CreativePipeline(
        input_agent=InputAgent(),
        rule=RuleAgent(llm=llm),
        dedup=DeduplicateAgent(embedder=embedder, llm=llm),
        sql=SQLAgent(llm=llm),
        execution=execution,
        refine=refine,
        catalog=catalog,
    )
    return OrchestratorAgent(pipeline=pipeline, catalog=catalog, llm=llm)


def main() -> None:
    app = build_app()
    print("CliniQ 데이터 품질 지표 생성 에이전트")
    print("평가할 테이블·컬럼과 관점을 알려주세요. (종료: exit / 대화 초기화: reset)")
    while True:
        try:
            query = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() in {"exit", "quit"}:
            break
        if query.lower() == "reset":
            app.reset()
            print("\n대화를 초기화했습니다.")
            continue
        try:
            answer = app.handle(query)
            print("\n" + answer.answer)
        except Exception as e:  # noqa: BLE001
            print(f"\n[오류] {e}")


if __name__ == "__main__":
    main()