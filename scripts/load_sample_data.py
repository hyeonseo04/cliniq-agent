"""OMOP 샘플 데이터(CSV) → Oracle 적재.
리포 경로: scripts/load_sample_data.py

EXPLAIN 검증(Phase 4-1)은 빈 스키마로 충분하지만, 실제 실행 검증(Phase 4-2)에는
데이터가 필요하다. 이 스크립트는 CSV 디렉토리를 읽어 해당 이름의 테이블에 적재한다.

사용법:
    # 실데이터 CSV가 있는 경우 (파일명 = 테이블명, 예: person.csv)
    uv run python scripts/load_sample_data.py --csv-dir /path/to/synpuf

    # CSV가 없으면 최소 테스트 데이터 생성 (오류 케이스 포함)
    uv run python scripts/load_sample_data.py --synthetic

주의:
    - 기존 데이터를 지우고 적재한다 (--append 로 유지 가능)
    - CSV 헤더는 CDM 컬럼명과 일치해야 한다 (대소문자 무관)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys

# Athena 어휘 파일에는 128KB 기본 제한을 넘는 긴 필드(약물명 등)가 있다.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
from core.schema_store import get_schema_store  # noqa: E402

# 합성 데이터: 정상 + 의도적 오류를 섞어 규칙이 실제로 위반을 잡는지 확인
SYNTHETIC = {
    "person": {
        "columns": [
            "person_id", "gender_concept_id", "year_of_birth", "birth_datetime",
            "race_concept_id", "ethnicity_concept_id",
        ],
        "rows": [
            # 정상 8건
            *[
                (i, 8507 if i % 2 else 8532, 1950 + i,
                 datetime(1950 + i, 3, 15), 8527, 38003564)
                for i in range(1, 9)
            ],
            # 오류: 출생일 NULL (결측 검사 대상)
            (9, 8507, 1960, None, 8527, 38003564),
            # 오류: 미래 출생일
            (10, 8532, 2090, datetime(2090, 1, 1), 8527, 38003564),
        ],
    },
    "death": {
        "columns": ["person_id", "death_date", "death_datetime"],
        "rows": [
            # 정상 3건 (출생 이후 사망)
            (1, datetime(2020, 5, 1), datetime(2020, 5, 1)),
            (2, datetime(2021, 6, 1), datetime(2021, 6, 1)),
            (3, datetime(2019, 2, 1), datetime(2019, 2, 1)),
            # 오류: 사망일이 출생일보다 이름 (person_id=4는 1954년생)
            (4, datetime(1940, 1, 1), datetime(1940, 1, 1)),
        ],
    },
    "measurement": {
        "columns": [
            "measurement_id", "person_id", "measurement_concept_id",
            "measurement_date", "measurement_type_concept_id", "value_as_number",
        ],
        "rows": [
            # 정상 7건
            *[
                (i, i, 3013682, datetime(2020, 1, 10), 44818702, 10.0 + i)
                for i in range(1, 8)
            ],
            # 오류: 음수 측정값 2건
            (8, 1, 3013682, datetime(2020, 1, 10), 44818702, -5.0),
            (9, 2, 3013682, datetime(2020, 1, 10), 44818702, -1.2),
            # 결측 1건
            (10, 3, 3013682, datetime(2020, 1, 10), 44818702, None),
        ],
    },
    "drug_exposure": {
        "columns": [
            "drug_exposure_id", "person_id", "drug_concept_id",
            "drug_exposure_start_date", "drug_exposure_end_date",
            "drug_type_concept_id",
        ],
        "rows": [
            # 정상 5건
            *[
                (i, i, 1125315, datetime(2020, 1, 1),
                 datetime(2020, 1, 1) + timedelta(days=7), 38000177)
                for i in range(1, 6)
            ],
            # 오류: 종료일이 시작일보다 빠름
            (6, 1, 1125315, datetime(2020, 3, 10), datetime(2020, 3, 1), 38000177),
            # 오류: 사망일(2020-05-01) 이후 약물 노출 (person_id=1)
            (7, 1, 1125315, datetime(2021, 1, 1), datetime(2021, 1, 8), 38000177),
        ],
    },
}


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S")


def _sniff_delimiter(path: Path) -> str:
    """구분자 자동 감지.

    SynPUF는 쉼표(CSV), OHDSI Athena 어휘 파일은 탭(TSV)이다.
    헤더 첫 줄에서 탭이 쉼표보다 많으면 탭으로 본다.
    """
    with path.open(encoding="utf-8-sig", errors="replace") as f:
        head = f.readline()
    return "\t" if head.count("\t") > head.count(",") else ","


_VARCHAR_LEN = re.compile(r"varchar\s*\((\d+)\)", re.IGNORECASE)


def _convert(value: str | None, col_type: str):
    """CSV 문자열 → Oracle 바인드 값.

    - 날짜형은 datetime으로 변환 (파싱 실패 시 NULL)
    - varchar(n)보다 긴 값은 n자로 절단 (Athena concept_name 등)
    """
    if value is None or value == "":
        return None
    t = col_type.lower()
    if "date" in t or "timestamp" in t:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
        return None
    m = _VARCHAR_LEN.search(t)
    if m:
        # Oracle VARCHAR2는 기본이 BYTE 단위다. 파이썬 len()은 문자 수이므로
        # 비ASCII가 섞이면 문자 기준 절단만으로는 ORA-12899가 난다.
        limit = int(m.group(1))
        encoded = value.encode("utf-8")
        if len(encoded) > limit:
            return encoded[:limit].decode("utf-8", errors="ignore")
    return value


def _connect():
    import oracledb

    return oracledb.connect(
        user=config.ORACLE_USER,
        password=config.ORACLE_PASSWORD,
        dsn=config.ORACLE_DSN["integrated"],
    )


def _insert(
    conn, table: str, columns: list[str], rows: list[tuple], append: bool,
    chunk: int = 20000,
) -> int:
    """청크 단위 적재 — CONCEPT처럼 수백만 행인 파일에서 메모리 폭주를 막는다."""
    cur = conn.cursor()
    if not append:
        cur.execute(f"DELETE FROM {table}")
    binds = ", ".join(f":{i + 1}" for i in range(len(columns)))
    stmt = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({binds})"
    for i in range(0, len(rows), chunk):
        cur.executemany(stmt, rows[i : i + chunk])
        conn.commit()
    return len(rows)


def load_synthetic(append: bool) -> None:
    conn = _connect()
    try:
        # FK 의존 순서: person 먼저
        for table in ("person", "death", "measurement", "drug_exposure"):
            spec = SYNTHETIC[table]
            n = _insert(conn, table, spec["columns"], spec["rows"], append)
            print(f"  {table}: {n} rows")
    finally:
        conn.close()


def _resolve_table(stem: str, store) -> str | None:
    """파일명 → CDM 테이블명 정규화.

    SynPUF 등 공개 데이터셋은 파일명에 접두사·버전이 붙는다:
        CDM_PERSON.csv                    → person
        CDM_MEASUREMENT5.2.2.csv          → measurement
        CDM_OBSERVATION_PERIOD5.3.1.csv   → observation_period
        CDM_DRUG_EXPOSURES5.2.2.csv       → drug_exposure (복수형 s 제거)
    """
    name = stem.lower()
    for prefix in ("cdm_", "omop_", "cdm"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    # 끝에 붙은 버전 표기 제거 (5.2.2, 5.3.1, _v5 등)
    name = re.sub(r"[\s_]*v?\d+(\.\d+)*$", "", name).strip("_")
    if store.has_table(name):
        return name
    # 복수형 s 제거 후 재시도
    if name.endswith("s") and store.has_table(name[:-1]):
        return name[:-1]
    return None


def _row_count(conn, table: str) -> int:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def load_csv(
    csv_dir: Path, append: bool, limit: int | None = None,
    skip: list[str] | None = None, skip_loaded: bool = False,
) -> None:
    store = get_schema_store()
    conn = _connect()
    seen: dict[str, Path] = {}
    skip_set = {t.lower() for t in (skip or [])}

    # 같은 테이블에 여러 파일이 매칭되면 파일명이 짧은 쪽(버전 표기 없는 것) 우선
    for path in sorted(csv_dir.glob("*.csv"), key=lambda p: (len(p.name), p.name)):
        table = _resolve_table(path.stem, store)
        if table is None:
            print(f"  (건너뜀) {path.name} — CDM 테이블 아님")
            continue
        if table in skip_set:
            print(f"  (건너뜀) {path.name} — --skip 지정")
            continue
        if table in seen:
            print(f"  (건너뜀) {path.name} — {table}은 {seen[table].name} 사용")
            continue
        seen[table] = path

    # FK 참조 대상 먼저 적재 (제약이 걸려 있어도 실패하지 않도록)
    order = ["location", "care_site", "provider", "person"]
    tables = [t for t in order if t in seen] + [t for t in seen if t not in order]

    try:
        for table in tables:
            path = seen[table]
            if skip_loaded:
                existing = _row_count(conn, table)
                if existing > 0:
                    print(f"  (건너뜀) {table} — 이미 {existing:,}건 적재됨")
                    continue

            known = store.get(table).field_names()
            delim = _sniff_delimiter(path)
            with path.open(encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f, delimiter=delim)
                cols = [c for c in (reader.fieldnames or []) if c.lower() in known]
                if not cols:
                    print(f"  (건너뜀) {path.name} — 일치 컬럼 없음")
                    continue
                types = {f.name: f.type for f in store.get(table).fields}
                rows = []
                for i, r in enumerate(reader):
                    if limit and i >= limit:
                        break
                    rows.append(
                        tuple(_convert(r[c], types.get(c.lower(), "")) for c in cols)
                    )

            if rows:
                n = _insert(conn, table, [c.lower() for c in cols], rows, append)
                print(f"  {table}: {n:,} rows ({len(cols)} cols) ← {path.name}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", type=Path, help="CSV 디렉토리 (파일명 = 테이블명)")
    ap.add_argument("--synthetic", action="store_true", help="합성 테스트 데이터 생성")
    ap.add_argument("--append", action="store_true", help="기존 데이터 유지")
    ap.add_argument("--limit", type=int, default=None,
                    help="테이블당 최대 행 수 (대용량 CSV 일부만 적재)")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="적재하지 않을 테이블 (예: --skip concept_ancestor concept_relationship)")
    ap.add_argument("--skip-loaded", action="store_true",
                    help="이미 데이터가 있는 테이블은 건너뜀 (중단 후 재개 시 사용)")
    args = ap.parse_args()

    if not args.csv_dir and not args.synthetic:
        ap.error("--csv-dir 또는 --synthetic 중 하나가 필요하다")

    print(f"[적재] DSN={config.ORACLE_DSN['integrated']}")
    if args.synthetic:
        print("합성 데이터 (정상 + 의도적 오류):")
        load_synthetic(args.append)
    else:
        print(f"CSV: {args.csv_dir}")
        load_csv(args.csv_dir, args.append, args.limit, args.skip, args.skip_loaded)
    print("완료.")


if __name__ == "__main__":
    main()