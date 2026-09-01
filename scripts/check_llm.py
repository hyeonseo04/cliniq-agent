"""vLLM 서버 연결·구조화 출력 진단 스크립트 (에이전트 실행 전 점검용).
리포 경로: scripts/check_llm.py
사용법: uv run python scripts/check_llm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import config  # noqa: E402
from core.llm import get_llm, harden_schema  # noqa: E402
from models.intent import IntentResult  # noqa: E402


def main() -> None:
    print(f"[설정] base_url={config.VLLM_BASE_URL}  model={config.VLLM_MODEL}")
    llm = get_llm()

    # 1) 서버 연결 + 모델 목록
    try:
        models = llm.client.models.list()
        served = [m.id for m in models.data]
        print(f"[1] 서버 연결 OK. 서빙 중 모델: {served}")
        if config.VLLM_MODEL not in served:
            print(f"    ⚠ config의 VLLM_MODEL({config.VLLM_MODEL})이 목록에 없다. "
                  f"VLLM_MODEL 환경변수를 {served[0]!r}로 맞춰라.")
    except Exception as e:  # noqa: BLE001
        print(f"[1] ✗ 서버 연결 실패: {e}")
        print("    vLLM 서버가 떠 있는지, VLLM_BASE_URL이 맞는지 확인하라.")
        return

    # 2) 평문 생성
    try:
        out = llm.text("한국어로 간결히 답하라.", "OMOP CDM이 무엇인지 한 문장으로 설명하라.")
        print(f"[2] 평문 생성 OK: {out[:120]}")
    except Exception as e:  # noqa: BLE001
        print(f"[2] ✗ 평문 생성 실패: {e}")
        return

    # 3) 구조화 출력 (실패 시 폴백 동작까지 확인)
    try:
        r = llm.structured(
            "당신은 라우터다. 평면 JSON만 출력하라.",
            "사용자 발화: '통합DB에서 measurement 음수값 평가해줘'",
            IntentResult,
        )
        mode = "guided_json(구버전 폴백)" if llm._use_guided_json else "response_format(json_schema)"
        print(f"[3] 구조화 출력 OK [{mode}]")
        print(f"    intent={r.intent.value} target_db={r.target_db} req={r.request_summary!r}")
    except Exception as e:  # noqa: BLE001
        print(f"[3] ✗ 구조화 출력 실패: {e}")
        print("    → vLLM을 --structured-outputs-config.backend xgrammar 로 재기동해보라.")
        return

    print("\n모든 점검 통과. main.py 실행 가능.")


if __name__ == "__main__":
    main()