"""로컬 vLLM(Qwen3-32B) 클라이언트 — 모든 에이전트의 LLM 호출 단일 창구.

핵심 설계:
  1) structured(): vLLM의 guided_json(문법 제약 디코딩)으로 출력을
     Pydantic 스키마에 강제한다 → JSON 파싱 실패가 구조적으로 불가능.
     실패 시(스키마는 맞지만 의미가 깨진 경우 등) 에러를 프롬프트에 붙여 재시도.
  2) Qwen3 thinking 모드 제어: 구조화 출력·분류 태스크는 enable_thinking=False로
     끄고(속도·안정성), 혹시 켜진 채 응답이 와도 <think>...</think> 블록을 제거.
  3) temperature=0 기본 (LLM-DQR 논문과 동일, 재현성).

vLLM 서버 실행 예 (연구실 서버):
    vllm serve Qwen/Qwen3-32B \
        --max-model-len 16384 \
        --tensor-parallel-size 2 \
        --gpu-memory-utilization 0.90
    # VRAM 부족 시(단일 48GB 등): Qwen/Qwen3-32B-AWQ 또는 FP8 양자화본 사용
"""
from __future__ import annotations

import json
import re
from typing import TypeVar

import logging

from openai import OpenAI
from pydantic import BaseModel, ValidationError

