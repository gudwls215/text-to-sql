"""중앙 LLM 클라이언트 — 모든 외부 LLM 호출의 단일 경유지.

CLAUDE.md 규약: "모든 외부 LLM 호출은 core/llm_client.py 경유 (직접 SDK
호출 금지)". 프롬프트 생성기(main)와 self-correction 그래프의 judge 가 모두
이 모듈을 통해 호출하므로, 모델/엔드포인트/재시도 정책을 한 곳에서 바꿀 수
있다.

클라이언트는 지연 생성한다(import 만으로 API 키를 요구하지 않도록). 이는
DB/LLM 없이 도는 단위 테스트가 이 모듈을 임포트할 수 있게 해준다.
"""
from __future__ import annotations

import os
from typing import Any

from openai import OpenAI

try:  # langsmith 는 선택적 — 없거나 트레이싱 미설정이면 그냥 통과
    from langsmith.wrappers import wrap_openai
except Exception:  # noqa: BLE001
    wrap_openai = None  # type: ignore[assignment]

# 기본 모델 — main.MODEL 과 어긋나지 않도록 호출부에서 model 을 명시하는 것을
# 권장하되, 미지정 시 환경변수로 덮을 수 있게 한다.
DEFAULT_MODEL = os.environ.get("T2S_MODEL", "gpt-4o-mini")

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """OpenAI 클라이언트(지연 생성)를 반환한다.

    LangSmith 트레이싱이 켜져 있으면(env: LANGSMITH_TRACING/LANGSMITH_API_KEY)
    `wrap_openai` 로 감싸 모든 chat 호출의 프롬프트·응답·토큰이 자동으로
    추적된다. 미설정이면 wrap_openai 는 사실상 passthrough 라 부작용이 없다.
    이렇게 중앙 한 곳에서 감싸므로 generate/judge 등 모든 LLM 호출이 트레이스에
    LangGraph 노드의 자식 run 으로 나타난다.
    """
    global _client
    if _client is None:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        _client = wrap_openai(client) if wrap_openai is not None else client
    return _client


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """채팅 완성을 호출해 응답 텍스트를 반환한다."""
    resp = get_client().chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


def complete(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """단일 user 프롬프트로 chat() 을 호출하는 편의 함수."""
    return chat(
        [{"role": "user", "content": prompt}],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
