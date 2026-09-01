"""전역 설정 — 전부 환경변수로 오버라이드 가능 (배포 대비).

.env 예시:
    VLLM_BASE_URL=http://localhost:8000/v1
    VLLM_MODEL=Qwen/Qwen3-32B
    LLM_TEMPERATURE=0.0
    EMBED_MODEL=BAAI/bge-m3
    ORACLE_DSN_INTEGRATED=...   # Phase 4
"""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()          # ← 반드시 여기

ROOT = Path(__file__).resolve().parent

# ---------- LLM (로컬 vLLM, OpenAI 호환 서버) ----------
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")  # vLLM 기본값
VLLM_MODEL = os.getenv("VLLM_MODEL", "Qwen/Qwen3-14B")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))  # 논문과 동일: 재현성
#: 생성 토큰 상한. 프롬프트 + 이 값이 vLLM의 --max-model-len을 넘으면 400 오류가 난다.
#: (8192 컨텍스트에서 프롬프트가 5천 토큰이면 3천 이하로 두어야 안전)
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "300"))

# ---------- 임베딩 (RAG + Dedup 1단계) ----------
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")  # 한국어 질의 ↔ 영어 스키마
DEDUP_SIMILARITY_THRESHOLD = float(os.getenv("DEDUP_SIM_THRESHOLD", "0.95"))  # 논문 τ

# ---------- 경로 ----------
DATA_DIR = ROOT / "data"
SCHEMA_JSON = DATA_DIR / "cdm" / "schema" / "cdm_schema.json"
SCHEMA_GRAPH_JSON = DATA_DIR / "cdm" / "schema" / "cdm_schema_graph.json"
CHROMA_DIR = DATA_DIR / "chroma"

# ---------- Oracle (Phase 4에서 사용, 지금은 미접속) ----------
ORACLE_DSN = {
    "integrated": os.getenv("ORACLE_DSN_INTEGRATED", ""),
    "preprocessed": os.getenv("ORACLE_DSN_PREPROCESSED", ""),
    "linked": os.getenv("ORACLE_DSN_LINKED", ""),
}
ORACLE_USER = os.getenv("ORACLE_USER", "")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "")
SQL_TIMEOUT_S = int(os.getenv("SQL_TIMEOUT_S", "60"))

#: EXPLAIN만 할지(True), 실제 실행까지 할지(False).
#: 빈 스키마에서는 True, 샘플 데이터 적재 후 False로 전환한다.
EXPLAIN_ONLY = os.getenv("EXPLAIN_ONLY", "true").lower() in {"1", "true", "yes"}

#: 생성된 규칙을 파일로 저장할지. results/rules/ 스냅샷 + data/rule/catalog.json 누적
SAVE_RULES = os.getenv("SAVE_RULES", "true").lower() in {"1", "true", "yes"}