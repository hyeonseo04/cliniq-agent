"""DQD 표준 지표 카탈로그 조회. 리포 경로: src/core/dqd_catalog.py

(테이블, 컬럼) → 적용 가능한 표준 체크 목록을 반환한다.
사용자가 컬럼을 직접 지목하므로 벡터 검색이 아닌 직접 조회로 충분하다.

목적: Rule Generation이 임계값을 지어내지 않고 표준 정의를 인스턴스화하게 한다.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "dqd" / "dqd_catalog.json"
)


class DQDCatalog:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.available = Path(path).exists()
        if not self.available:
            self._columns: dict[str, list[dict]] = {}
            self._aspect_map: dict[str, list[str]] = {}
            return
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self._columns = raw.get("columns", {})
        self._aspect_map = raw.get("aspect_map", {})

    def lookup(
        self, table: str, column: str, aspect: str | None = None
    ) -> list[dict]:
        """해당 컬럼에 적용 가능한 체크 목록. aspect 지정 시 관점에 맞는 것만."""
        entries = self._columns.get(f"{table.lower()}.{column.lower()}", [])
        if aspect is None:
            return entries
        allowed = set(self._aspect_map.get(aspect, []))
        return [e for e in entries if e["check_id"] in allowed]

    #: 다른 테이블과의 JOIN이 필요한 체크 → (요구 테이블, 연결 컬럼)
    CROSS_TABLE_CHECKS = {
        "plausibleAfterBirth": "person",
        "plausibleBeforeDeath": "death",
        "plausibleDuringLife": "death",
        "withinVisitDates": "visit_occurrence",
    }

    #: 체크별 JOIN 연결 컬럼 (기본은 person_id)
    JOIN_KEY = {"withinVisitDates": "visit_occurrence_id"}

    def required_tables(
        self, targets: list[tuple[str, list[str]]], aspects: list[str]
    ) -> list[str]:
        """지목 대상에 적용 가능한 체크들이 추가로 요구하는 테이블 목록.

        예: procedure_occurrence.procedure_date + temporal 관점
            → plausibleBeforeDeath가 death를, plausibleAfterBirth가 person을 요구
        입력 처리(InputAgent)가 이 테이블들을 자동 추가해 JOIN 경로를 확정한다.
        """
        if not self.available:
            return []

        selected = {t.lower() for t, _ in targets}
        need: set[str] = set()
        for table, columns in targets:
            for col in columns:
                entries: list[dict] = []
                if aspects:
                    for a in aspects:
                        entries.extend(self.lookup(table, col, a))
                else:
                    entries = self.lookup(table, col)
                for e in entries:
                    t = self.CROSS_TABLE_CHECKS.get(e["check_id"])
                    if t and t not in selected:
                        need.add(t)
        return sorted(need)

    #: 같은 check_id를 프롬프트에 나열할 최대 개수.
    #: plausibleGender/UnitConceptIds는 concept마다 별도 정의가 있어 200건이 넘는다.
    #: 전부 넣으면 컨텍스트를 초과하므로 대표 사례만 보이고 나머지는 요약한다.
    MAX_PER_CHECK = 8

    def render_for_prompt(
        self, targets: list[tuple[str, list[str]]], aspects: list[str]
    ) -> str:
        """Rule Generation 프롬프트에 넣을 텍스트.

        targets: [(table, [column, ...]), ...]
        aspects: ["temporal", ...] — 빈 리스트면 전체 체크
        """
        if not self.available:
            return ""

        # 사용자가 지목한 테이블 — 이 밖의 테이블을 요구하는 체크는 제외한다
        # (지목하지 않은 테이블 참조 규칙이 생성되어 구조검사에서 탈락하는 것을 방지)
        selected = {t.lower() for t, _ in targets}

        lines: list[str] = []
        for table, columns in targets:
            for col in columns:
                found: list[dict] = []
                if aspects:
                    for a in aspects:
                        for e in self.lookup(table, col, a):
                            if e not in found:
                                found.append(e)
                else:
                    found = self.lookup(table, col)
                if not found:
                    continue

                lines.append(f"\n### {table}.{col}")

                # check_id별로 묶어 상한을 적용한다 (컨텍스트 초과 방지)
                grouped: dict[str, list[dict]] = {}
                for e in found:
                    grouped.setdefault(e["check_id"], []).append(e)

                trimmed: list[dict] = []
                overflow: dict[str, int] = {}
                shown_rule: set[str] = set()   # 같은 체크의 필수 조건은 한 번만
                for cid, group in grouped.items():
                    trimmed.extend(group[: self.MAX_PER_CHECK])
                    if len(group) > self.MAX_PER_CHECK:
                        overflow[cid] = len(group) - self.MAX_PER_CHECK

                for e in trimmed:
                    parts = [f"- `{e['check_id']}` ({e['dimension']})"]
                    need = self.CROSS_TABLE_CHECKS.get(e["check_id"])
                    if need and need not in selected:
                        key = self.JOIN_KEY.get(e["check_id"], "person_id")
                        parts.append(
                            f"  ※ 이 검사는 `{need}` 테이블 JOIN이 필요하다. "
                            f"{key}로 연결해 사용하라."
                        )
                    if "threshold_value" in e:
                        parts.append(f"  임계값: {e['threshold_value']}")
                    if "reference" in e:
                        parts.append(f"  참조: {e['reference']}")
                    if "domain" in e:
                        parts.append(f"  도메인: {e['domain']}")
                    if e.get("concept_id"):
                        cn = e.get("concept_name", "")
                        parts.append(
                            f"  대상 concept: {e['concept_id']}"
                            + (f" ({cn})" if cn else "")
                        )
                    if e.get("gender"):
                        parts.append(f"  타당한 성별: {e['gender']}")
                    if e.get("unit_concept_ids"):
                        parts.append(f"  타당한 단위 concept: {e['unit_concept_ids']}")
                    cid = e["check_id"]
                    if e.get("rule") and cid not in shown_rule:
                        parts.append(f"  **필수 조건**: {e['rule']}")
                        shown_rule.add(cid)
                    elif not e.get("rule") and e.get("description"):
                        parts.append(f"  정의: {e['description']}")
                    lines.append("\n".join(parts))

                for cid, n in overflow.items():
                    lines.append(
                        f"\n> `{cid}`는 위 외에도 concept {n}건에 대한 기준이 더 있다. "
                        "위 사례와 동일한 형태로 여러 concept을 함께 검사하는 규칙을 "
                        "하나로 묶어 작성해도 된다."
                    )

        if not lines:
            return ""
        return (
            "## 이 컬럼에 적용 가능한 표준 지표 (OHDSI DQD)\n"
            "**임계값이 제시된 경우 반드시 그 값을 사용하라. 다른 숫자를 만들지 마라.**\n"
            "해당하는 지표가 있으면 규칙 이름 끝에 `[지표: check_id]`를 붙인다.\n"
            + "\n".join(lines)
        )


@lru_cache(maxsize=1)
def get_dqd_catalog() -> DQDCatalog:
    return DQDCatalog()