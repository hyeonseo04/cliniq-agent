"""규칙 카탈로그 조회 인터페이스. 리포 경로: src/core/catalog.py

카탈로그 자체는 타 파트 담당 — 이 모듈은 '조회 전용 어댑터'다.
초기 구현: data/rule/catalog.json (QualityRule JSON 배열) 파일 기반.
타 파트 저장소가 확정되면 이 클래스 내부만 교체하면 된다 (인터페이스 유지).
"""
from __future__ import annotations

import json
from pathlib import Path

import config
from models.common import DQDimension, TargetDB
from models.rule import QualityRule

DEFAULT_CATALOG_PATH = config.DATA_DIR / "rule" / "catalog.json"


class RuleCatalog:
    def __init__(self, path: str | Path = DEFAULT_CATALOG_PATH):
        self.path = Path(path)
        self._rules: list[QualityRule] = []
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._rules = [QualityRule(**r) for r in raw]

    def search(
        self,
        target_db: TargetDB | None = None,
        dq_dimension: DQDimension | None = None,
        table: str | None = None,
    ) -> list[QualityRule]:
        out = self._rules
        if target_db:
            out = [r for r in out if r.target_db == target_db]
        if dq_dimension:
            out = [r for r in out if r.dq_dimension == dq_dimension]
        if table:
            out = [r for r in out if table.lower() in r.target_tables]
        return out

    def all(self) -> list[QualityRule]:
        return list(self._rules)