"""
pytest 공용 픽스처/스킵 로직.

이 파일과 테스트들은 *구현이 아니라 안정 계약(eval.runner)* 에만 의존하므로
main.py 내부가 바뀌어도 수정할 필요가 없다.

테스트 계층:
  1) test_dataset.py        — DB/LLM 불필요. 항상 실행되는 데이터셋 무결성 검증.
  2) test_execution_accuracy.py — DB+LLM 필요. 조건 미충족 시 자동 skip.

통합 테스트 실행 조건(모두 충족 시에만 수행):
  - 환경변수 RUN_EVAL=1
  - DB 연결 가능 (.env 의 DB_*)
  - SUT 진입점 임포트 가능 (기본 main:generate_sql)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 레포 루트를 import 경로에 추가 (main, eval 임포트용)
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import runner  # noqa: E402


def _db_available() -> bool:
    try:
        conn = runner.get_connection()
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _sut_available() -> bool:
    try:
        runner.resolve_generate_sql()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def db_conn():
    """세션 단위 DB 연결. 통합 테스트에서만 사용."""
    conn = runner.get_connection()
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def schema_text(db_conn):
    meta = runner.dataset_meta()
    return runner.get_schema_text(db_conn, meta.get("schema", "public"))


@pytest.fixture(scope="session")
def generate_sql():
    return runner.resolve_generate_sql()


# 통합 테스트 전용 스킵 마커 — 조건 미충족 시 깔끔히 skip
requires_eval_env = pytest.mark.skipif(
    not (os.environ.get("RUN_EVAL") == "1" and _db_available() and _sut_available()),
    reason="통합 평가 조건 미충족 (RUN_EVAL=1 + DB 연결 + SUT 진입점 필요)",
)
