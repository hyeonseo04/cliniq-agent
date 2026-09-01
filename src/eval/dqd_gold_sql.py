"""DQD 공식 체크 SQL의 Oracle 이식 — 평가용 골드 SQL 생성기.
리포 경로: src/eval/dqd_gold_sql.py

출처: OHDSI/DataQualityDashboard  inst/sql/sql_server/field_*.sql
      각 파일의 /*violatedRowsBegin*/ ~ /*violatedRowsEnd*/ 구간을 Oracle로 이식.

용도:
    생성된 규칙의 위반 행 집합과 공식 정의의 위반 행 집합을 비교(행 집합 실행 등가).
    건수가 아니라 PK 집합을 반환하도록 SELECT 절을 바꾼 것이 원본과의 유일한 차이다.

SQL Server → Oracle 변환 규칙:
    DATEADD(day, n, d)        → d + n
    CAST(x AS DATE)           → CAST(x AS DATE)          (동일)
    CONCAT(a, b)              → a || b
    RIGHT('0'+CAST(n AS VARCHAR),2) → LPAD(TO_CHAR(n), 2, '0')
    COUNT_BIG(*)              → COUNT(*)                 (건수 비교 시)

주의 — 공식 정의에 이미 허용 오차가 포함된 체크가 있다:
    withinVisitDates      : 방문 시작일 -7일 ~ 종료일 +7일 (DQD 원본에 명시)
    plausibleBeforeDeath  : 사망일 +60일
    plausibleDuringLife   : 사망일 +60일
    이는 임의 값이 아니라 표준 정의이므로 이식 시 그대로 유지한다.
"""
from __future__ import annotations

from dataclasses import dataclass

#: 테이블별 기본키 (위반 행 집합 비교용 식별자)
PK = {
    "person": "person_id",
    "observation_period": "observation_period_id",
    "visit_occurrence": "visit_occurrence_id",
    "visit_detail": "visit_detail_id",
    "condition_occurrence": "condition_occurrence_id",
    "drug_exposure": "drug_exposure_id",
    "procedure_occurrence": "procedure_occurrence_id",
    "device_exposure": "device_exposure_id",
    "measurement": "measurement_id",
    "observation": "observation_id",
    "death": "person_id",
    "note": "note_id",
    "specimen": "specimen_id",
    "payer_plan_period": "payer_plan_period_id",
    "cost": "cost_id",
    "drug_era": "drug_era_id",
    "dose_era": "dose_era_id",
    "condition_era": "condition_era_id",
    "location": "location_id",
    "care_site": "care_site_id",
    "provider": "provider_id",
}

DATE_TYPES = {"date", "datetime", "timestamp"}


@dataclass
class GoldSQL:
    """골드 SQL 한 건. rows는 위반 행 PK 집합, count는 건수."""

    check_id: str
    table: str
    column: str
    rows_sql: str          # 위반 행 PK 목록
    count_sql: str         # 위반 건수 + 분모
    note: str = ""


def _pk(table: str) -> str:
    if table not in PK:
        raise KeyError(f"PK 미정의 테이블: {table}")
    return PK[table]


def _cast_date(expr: str, col_type: str | None) -> str:
    """DQD는 날짜형 컬럼에만 CAST를 씌운다."""
    if col_type and col_type.lower() in DATE_TYPES:
        return f"CAST({expr} AS DATE)"
    return expr


# ---------------------------------------------------------------- 체크별 WHERE 절

