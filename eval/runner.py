"""
평가 인프라 (구현과 분리된 안정 계층).

핵심 계약(contract):
    프로젝트는 `generate_sql(question: str, schema_text: str) -> str` 형태의
    함수를 제공한다. 기본 진입점은 `main:generate_sql` 이며 환경변수
    `T2S_ENTRYPOINT="모듈:함수"` 로 교체할 수 있다.

이 계약만 지키면 main.py 내부 구현이 어떻게 바뀌어도 테스트/평가 코드는
변경할 필요가 없다. 평가지표는 구현과 무관한 "실행 결과 비교"(execution
accuracy): 정답 SQL과 생성 SQL을 같은 DB에서 실행해 결과 집합을 비교한다.
"""
from __future__ import annotations

import importlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = REPO_ROOT / "eval" / "dataset" / "financial_ko.json"
DEFAULT_ENTRYPOINT = "main:generate_sql"

# 결과 비교 시 부동소수 허용 오차
FLOAT_TOLERANCE = 1e-6


# ── 데이터셋 ────────────────────────────────────────────────────────────────

def load_dataset(path: Path | str = DATASET_PATH) -> list[dict[str, Any]]:
    """평가 데이터셋(JSON)을 로드해 item 리스트를 반환한다."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["items"]


def dataset_meta(path: Path | str = DATASET_PATH) -> dict[str, Any]:
    """데이터셋의 schema/database 등 메타데이터를 반환한다."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── DB 연결 (main.py 와 독립적으로 .env 기반 연결) ─────────────────────────

def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env")


def get_connection():
    """.env / 환경변수 기반으로 PostgreSQL 연결을 생성한다."""
    import psycopg2  # 지연 임포트: 단위 테스트에서는 불필요

    load_env()
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "bird"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ["DB_PASSWORD"],
    )


def get_schema_text(conn, schema: str = "public") -> str:
    """카탈로그(컬럼·코멘트·FK)에서 풍부한 스키마 문자열을 만든다.

    main.py 와 동일한 `core.schema_introspect` 를 사용해 평가 입력과 실제
    추론 입력이 일치하도록 한다. 코멘트/FK 가 없는 DB 에서도 컬럼/타입만으로
    동작한다(보강 정보는 모두 선택적).
    """
    from core import schema_introspect

    return schema_introspect.render_schema_text(
        schema_introspect.fetch_schema(conn, schema)
    )


def run_sql(conn, sql: str) -> list[tuple]:
    """읽기 전용 SQL 을 실행해 결과 행을 반환한다 (롤백으로 부작용 차단)."""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        conn.rollback()  # SELECT 전용 — 어떤 변경도 남기지 않는다
    return rows


# ── SUT(System Under Test) 진입점 해석 ──────────────────────────────────────

def resolve_generate_sql(entrypoint: str | None = None) -> Callable[[str, str], str]:
    """
    `모듈:함수` 형식의 진입점을 임포트해 generate_sql 콜러블을 반환한다.

    기본값은 main:generate_sql. 코드 구조가 바뀌면 T2S_ENTRYPOINT 환경변수만
    조정하면 되고, 테스트 코드 자체는 수정할 필요가 없다.
    """
    ep = entrypoint or os.environ.get("T2S_ENTRYPOINT", DEFAULT_ENTRYPOINT)
    module_name, _, func_name = ep.partition(":")
    if not module_name or not func_name:
        raise ValueError(f"잘못된 진입점 형식: {ep!r} (예: 'main:generate_sql')")
    module = importlib.import_module(module_name)
    func = getattr(module, func_name)
    if not callable(func):
        raise TypeError(f"{ep} 은 호출 가능한 객체가 아닙니다.")
    return func


def resolve_model_name(entrypoint: str | None = None) -> str | None:
    """SUT 가 사용하는 LLM 모델명을 best-effort 로 알아낸다 (이력 기록용).

    관례상 진입점 모듈이 `MODEL` 상수를 노출하면 그 값을 쓰고, 없으면
    환경변수 `T2S_MODEL` 로 명시할 수 있다. 둘 다 없으면 None.
    """
    ep = entrypoint or os.environ.get("T2S_ENTRYPOINT", DEFAULT_ENTRYPOINT)
    module_name = ep.partition(":")[0]
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — 모델명을 못 구해도 평가는 진행
        return os.environ.get("T2S_MODEL")
    return getattr(module, "MODEL", None) or os.environ.get("T2S_MODEL")


# ── 결과 비교 (execution accuracy) ──────────────────────────────────────────

def _normalize_value(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        return float(v)
    return v


def _normalize_rows(rows: list[tuple]) -> list[tuple]:
    norm = [tuple(_normalize_value(v) for v in row) for row in rows]
    # ORDER BY 가 없는 쿼리도 동일 취급되도록 다중집합(정렬) 비교
    return sorted(norm, key=lambda r: tuple(str(x) for x in r))


def results_match(gold_rows: list[tuple], pred_rows: list[tuple]) -> bool:
    """두 결과 집합이 (부동소수 오차 허용, 순서 무시) 동일한지 판단한다."""
    g = _normalize_rows(gold_rows)
    p = _normalize_rows(pred_rows)
    if len(g) != len(p):
        return False
    for grow, prow in zip(g, p):
        if len(grow) != len(prow):
            return False
        for gv, pv in zip(grow, prow):
            if isinstance(gv, float) and isinstance(pv, (int, float)):
                if abs(gv - float(pv)) > FLOAT_TOLERANCE:
                    return False
            elif gv != pv:
                return False
    return True
