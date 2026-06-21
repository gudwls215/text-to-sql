"""core 패키지.

import 시 OS 인증서 저장소(시스템 트러스트 스토어)를 Python 의 ssl 에 주입한다.
사내 SSL 검사(MITM 프록시) 환경에서는 사내 루트 CA 가 certifi 번들에 없어
LangSmith 트레이스 업로드(api.smith.langchain.com)나 기타 HTTPS 호출이
CERTIFICATE_VERIFY_FAILED 로 실패한다. truststore 로 OS 저장소(사내 CA 포함)를
쓰게 해 이를 해결한다 — pyproject 의 `[tool.uv] native-tls = true` 와 같은 취지.

truststore 가 없거나 주입에 실패해도 조용히 넘어간다(앱 동작 자체엔 영향 없음).
끄려면 환경변수 T2S_NO_TRUSTSTORE=1 을 설정한다.
"""
from __future__ import annotations

import os as _os


def _inject_truststore() -> None:
    if _os.environ.get("T2S_NO_TRUSTSTORE") == "1":
        return
    try:
        import truststore  # type: ignore[import-not-found]

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 — 트러스트 주입 실패가 import 를 깨면 안 됨
        pass


_inject_truststore()