def _where(
    check_id: str,
    table: str,
    column: str,
    *,
    col_type: str | None = None,
    threshold: str | None = None,
    pair_field: str | None = None,
    domain: str | None = None,
    source_value_field: str | None = None,
    ref_table: str | None = None,
    ref_field: str | None = None,
    fk_class: str | None = None,
    concept_id: str | int | None = None,
    gender: str | None = None,
    unit_concept_ids: str | None = None,
) -> tuple[str, str]:
    """(FROM/JOIN 절, WHERE 절)을 반환한다."""
    t = "cdmTable"
    col = f"{t}.{column}"

    if check_id in ("isRequired", "measureValueCompleteness"):
        # field_is_not_nullable.sql / field_measure_value_completeness.sql
        return "", f"{col} IS NULL"

    if check_id in ("standardConceptRecordCompleteness",
                    "sourceConceptRecordCompleteness"):
        # field_concept_record_completeness.sql
        # 0(매핑 실패) 또는 (NULL이면서 대응 source_value가 존재)
        cond = f"{col} = 0"
        if source_value_field:
            cond += (
                f" OR ({col} IS NULL AND {t}.{source_value_field} IS NOT NULL)"
            )
        return "", cond

    if check_id == "sourceValueCompleteness":
        # 원천값이 표준 concept으로 매핑되지 않은 비율 — DQD는 source_value 기준 집계
        return "", f"{col} IS NULL"

    if check_id == "plausibleValueLow":
        # field_plausible_value_low.sql
        return "", f"{_cast_date(col, col_type)} < {_cast_threshold(threshold, col_type)}"

    if check_id == "plausibleValueHigh":
        return "", f"{_cast_date(col, col_type)} > {_cast_threshold(threshold, col_type)}"

    if check_id == "isForeignKey":
        # 참조 대상에 존재하지 않는 값
        j = (
            f"\n    LEFT JOIN {ref_table} fk"
            f"\n      ON {col} = fk.{ref_field}"
        )
        return j, f"{col} IS NOT NULL AND fk.{ref_field} IS NULL"

    if check_id == "fkDomain":
        # field_fk_domain.sql
        j = f"\n    LEFT JOIN concept co\n      ON {col} = co.concept_id"
        return j, f"co.concept_id != 0 AND co.domain_id NOT IN ('{domain}')"

    if check_id == "isStandardValidConcept":
        # field_is_standard_valid_concept.sql
        j = f"\n    JOIN concept co\n      ON {col} = co.concept_id"
        return j, (
            "co.concept_id != 0 AND ("
            "co.standard_concept != 'S' "
            "OR co.invalid_reason IS NOT NULL "
            "OR co.standard_concept IS NULL)"
        )

    if check_id == "plausibleStartBeforeEnd":
        # field_plausible_start_before_end.sql
        return "", (
            f"{col} IS NOT NULL AND {t}.{pair_field} IS NOT NULL "
            f"AND CAST({col} AS DATE) > CAST({t}.{pair_field} AS DATE)"
        )

    if check_id == "plausibleAfterBirth":
        # field_plausible_after_birth.sql
        # 출생일시가 없으면 year/month/day 조합으로 대체 (Oracle 문자열 연결)
        j = "\n    JOIN person p ON cdmTable.person_id = p.person_id"
        birth = (
            "COALESCE(CAST(p.birth_datetime AS DATE), "
            "TO_DATE(TO_CHAR(p.year_of_birth) "
            "|| LPAD(TO_CHAR(COALESCE(p.month_of_birth, 1)), 2, '0') "
            "|| LPAD(TO_CHAR(COALESCE(p.day_of_birth, 1)), 2, '0'), 'YYYYMMDD'))"
        )
        return j, f"{col} IS NOT NULL AND CAST({col} AS DATE) < {birth}"

    if check_id in ("plausibleBeforeDeath", "plausibleDuringLife"):
        # field_plausible_before_death.sql / field_plausible_during_life.sql
        # 원본: DATEADD(day, 60, death_date) — 사망일 +60일 허용 오차 (표준 정의)
        j = "\n    JOIN death de ON cdmTable.person_id = de.person_id"
        return j, (
            f"{col} IS NOT NULL "
            f"AND CAST({col} AS DATE) > CAST(de.death_date AS DATE) + 60"
        )

    if check_id == "plausibleTemporalAfter":
        # field_plausible_temporal_after.sql
        if ref_table and ref_table.lower() != table.lower():
            j = (
                f"\n    JOIN {ref_table} plausibleTable"
                f"\n      ON cdmTable.person_id = plausibleTable.person_id"
            )
            if ref_table.lower() == "person":
                prev = (
                    f"COALESCE(CAST(plausibleTable.{pair_field} AS DATE), "
                    "TO_DATE(TO_CHAR(plausibleTable.year_of_birth) || '0601', "
                    "'YYYYMMDD'))"
                )
            else:
                prev = f"CAST(plausibleTable.{pair_field} AS DATE)"
        else:
            j = ""
            prev = f"CAST({t}.{pair_field} AS DATE)"
        return j, f"{prev} > CAST({col} AS DATE)"

    if check_id == "isPrimaryKey":
        # field_is_primary_key.sql — 중첩 집계(서브쿼리)로 중복 값 검출
        return "", (
            f"{col} IN (SELECT {column} FROM {table} "
            f"GROUP BY {column} HAVING COUNT(*) > 1)"
        )

    if check_id == "fkClass":
        # field_fk_class.sql — concept_class 일치 여부
        j = f"\n    LEFT JOIN concept co\n      ON {col} = co.concept_id"
        return j, (
            f"co.concept_id != 0 AND co.concept_class_id != '{fk_class}'"
        )

    if check_id == "plausibleGender":
        # concept_plausible_gender.sql — 특정 concept이 타당하지 않은 성별에 기록
        j = "\n    JOIN person p ON cdmTable.person_id = p.person_id"
        gid = 8507 if (gender or "").lower() == "male" else 8532
        return j, f"{col} = {concept_id} AND p.gender_concept_id <> {gid}"

    if check_id == "plausibleGenderUseDescendants":
        # concept_plausible_gender_use_descendants.sql — 하위 개념까지 포함
        j = (
            "\n    JOIN person p ON cdmTable.person_id = p.person_id"
            f"\n    JOIN concept_ancestor ca ON ca.descendant_concept_id = {col}"
        )
        gid = 8507 if (gender or "").lower() == "male" else 8532
        return j, (
            f"ca.ancestor_concept_id IN ({concept_id}) "
            f"AND p.gender_concept_id <> {gid}"
        )

    if check_id == "plausibleUnitConceptIds":
        # concept_plausible_unit_concept_ids.sql — 측정 단위 타당성
        units = (unit_concept_ids or "").strip()
        if units == "-1":
            cond = "cdmTable.unit_concept_id != 0"
        else:
            cond = f"cdmTable.unit_concept_id NOT IN ({units}, 0)"
        return "", (
            f"{col} = {concept_id} AND cdmTable.unit_concept_id IS NOT NULL "
            f"AND ({cond})"
        )

    if check_id == "withinVisitDates":
        # field_within_visit_dates.sql
        # 원본: visit_start_date -7일 ~ visit_end_date +7일 (표준 정의의 허용 오차)
        j = (
            "\n    JOIN visit_occurrence vo"
            "\n      ON cdmTable.visit_occurrence_id = vo.visit_occurrence_id"
        )
        return j, (
            f"{col} < vo.visit_start_date - 7 OR {col} > vo.visit_end_date + 7"
        )

    raise ValueError(f"미지원 체크: {check_id}")


