"""표준 OMOP CDM v5.3 스펙(OHDSI 공식 CSV) → cdm_schema.json / cdm_schema_graph.json 생성.

사용법:
    python scripts/build_schema_json.py                  # 다운로드 후 생성
    python scripts/build_schema_json.py --offline DIR    # 받아둔 CSV 디렉토리 사용

출력:
    data/cdm/schema/cdm_schema.json        (9요소 포맷, models.FieldInfo와 1:1)
    data/cdm/schema/cdm_schema_graph.json  (FK 간선 목록 — fk_graph.py 입력)

주의:
    - nullable은 스펙의 isRequired를 반전해 기록한다 (isRequired=Yes → nullable=No).
      (이전 리포의 cdm_schema.json은 이 값이 반전되어 있었음 — 본 스크립트가 정정본)
    - 실DB DDL이 확정되면 이 스크립트 출력 대신 실DB 기준 JSON으로 교체하면 되고,
      포맷만 같으면 이후 코드는 무변경.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/OHDSI/CommonDataModel/main/inst/csv"
FIELD_CSV = "OMOP_CDMv5.3_Field_Level.csv"
TABLE_CSV = "OMOP_CDMv5.3_Table_Level.csv"

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "cdm" / "schema"

# CDM 스키마에 실데이터로 존재하지 않는 영역 제외 (필요 시 조정)
EXCLUDE_SCHEMAS = {"results"}  # cohort, cohort_definition 등


def _yn(v: str) -> str:
    return "Yes" if str(v).strip().lower() in {"yes", "y", "true", "1"} else "No"


def _read_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def _fetch(name: str, offline_dir: str | None) -> str:
    if offline_dir:
        return (Path(offline_dir) / name).read_text(encoding="utf-8-sig")
    with urllib.request.urlopen(f"{BASE}/{name}", timeout=30) as r:
        return r.read().decode("utf-8-sig")


def build(offline_dir: str | None = None) -> tuple[dict, dict]:
    fields = _read_csv(_fetch(FIELD_CSV, offline_dir))
    tables = _read_csv(_fetch(TABLE_CSV, offline_dir))

    table_meta = {
        r["cdmTableName"].strip().lower(): r
        for r in tables
        if r.get("cdmTableName")
    }
    excluded_tables = {
        t for t, r in table_meta.items()
        if str(r.get("schema", "")).strip().lower() in EXCLUDE_SCHEMAS
    }

    schema: dict[str, dict] = {}
    edges: list[dict] = []

    for row in fields:
        tname = row["cdmTableName"].strip().lower()
        if not tname or tname in excluded_tables:
            continue

        if tname not in schema:
            meta = table_meta.get(tname, {})
            schema[tname] = {
                "schema": {
                    "table_name": tname,
                    "table_description": (meta.get("tableDescription") or "").strip(),
                    "fields": [],
                }
            }

        field: dict = {
            "name": row["cdmFieldName"].strip().lower(),
            "type": row["cdmDatatype"].strip().lower(),
            "description": (row.get("userGuidance") or "").strip(),
            "nullable": "No" if _yn(row["isRequired"]) == "Yes" else "Yes",
        }
        if _yn(row.get("isPrimaryKey", "")) == "Yes":
            field["primary_key"] = "Yes"

        # isForeignKey 플래그와 fkTableName이 불일치하는 행이 있다.
        # 예: CommonDataModel의 procedure_occurrence.visit_occurrence_id는
        #     isForeignKey=No 인데 fkTableName=VISIT_OCCURRENCE 로 채워져 있고,
        #     DQD 저장소에서는 같은 필드가 isForeignKey=Yes 다.
        # fkTableName이 있으면 실제 참조 관계가 존재하므로 이를 기준으로 삼는다.
        fk_table_raw = row.get("fkTableName", "").strip()
        if fk_table_raw and fk_table_raw.lower() not in {"na", "n/a", "none", "-"}:
            ref_table = row["fkTableName"].strip().lower()
            ref_field = row["fkFieldName"].strip().lower()
            fk: dict = {"reference_table": ref_table, "reference_field": ref_field}
            domain = (row.get("fkDomain") or "").strip()
            if domain:
                fk["domain_info"] = f"[domain.domain_name] = {domain}"
            field["foreign_key"] = fk

            if ref_table not in excluded_tables:
                edges.append(
                    {
                        "from_table": tname,
                        "from_column": field["name"],
                        "to_table": ref_table,
                        "to_column": ref_field,
                    }
                )

        schema[tname]["schema"]["fields"].append(field)

    graph = {
        "cdm_version": "5.3",
        "source": f"OHDSI CommonDataModel {FIELD_CSV}",
        "edges": edges,
    }
    return schema, graph


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", default=None, help="받아둔 CSV 디렉토리 경로")
    args = ap.parse_args()

    schema, graph = build(args.offline)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "cdm_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "cdm_schema_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_fields = sum(len(t["schema"]["fields"]) for t in schema.values())
    print(f"tables={len(schema)}  fields={n_fields}  fk_edges={len(graph['edges'])}")
    print(f"→ {OUT_DIR / 'cdm_schema.json'}")
    print(f"→ {OUT_DIR / 'cdm_schema_graph.json'}")


if __name__ == "__main__":
    sys.exit(main())