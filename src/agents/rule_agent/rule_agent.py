"""Rule Generation 에이전트 진입 클래스.
리포 경로: src/agents/rule_agent/rule_agent.py
"""
from __future__ import annotations

from core.dqd_catalog import DQDCatalog, get_dqd_catalog
from core.llm import LLM, get_llm
from models.intent import IntentResult
from models.rule import QualityRule
from models.schema_link import SchemaLinkResult

from .generator import generate_rules


class RuleAgent:
    def __init__(self, llm: LLM | None = None, catalog: DQDCatalog | None = None):
        self.llm = llm or get_llm()
        self.catalog = catalog or get_dqd_catalog()

    def run(
        self,
        intent: IntentResult,
        schema_link: SchemaLinkResult,
        feedback: str | None = None,
    ) -> tuple[list[QualityRule], list[str]]:
        """feedback: Rule Validation 불합격 사유 (재생성 시 주입)."""
        return generate_rules(
            intent=intent,
            schema_link=schema_link,
            llm=self.llm,
            feedback=feedback,
            catalog=self.catalog,
        )