def _cast_threshold(threshold: str | None, col_type: str | None) -> str:
    if threshold is None:
        raise ValueError("임계값이 필요한 체크인데 threshold가 없다")
    if col_type and col_type.lower() in DATE_TYPES:
        return f"CAST({threshold} AS DATE)"
    return str(threshold)


# ---------------------------------------------------------------- 공개 API

def build(
    check_id: str,
    table: str,
    column: str,
    **kwargs,
) -> GoldSQL:
    """골드 SQL 생성. kwargs는 체크별 파라미터(threshold, pair_field, domain 등)."""
    table = table.lower()
    column = column.lower()
    join, where = _where(check_id, table, column, **kwargs)
    pk = _pk(table)

    rows_sql = (
        f"SELECT cdmTable.{pk} AS violating_pk\n"
        f"FROM {table} cdmTable{join}\n"
        f"WHERE {where}"
    )
    count_sql = (
        f"SELECT COUNT(*) AS num_violated_rows\n"
        f"FROM {table} cdmTable{join}\n"
        f"WHERE {where}"
    )

    note = ""
    if check_id == "withinVisitDates":
        note = "DQD 원본에 ±7일 허용 오차가 정의되어 있음"
    elif check_id in ("plausibleBeforeDeath", "plausibleDuringLife"):
        note = "DQD 원본에 사망일 +60일 허용 오차가 정의되어 있음"

    return GoldSQL(check_id, table, column, rows_sql, count_sql, note)


SUPPORTED = [
    "isPrimaryKey", "fkClass",
    "plausibleGender", "plausibleGenderUseDescendants", "plausibleUnitConceptIds",
    "isRequired", "measureValueCompleteness",
    "standardConceptRecordCompleteness", "sourceConceptRecordCompleteness",
    "sourceValueCompleteness",
    "plausibleValueLow", "plausibleValueHigh",
    "isForeignKey", "fkDomain", "isStandardValidConcept",
    "plausibleStartBeforeEnd", "plausibleAfterBirth",
    "plausibleBeforeDeath", "plausibleDuringLife",
    "plausibleTemporalAfter", "withinVisitDates",
]