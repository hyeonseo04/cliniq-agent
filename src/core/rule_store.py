"""생성된 규칙 저장소. 리포 경로: src/core/rule_store.py

파이프라인이 만든 규칙을 파일로 남긴다. 두 가지 형식으로 저장한다:
  1) results/rules/<timestamp>_<id>.json — 실행 단위 스냅샷 (규칙 + 실행 결과)
  2) data/rule/catalog.json              — 누적 카탈로그 (rule_id 기준 갱신)

카탈로그는 타 파트 담당이므로 이 파일은 '인계 전 임시 저장소'다.
저장 형식은 models.QualityRule 그대로여서 그대로 읽어 재사용할 수 있다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import config
from models.rule import QualityRule
from models.sql import ExecutionResult

log = logging.getLogger(__name__)

SNAPSHOT_DIR = config.ROOT / "results" / "rules"
CATALOG_PATH = config.DATA_DIR / "rule" / "catalog.json"


def _exec_summary(e: ExecutionResult) -> dict:
    return {
        "success": e.success,
        "denominator_count": e.denominator_count,
        "violation_count": e.violation_count,
        "violation_ratio": e.violation_ratio,
        "passed_threshold": e.passed_threshold,
        "elapsed_ms": e.elapsed_ms,
        "error_message": e.error_message,
    }


def save_snapshot(
    rules: list[QualityRule],
    *,
    request: str,
    executions: list[ExecutionResult] | None = None,
) -> Path | None:
    """이번 실행에서 생성된 규칙을 타임스탬프 파일로 저장."""
    if not rules:
        return None

    execs = {e.rule_id: e for e in (executions or [])}
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "request": request,
        "rule_count": len(rules),
        "rules": [
            {
                **r.model_dump(mode="json"),
                "execution": (
                    _exec_summary(execs[r.rule_id]) if r.rule_id in execs else None
                ),
            }
            for r in rules
        ],
    }

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SNAPSHOT_DIR / f"{stamp}_{rules[0].rule_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("규칙 %d건 저장: %s", len(rules), path)
    return path


def append_catalog(rules: list[QualityRule]) -> int:
    """누적 카탈로그에 추가. 같은 rule_id는 갱신한다.

    rule_id는 테이블 기준 일련번호라 세션이 다르면 충돌할 수 있으므로,
    대상 테이블·컬럼·위반조건이 모두 같을 때만 동일 규칙으로 본다.
    """
    if not rules:
        return 0

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if CATALOG_PATH.exists():
        try:
            existing = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("카탈로그 파싱 실패 — 새로 만든다: %s", CATALOG_PATH)

    def key(d: dict) -> tuple:
        return (
            d.get("target_db"),
            tuple(sorted(d.get("target_tables", []))),
            tuple(sorted(d.get("target_columns", []))),
            (d.get("logic_nl") or "").strip(),
        )

    index = {key(d): i for i, d in enumerate(existing)}
    added = 0
    for r in rules:
        d = r.model_dump(mode="json")
        k = key(d)
        if k in index:
            existing[index[k]] = d  # 갱신 (상태·SQL이 바뀌었을 수 있음)
        else:
            existing.append(d)
            added += 1

    CATALOG_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("카탈로그: 신규 %d건, 전체 %d건 → %s", added, len(existing), CATALOG_PATH)
    return added