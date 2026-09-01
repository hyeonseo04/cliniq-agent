"""OMOP v5.3 FK 그래프 — JOIN 경로를 LLM이 아닌 결정론적 탐색으로 확정.

설계 원칙 (design doc §3.1, §5):
  - JOIN은 LLM에게 추측시키지 않는다. cdm_schema_graph.json의 FK 간선만 사용.
  - vocabulary 허브 테이블(concept 등)은 기본적으로 "경유지"에서 제외한다.
    거의 모든 클리니컬 테이블이 concept를 FK로 참조하므로, 이를 허용하면
    drug_exposure ↔ death 같은 쌍이 concept 경유(의미 없는 JOIN)로 연결되어 버린다.
    올바른 경로는 person 경유. (concept를 명시적으로 선택한 경우엔 endpoint로 허용)

입력 그래프 포맷 두 가지 수용:
  1) {"edges": [{"from_table","from_column","to_table","to_column"}, ...]}  ← 표준(신규)
  2) {"location": {"person": {"from": "[person.location_id]", "to": "[location.location_id]"}}}
     ← 이전 리포 포맷
"""
from __future__ import annotations

import json
import re
from collections import deque
from functools import lru_cache
from pathlib import Path

from models.schema_link import JoinEdge

DEFAULT_GRAPH_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "integrated" / "schema" / "cdm_schema_graph.json"
)

# 경유지로 쓰지 않는 vocabulary/메타 허브 테이블
VOCAB_HUBS = frozenset(
    {
        "concept", "concept_class", "concept_relationship", "concept_ancestor",
        "concept_synonym", "vocabulary", "domain", "relationship",
        "source_to_concept_map", "drug_strength", "cdm_source", "metadata",
    }
)

_BRACKET = re.compile(r"\[?\s*([a-z0-9_]+)\.([a-z0-9_]+)\s*\]?", re.IGNORECASE)


def _parse_legacy(raw: dict) -> list[JoinEdge]:
    """이전 리포의 중첩 dict 포맷 → JoinEdge 목록."""
    edges = []
    for _ref_table, links in raw.items():
        for _src_table, link in links.items():
            m_from = _BRACKET.search(link["from"])
            m_to = _BRACKET.search(link["to"])
            if not (m_from and m_to):
                continue
            edges.append(
                JoinEdge(
                    from_table=m_from.group(1).lower(),
                    from_column=m_from.group(2).lower(),
                    to_table=m_to.group(1).lower(),
                    to_column=m_to.group(2).lower(),
                )
            )
    return edges


class FKGraph:
    def __init__(self, graph_path: str | Path = DEFAULT_GRAPH_PATH):
        raw = json.loads(Path(graph_path).read_text(encoding="utf-8"))
        if "edges" in raw:
            self.edges = [JoinEdge(**e) for e in raw["edges"]]
        else:
            self.edges = _parse_legacy(raw)

        # 무방향 인접 리스트: table → [(이웃 table, JoinEdge)]
        self._adj: dict[str, list[tuple[str, JoinEdge]]] = {}
        for e in self.edges:
            self._adj.setdefault(e.from_table, []).append((e.to_table, e))
            self._adj.setdefault(e.to_table, []).append((e.from_table, e))

    # ---------- 경로 탐색 ----------
    def shortest_path(
        self, src: str, dst: str, allow_hubs: bool = False
    ) -> list[JoinEdge] | None:
        """src ↔ dst 최단 JOIN 경로 (BFS). 없으면 None.

        allow_hubs=False(기본): VOCAB_HUBS는 경유지 불가, endpoint만 허용.
        """
        src, dst = src.lower(), dst.lower()
        if src == dst:
            return []
        if src not in self._adj or dst not in self._adj:
            return None

        def passable(node: str) -> bool:
            return allow_hubs or node in (src, dst) or node not in VOCAB_HUBS

        q: deque[str] = deque([src])
        prev: dict[str, tuple[str, JoinEdge]] = {src: None}  # type: ignore[assignment]
        while q:
            cur = q.popleft()
            for nxt, edge in self._adj.get(cur, []):
                if nxt in prev or not passable(nxt):
                    continue
                prev[nxt] = (cur, edge)
                if nxt == dst:
                    path = []
                    node = dst
                    while prev[node] is not None:
                        p, e = prev[node]
                        path.append(e)
                        node = p
                    return list(reversed(path))
                q.append(nxt)
        return None

    def connect_tables(
        self, tables: list[str]
    ) -> tuple[list[JoinEdge], list[str], bool]:
        """선택된 테이블들을 하나의 연결 성분으로 잇는 JOIN 경로 확정.

        Schema Linking의 '연결성 검사' 구현체:
        첫 테이블을 기준으로 나머지를 순차 병합하며 최단경로 합집합을 만든다.
        경유지로 추가된 테이블(브리지, 예: person)도 함께 반환한다.

        Returns:
            (join_edges, bridge_tables, is_connected)
        """
        tables = [t.lower() for t in dict.fromkeys(tables)]  # 중복 제거, 순서 유지
        if len(tables) <= 1:
            return [], [], True

        connected = {tables[0]}
        edges: list[JoinEdge] = []
        seen_edges: set[tuple] = set()

        for target in tables[1:]:
            if target in connected:
                continue
            # 이미 연결된 성분 중 가장 가까운 노드에서 target까지
            best: list[JoinEdge] | None = None
            for anchor in list(connected):
                p = self.shortest_path(anchor, target)
                if p is not None and (best is None or len(p) < len(best)):
                    best = p
            if best is None:
                return edges, [], False  # 연결 불가
            for e in best:
                key = (e.from_table, e.from_column, e.to_table, e.to_column)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(e)
                connected.add(e.from_table)
                connected.add(e.to_table)

        bridges = sorted(connected - set(tables))
        return edges, bridges, True

    def neighbors(self, table: str) -> list[str]:
        return sorted({t for t, _ in self._adj.get(table.lower(), [])})


@lru_cache(maxsize=1)
def get_fk_graph() -> FKGraph:
    return FKGraph()