import config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def harden_schema(schema: dict) -> dict:
    """JSON Schema를 구조화 출력용으로 강화.

    - additionalProperties: false → 스키마에 없는 키(예: LLM이 임의로 만든 "slots"
      래퍼)를 원천 차단. 이번 실패의 직접 원인이었다.
    - 모든 property를 required로 → 필드 누락 방지 (nullable 타입은 null 허용).
    $defs 등 중첩 정의까지 재귀 적용한다.
    """
    if not isinstance(schema, dict):
        return schema

    out = {k: harden_schema(v) if isinstance(v, (dict, list)) else v
           for k, v in schema.items()}

    for key in ("$defs", "definitions", "properties"):
        if key in out and isinstance(out[key], dict):
            out[key] = {k: harden_schema(v) for k, v in out[key].items()}
    for key in ("items", "additionalItems"):
        if key in out and isinstance(out[key], dict):
            out[key] = harden_schema(out[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in out and isinstance(out[key], list):
            out[key] = [harden_schema(s) for s in out[key]]

    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


def strip_thinking(text: str) -> str:
    """Qwen3 thinking 블록 제거 (닫힌 태그 + 미완결 태그 모두)."""
    text = _THINK_RE.sub("", text)
    if "<think>" in text:  # max_tokens로 잘려 </think>가 없는 경우
        text = text.split("<think>")[0]
    return text.strip()


def _looks_truncated(raw: str) -> bool:
    """출력 길이 제한으로 잘린 응답인지 추정.

    JSON이 열려 있고 닫히지 않았거나, 끝이 숫자·쉼표로 끊긴 경우.
    """
    t = raw.rstrip()
    if not t:
        return False
    if t.count("{") > t.count("}") or t.count("[") > t.count("]"):
        return True
    return bool(t and (t[-1].isdigit() or t.endswith(",")))


def extract_json(text: str) -> str:
    """코드펜스/잡담 속에서 JSON 본문 추출. guided_json 사용 시엔 거의 불필요한
    안전망이지만, 서버 설정에 따라 펜스가 붙는 경우를 방어."""
    text = strip_thinking(text)
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    # 첫 { ~ 마지막 } 또는 첫 [ ~ 마지막 ]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j > i:
            return text[i : j + 1]
    return text


class LLM:
    """vLLM OpenAI 호환 클라이언트 래퍼. client 주입 가능(테스트용)."""

    def __init__(
        self,
        model: str = config.VLLM_MODEL,
        base_url: str = config.VLLM_BASE_URL,
        api_key: str = config.VLLM_API_KEY,
        temperature: float = config.LLM_TEMPERATURE,
        client: OpenAI | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self._use_guided_json = False  # response_format 미지원 서버 감지 시 True
        self.client = client or OpenAI(
            base_url=base_url, api_key=api_key, timeout=config.LLM_TIMEOUT_S
        )

    # ---------- 내부 공통 호출 ----------
    def _chat(
        self,
        system: str,
        user: str,
        *,
        thinking: bool,
        guided_schema: dict | None = None,
        schema_name: str = "output",
        max_tokens: int = config.LLM_MAX_TOKENS,
    ) -> str:
        """구조화 출력 강제 방식은 vLLM 버전에 따라 다르다:
          - 신버전(권장): OpenAI 표준 response_format={"type": "json_schema", ...}
          - 구버전: extra_body={"guided_json": ...}
        신버전 방식을 먼저 시도하고, 서버가 거부하면 자동으로 구버전으로 폴백한다.
        (폴백 여부는 인스턴스에 기억해 이후 호출에서 재시도 비용을 없앰)
        """
        base_kwargs = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # Qwen3 thinking 모드 on/off (vLLM chat template 파라미터)
        extra_body: dict = {"chat_template_kwargs": {"enable_thinking": thinking}}

        if guided_schema is None:
            resp = self.client.chat.completions.create(
                **base_kwargs, extra_body=extra_body
            )
            return resp.choices[0].message.content or ""

        if not self._use_guided_json:
            try:
                resp = self.client.chat.completions.create(
                    **base_kwargs,
                    extra_body=extra_body,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "schema": guided_schema,
                            "strict": True,
                        },
                    },
                )
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001 — 서버가 response_format 미지원
                log.warning(
                    "response_format(json_schema) 실패 → guided_json 폴백: %s", e
                )
                self._use_guided_json = True

        extra_body["guided_json"] = guided_schema
        resp = self.client.chat.completions.create(**base_kwargs, extra_body=extra_body)
        return resp.choices[0].message.content or ""

    # ---------- 공개 API ----------
    def text(self, system: str, user: str, thinking: bool = False) -> str:
        """자유 텍스트 응답 (informational/other 분기, 리포트 문장 생성 등)."""
        return strip_thinking(self._chat(system, user, thinking=thinking))

    def structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        thinking: bool = False,
        max_retries: int = 2,
    ) -> T:
        """Pydantic 스키마로 강제된 구조화 출력.

        guided_json으로 1차 보장 + ValidationError 시 오류 메시지를
        피드백으로 붙여 재시도 (설계 원칙: 사유 없는 재시도 금지).
        """
        json_schema = harden_schema(schema.model_json_schema())
        last_err: Exception | None = None
        last_raw = ""
        prompt = user

        for _ in range(max_retries + 1):
            raw = self._chat(
                system,
                prompt,
                thinking=thinking,
                guided_schema=json_schema,
                schema_name=schema.__name__,
            )
            last_raw = raw
            try:
                return schema.model_validate_json(extract_json(raw))
            except (ValidationError, json.JSONDecodeError) as e:
                last_err = e
                log.warning(
                    "structured output 검증 실패 (%s). raw=%s",
                    schema.__name__,
                    raw[:500],
                )
                # 출력 길이 초과로 잘린 경우: 같은 프롬프트로 재시도해도
                # 같은 지점에서 잘린다. 짧게 쓰라는 지시를 추가해야 한다.
                # (예: plausibleUnitConceptIds에서 LLM이 concept_id 수백 개를
                #  나열하다 max_tokens에 걸려 JSON이 미완성되는 사례)
                truncated = _looks_truncated(raw)
                extra = (
                    "\n[중요: 이전 응답이 출력 길이 제한으로 잘렸다. "
                    "IN 목록 등을 임의로 확장하지 말고, 주어진 값만 사용해 "
                    "가능한 한 짧은 SQL을 작성하라.]"
                    if truncated
                    else ""
                )
                prompt = (
                    f"{user}\n\n"
                    f"[이전 응답이 스키마 검증에 실패했다. 최상위 평면 JSON만 "
                    f"다시 출력하라. 중첩 래퍼 키를 만들지 마라]{extra}\n오류: {e}"
                )
        raise RuntimeError(
            f"structured output failed after {max_retries + 1} attempts "
            f"({schema.__name__}): {last_err}\n마지막 응답: {last_raw[:500]}"
        )


_default: LLM | None = None


def get_llm() -> LLM:
    """기본 클라이언트 싱글턴."""
    global _default
    if _default is None:
        _default = LLM()
    return _default