"""DQD CSV → 컬럼 단위 지표 카탈로그 생성.
리포 경로: scripts/build_dqd_catalog.py

DQD는 체크 '유형'(Check_Descriptions, 27종)과
컬럼별 '적용 정의'(Field_Level, 306행)를 나눠 관리한다.
이 스크립트는 둘을 결합해 (테이블, 컬럼) → 적용 가능한 체크 목록으로 뒤집는다.

목적:
    Rule Generation이 "이 컬럼에는 어떤 표준 체크가 있고 임계값은 무엇인가"를
    조회할 수 있게 하여, LLM이 임계값을 임의로 지어내지 않도록 한다.
    (예: person.year_of_birth 하한 1850 — 모델이 만들어낸 1900이 아니라)

사용법:
    uv run python scripts/build_dqd_catalog.py
    → data/dqd/dqd_catalog.json 생성

입력:
    data/dqd/csv/OMOP_CDMv5.3_Check_Descriptions.csv
    data/dqd/csv/OMOP_CDMv5.3_Field_Level.csv
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CSV_DIR = ROOT / "data" / "dqd" / "csv"
OUT = ROOT / "data" / "dqd" / "dqd_catalog.json"

# 우리 SQL 계약(단일 SELECT, 레코드 단위 집계)으로 표현 가능한 체크만 채택.
# 제외: cdmTable/cdmField(분모 없음), cdmDatatype(DB가 타입 강제)
#: 지원 체크 15종. 품질 차원은 하드코딩하지 않고 DQD의 kahnCategory를 그대로 사용한다.
#: (골드가 DQD이므로 집계 축도 DQD 분류를 따라야 해석이 일관된다)
SUPPORTED: set[str] = {
    "isRequired",
    "isPrimaryKey",   # 중첩 집계(서브쿼리) — SQL 계약 확장으로 지원
    "fkClass",        # concept_class 일치
    "measureValueCompleteness",
    "standardConceptRecordCompleteness",
    "sourceConceptRecordCompleteness",
    "sourceValueCompleteness",
    "isForeignKey",
    "fkDomain",
    "isStandardValidConcept",
    "plausibleValueLow",
    "plausibleValueHigh",
    "plausibleStartBeforeEnd",
    "plausibleAfterBirth",
    "plausibleBeforeDeath",
    "plausibleDuringLife",
    "plausibleTemporalAfter",
    "withinVisitDates",
}

#: kahnSubcategory가 비어 있는 체크의 보정 (DQD CSV 누락분)
SUBCATEGORY_FIX = {"withinVisitDates": "Temporal"}

# 평가 관점(사용자 어휘) → 해당 체크.
# Kahn 범주와 어긋나지 않도록 4분류로 나눈다:
#   결측     = Completeness
#   참조·표준 = Conformance
#   값 범위   = Plausibility (Atemporal)
#   시간 타당성 = Plausibility (Temporal)
ASPECT_MAP: dict[str, set[str]] = {
    "missing": {
        "measureValueCompleteness",
        "standardConceptRecordCompleteness",
        "sourceConceptRecordCompleteness",
        "sourceValueCompleteness",
    },
    "referential": {"isRequired", "isForeignKey", "isPrimaryKey"},
    "standard": {"isStandardValidConcept", "fkDomain", "fkClass"},
    "clinical": {
        "plausibleGender", "plausibleGenderUseDescendants",
        "plausibleUnitConceptIds",
    },
    "value_range": {"plausibleValueLow", "plausibleValueHigh"},
    "temporal": {
        "plausibleStartBeforeEnd", "plausibleAfterBirth", "plausibleBeforeDeath",
        "plausibleDuringLife", "plausibleTemporalAfter", "withinVisitDates",
    },
}

# SQL Server 표현 → Oracle 표현
_ORACLE_EXPR = {
    "YEAR(GETDATE())+1": "EXTRACT(YEAR FROM SYSDATE) + 1",
    "DATEADD(dd,1,GETDATE())": "SYSDATE + 1",
    "GETDATE()": "SYSDATE",
}
_YYYYMMDD = re.compile(r"^'(\d{4})(\d{2})(\d{2})'$")


def to_oracle(value: str) -> str:
    """DQD 임계값 표현을 Oracle SQL로 변환."""
    v = value.strip()
    if not v:
        return v
    if v in _ORACLE_EXPR:
        return _ORACLE_EXPR[v]
    m = _YYYYMMDD.match(v)
    if m:  # '18500101' → TO_DATE('1850-01-01','YYYY-MM-DD')
        return f"TO_DATE('{m.group(1)}-{m.group(2)}-{m.group(3)}', 'YYYY-MM-DD')"
    for src, dst in _ORACLE_EXPR.items():
        v = v.replace(src, dst)
    return v


#: CSV에서 빈 값이 "None"/"NA" 문자열로 들어오는 경우가 있다
_EMPTY = {"", "none", "na", "n/a", "null", "-"}


def _clean(v: str | None) -> str:
    """빈 값 표현을 빈 문자열로 정규화."""
    t = (v or "").strip()
    return "" if t.lower() in _EMPTY else t


def _yes(v: str | None) -> bool:
    return (v or "").strip().lower() in {"yes", "true", "1"}


def build() -> dict:
    checks = {
        r["checkName"]: r
        for r in csv.DictReader(
            (CSV_DIR / "OMOP_CDMv5.3_Check_Descriptions.csv").open(encoding="utf-8-sig")
        )
    }
    fields = list(
        csv.DictReader(
            (CSV_DIR / "OMOP_CDMv5.3_Field_Level.csv").open(encoding="utf-8-sig")
        )
    )

    catalog: dict[str, list[dict]] = {}
    for row in fields:
        table = (row.get("cdmTableName") or "").strip().lower()
        column = (row.get("cdmFieldName") or "").strip().lower()
        if not table or not column:
            continue
        key = f"{table}.{column}"

        for check_id in SUPPORTED:
            meta = checks.get(check_id, {})
            category = (meta.get("kahnCategory") or "").strip()
            subcategory = (
                SUBCATEGORY_FIX.get(check_id)
                or (meta.get("kahnSubcategory") or "").strip()
            )
            raw = _clean(row.get(check_id))
            # plausibleValueLow/High는 값 자체가 임계값, 나머지는 Yes/No 플래그
            is_threshold_check = check_id in ("plausibleValueLow", "plausibleValueHigh")
            if is_threshold_check:
                if not raw:
                    continue
            elif not _yes(raw):
                continue

            entry: dict = {
                "check_id": check_id,
                "dimension": category,          # Kahn 범주 (Conformance/Completeness/Plausibility)
                "subcategory": subcategory,     # Relational/Value/Atemporal/Temporal
                "col_type": (row.get("cdmDatatype") or "").strip().lower(),
                "description": " ".join(
                    (checks.get(check_id, {}).get("checkDescription") or "").split()
                )[:220],
            }
            if is_threshold_check:
                entry["threshold_value"] = to_oracle(raw)
                entry["threshold_raw"] = raw

            # 위반율 허용 임계 (DQD의 *Threshold 컬럼, % 단위)
            pct = _clean(row.get(f"{check_id}Threshold"))
            if pct:
                entry["allowed_violation_pct"] = pct

            # 골드 SQL 생성에 필요한 파라미터를 함께 담는다
            # (평가 하네스가 DQD 공식 정의를 재현하려면 비교 대상 컬럼·테이블이 필요)
            if check_id == "plausibleStartBeforeEnd":
                pf = _clean(row.get("plausibleStartBeforeEndFieldName")).lower()
                if pf:
                    entry["pair_field"] = pf
            if check_id == "plausibleTemporalAfter":
                pf = _clean(row.get("plausibleTemporalAfterFieldName")).lower()
                pt = _clean(row.get("plausibleTemporalAfterTableName")).lower()
                if pf:
                    entry["pair_field"] = pf
                if pt:
                    entry["ref_table"] = pt
            if check_id in ("standardConceptRecordCompleteness",
                            "sourceConceptRecordCompleteness"):
                sf = _clean(row.get("standardConceptFieldName")).lower()
                if sf:
                    entry["source_value_field"] = sf

            # FK 체크는 참조 대상 정보 포함
            if check_id == "isForeignKey":
                ref_t = _clean(row.get("fkTableName")).lower()
                ref_f = _clean(row.get("fkFieldName")).lower()
                if ref_t:
                    entry["reference"] = f"{ref_t}.{ref_f}"
                    entry["ref_table"] = ref_t
                    entry["ref_field"] = ref_f
            if check_id == "fkDomain":
                dom = _clean(row.get("fkDomain"))
                if dom:
                    entry["domain"] = dom
            if check_id == "fkClass":
                cls = _clean(row.get("fkClass"))
                if cls:
                    entry["fk_class"] = cls

            catalog.setdefault(key, []).append(entry)

    # ---------- CONCEPT 레벨 (임상 도메인 지식 기반) ----------
    # Concept_Level.csv는 "특정 concept이 어떤 성별·단위에 타당한가"를 정의한다.
    # 스키마만으로는 절대 도출할 수 없는 지식이므로 별도 병합한다.
    concept_path = CSV_DIR / "OMOP_CDMv5.3_Concept_Level.csv"
    if concept_path.exists():
        for row in csv.DictReader(concept_path.open(encoding="utf-8-sig")):
            table = _clean(row.get("cdmTableName")).lower()
            column = _clean(row.get("cdmFieldName")).lower()
            cid = _clean(row.get("conceptId"))
            if not (table and column and cid):
                continue
            key = f"{table}.{column}"

            for check_id in ("plausibleGender", "plausibleGenderUseDescendants",
                             "plausibleUnitConceptIds"):
                val = _clean(row.get(check_id))
                if not val:
                    continue
                entry = {
                    "check_id": check_id,
                    "dimension": "Plausibility",
                    "subcategory": "Clinical",   # 임상 도메인 지식 기반
                    "col_type": "integer",
                    "concept_id": cid,
                    "concept_name": _clean(row.get("conceptName"))[:60],
                    "description": (
                        "특정 concept이 타당하지 않은 성별의 환자에게 기록되었는지 검사"
                        if "Gender" in check_id
                        else "측정값의 단위 concept이 해당 검사 항목에 타당한지 검사"
                    ),
                }
                if "Gender" in check_id:
                    entry["gender"] = val
                else:
                    entry["unit_concept_ids"] = val
                catalog.setdefault(key, []).append(entry)

    return {
        "source": "OHDSI DataQualityDashboard OMOP_CDMv5.3",
        "aspect_map": {k: sorted(v) for k, v in ASPECT_MAP.items()},
        "columns": catalog,
    }


def main() -> None:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    cols = data["columns"]
    total = sum(len(v) for v in cols.values())
    print(f"→ {OUT}")
    print(f"컬럼 {len(cols)}개 / 체크 조합 {total}건")

    from collections import Counter

    cnt = Counter(e["check_id"] for v in cols.values() for e in v)
    for k, n in cnt.most_common():
        print(f"  {k:36s} {n:4d}")


if __name__ == "__main__":
    main()