"""DQD 카탈로그 → 평가용 골든셋 생성 (층화 추출).
리포 경로: scripts/build_golden_set.py

배경:
    LLM-DQR 논문은 전문가 3명이 만든 규칙(PIC 238건, MIMIC-IV 224건)을 정답셋으로 삼았다.
    우리는 OMOP CDM v5.3을 쓰므로 OHDSI 커뮤니티가 정의한 DQD Field_Level을
    '전문가 합의 결과'로 사용한다 — 별도 라벨링이 불필요하다.

층화가 필요한 이유:
    카탈로그 분포가 편중되어 있다(measureValueCompleteness 혼자 138건, withinVisitDates 5건).
    무작위 추출하면 완전성 검사가 절반을 차지해 차원별·복잡도별 지표가 무의미해진다.

사용법:
    uv run python scripts/build_golden_set.py --size 200
    uv run python scripts/build_golden_set.py --size 200 --check-data   # 데이터 유무 확인
    → data/golden_set/golden_set.json 생성

출력 형식:
    각 항목이 하나의 평가 질의로 변환된다:
      "{table}의 {column}을(를) {aspect} 관점으로 평가하는 지표 만들어줘"
    기대 결과는 expected_checks(체크 ID 목록)로, 생성 규칙의 [지표: X] 태그와 대조한다.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

CATALOG = config.DATA_DIR / "dqd" / "dqd_catalog.json"
OUT = config.DATA_DIR / "golden_set" / "golden_set.json"

# SynPUF에 데이터가 존재하는 테이블 (실행 검증까지 평가 가능한 대상)
TARGET_TABLES = [
    "person", "visit_occurrence", "condition_occurrence", "drug_exposure",
    "measurement", "observation_period", "death", "procedure_occurrence",
    "observation",
]

# 체크 유형 → 복잡도 계층 (논문의 HCR 지표용)
#   ST_SF: 단일 테이블·단일 필드 / ST_MF: 단일 테이블·다중 필드 / MT_MF: 다중 테이블
COMPLEXITY = {
    "measureValueCompleteness": "ST_SF",
    "isRequired": "ST_SF",
    "standardConceptRecordCompleteness": "ST_SF",
    "sourceConceptRecordCompleteness": "ST_SF",
    "sourceValueCompleteness": "ST_SF",
    "plausibleValueLow": "ST_SF",
    "plausibleValueHigh": "ST_SF",
    "isStandardValidConcept": "ST_SF",
    "plausibleStartBeforeEnd": "ST_MF",
    "isPrimaryKey": "ST_SF",
    "fkClass": "MT_MF",
    "plausibleGender": "MT_MF",              # person JOIN 필요
    "plausibleGenderUseDescendants": "MT_MF",
    "plausibleUnitConceptIds": "ST_MF",      # 같은 테이블의 unit_concept_id 비교
    # plausibleTemporalAfter는 참조 대상에 따라 갈린다 (_complexity_of 참조)
    "isForeignKey": "MT_MF",
    "fkDomain": "MT_MF",
    "plausibleAfterBirth": "MT_MF",
    "plausibleBeforeDeath": "MT_MF",
    "plausibleDuringLife": "MT_MF",
    "plausibleTemporalAfter": "MT_MF",
    "withinVisitDates": "MT_MF",
}

# Kahn 범주(+하위) → 사용자 관점 어휘 (질의문 생성용)
# 골드가 DQD이므로 집계 축도 Kahn 분류를 그대로 따른다.
ASPECT = {
    ("Completeness", ""): "결측",
    ("Plausibility", "Clinical"): "임상 타당성",
    ("Conformance", "Relational"): "필수·참조 무결성",
    ("Conformance", "Value"): "표준 개념",
    ("Plausibility", "Atemporal"): "값 범위",
    ("Plausibility", "Temporal"): "시간 타당성",
}

#: 관점 한국어 → EvalAspect 값 (run_eval이 슬롯을 직접 구성할 때 사용)
ASPECT_CODE = {
    "결측": "missing",
    "임상 타당성": "clinical",
    "필수·참조 무결성": "referential",
    "표준 개념": "standard",
    "값 범위": "value_range",
    "시간 타당성": "temporal",
}

# 차원별 목표 비율 (Kahn 3범주, 합 1.0)
DIM_RATIO = {"Completeness": 0.33, "Conformance": 0.33, "Plausibility": 0.34}

#: 한 체크 유형이 차지할 수 있는 최대 비율 — 편중 방지
MAX_CHECK_RATIO = 0.15


def load_catalog() -> dict[str, list[dict]]:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))["columns"]
    return {k: v for k, v in raw.items() if k.split(".")[0] in TARGET_TABLES}


def check_data_availability(keys: list[str]) -> dict[str, bool]:
    """각 컬럼에 실제 값이 있는지 DB에서 확인. 전부 NULL이면 실행 검증이 불가능하다."""
    import oracledb

    conn = oracledb.connect(
        user=config.ORACLE_USER,
        password=config.ORACLE_PASSWORD,
        dsn=config.ORACLE_DSN["integrated"],
    )
    result: dict[str, bool] = {}
    try:
        cur = conn.cursor()
        for key in keys:
            table, column = key.split(".", 1)
            try:
                cur.execute(f"SELECT COUNT({column}) FROM {table}")
                row = cur.fetchone()
                result[key] = bool(row and row[0] > 0)
            except Exception:  # noqa: BLE001 — 테이블 미적재 등
                result[key] = False
    finally:
        conn.close()
    return result


#: 골드 SQL 생성에 필수 파라미터가 있는 체크 (없으면 평가 불가 → 골든셋에서 제외)
REQUIRED_PARAM = {
    "fkClass": "fk_class",
    "plausibleGender": "concept_id",
    "plausibleGenderUseDescendants": "concept_id",
    "plausibleUnitConceptIds": "concept_id",
    "plausibleValueLow": "threshold_value",
    "plausibleValueHigh": "threshold_value",
    "plausibleStartBeforeEnd": "pair_field",
    "plausibleTemporalAfter": "pair_field",
    "isForeignKey": "ref_table",
    "fkDomain": "domain",
}


def _complexity_of(check_id: str, table: str, entry: dict) -> str:
    """복잡도 판정.

    plausibleTemporalAfter는 비교 대상 테이블에 따라 실제 SQL 구조가 달라진다.
      - ref_table이 자기 자신이거나 비어 있음 → 같은 테이블 두 컬럼 비교 → ST_MF
      - ref_table이 다른 테이블 → JOIN 필요 → MT_MF
    (정적 매핑만 쓰면 14건이 MT_MF로 잘못 분류된다)
    """
    if check_id == "plausibleTemporalAfter":
        ref = (entry.get("ref_table") or "").lower()
        return "ST_MF" if ref in ("", table.lower()) else "MT_MF"
    return COMPLEXITY.get(check_id, "ST_SF")


def _evaluable(entry: dict) -> bool:
    """골드 SQL을 만들 수 있는 조합인지."""
    need = REQUIRED_PARAM.get(entry["check_id"])
    return need is None or bool(entry.get(need))


def stratified_sample(
    catalog: dict[str, list[dict]], size: int, seed: int, available: dict[str, bool] | None
) -> list[dict]:
    """차원 비율을 맞추고 체크 유형 편중을 억제하며 추출."""
    rng = random.Random(seed)

    # (컬럼, 체크) 조합을 차원별로 모은다
    by_dim: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for key, entries in catalog.items():
        if available is not None and not available.get(key, True):
            continue  # 데이터가 없는 컬럼 제외
        for e in entries:
            if not _evaluable(e):
                continue  # 골드 SQL 생성 불가 → 평가 대상에서 제외
            by_dim[e["dimension"]].append((key, e))

    max_per_check = max(2, int(size * MAX_CHECK_RATIO))
    picked: list[dict] = []
    check_count: Counter = Counter()

    for dim, ratio in DIM_RATIO.items():
        quota = round(size * ratio)
        pool = by_dim.get(dim, [])
        rng.shuffle(pool)

        # 체크 유형이 고르게 섞이도록 유형별로 라운드로빈
        by_check: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for key, e in pool:
            by_check[e["check_id"]].append((key, e))
        for lst in by_check.values():
            rng.shuffle(lst)

        order = sorted(by_check, key=lambda c: len(by_check[c]))  # 희소 유형 우선
        taken = 0
        while taken < quota:
            progressed = False
            for check_id in order:
                if taken >= quota:
                    break
                if check_count[check_id] >= max_per_check:
                    continue
                if not by_check[check_id]:
                    continue
                key, e = by_check[check_id].pop()
                table, column = key.split(".", 1)
                # 골드 SQL 생성에 필요한 파라미터를 함께 보존한다
                sub = e.get("subcategory", "")
                aspect = ASPECT.get((dim, sub)) or ASPECT.get((dim, ""), "값 범위")
                params = {
                    k: e[k]
                    for k in ("threshold_value", "pair_field", "ref_table",
                              "ref_field", "domain", "source_value_field", "col_type",
                              "fk_class", "concept_id", "gender", "unit_concept_ids")
                    if k in e
                }
                picked.append(
                    {
                        "id": f"G{len(picked) + 1:03d}",
                        "table": table,
                        "column": column,
                        "aspect": aspect,
                        "aspect_code": ASPECT_CODE.get(aspect, "other"),
                        "dimension": dim,
                        "subcategory": sub,
                        "complexity": _complexity_of(check_id, table, e),
                        "expected_checks": [check_id],
                        "threshold": e.get("threshold_value"),
                        "gold_params": params,
                        "query": (
                            f"{table}의 {column}을(를) {aspect} 관점으로 "
                            "평가하는 지표 만들어줘"
                        ),
                    }
                )
                check_count[check_id] += 1
                taken += 1
                progressed = True
            if not progressed:
                break  # 더 뽑을 조합이 없다

    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=100, help="골든셋 크기")
    ap.add_argument("--seed", type=int, default=42, help="재현성을 위한 시드")
    ap.add_argument(
        "--check-data", action="store_true",
        help="DB에서 컬럼별 데이터 존재 여부를 확인해 빈 컬럼을 제외",
    )
    args = ap.parse_args()

    catalog = load_catalog()
    print(f"대상 조합: {sum(len(v) for v in catalog.values())}건 ({len(catalog)}개 컬럼)")

    available = None
    if args.check_data:
        print("DB에서 데이터 존재 여부 확인 중...")
        available = check_data_availability(sorted(catalog))
        empty = [k for k, ok in available.items() if not ok]
        print(f"  값이 없는 컬럼 {len(empty)}개 제외")

    items = stratified_sample(catalog, args.size, args.seed, available)

    # 차원별로 묶인 순서를 섞는다 — --limit 로 일부만 돌려도 분포가 유지되도록
    random.Random(args.seed).shuffle(items)
    for i, it in enumerate(items, 1):
        it["id"] = f"G{i:03d}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "source": "OHDSI DQD OMOP_CDMv5.3 Field_Level",
                "size": len(items),
                "seed": args.seed,
                "data_verified": bool(args.check_data),
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n→ {OUT}  ({len(items)}건)")
    print("\n차원별(Kahn):", dict(Counter(i["dimension"] for i in items)))
    print("하위분류:", dict(Counter(
        f'{i["dimension"]}/{i["subcategory"] or "-"}' for i in items)))
    print("복잡도별:", dict(Counter(i["complexity"] for i in items)))
    print("테이블별:", dict(Counter(i["table"] for i in items).most_common()))
    print("\n체크 유형별:")
    for c, n in Counter(i["expected_checks"][0] for i in items).most_common():
        print(f"  {c:36s}{n:3d}")


if __name__ == "__main__":
    main()