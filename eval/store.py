"""평가 결과 이력(history)을 PostgreSQL 에 저장.

정확도 리포트를 재현·추적할 수 있도록 두 테이블에 적재한다.

    eval.run     — 실행 1건의 요약 (정확도, LLM 모델, 코드 버전, 데이터셋 원본 등)
    eval.result  — 문항별 내역 (gold/pred SQL, 통과 여부, 에러 — '틀린 내역' 포함)

설계 메모:
    이력 테이블은 일부러 별도 `eval` 스키마에 둔다. `public` 에 두면
    `core.schema_introspect.fetch_schema('public')` 이 이 테이블들까지 읽어
    LLM 프롬프트(와 데이터셋 화이트리스트)를 오염시키기 때문이다.

    재현성을 위해 데이터셋 원본 전체(JSONB)와 sha256, 실행 시점의 git
    커밋/브랜치/dirty 여부를 함께 남긴다. 같은 커밋 + 같은 데이터셋 해시면
    동일 조건의 재실행임을 보장할 수 있다.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from psycopg2.extras import Json

from eval import runner

REPO_ROOT = Path(__file__).resolve().parent.parent

DDL = """
CREATE SCHEMA IF NOT EXISTS eval;

CREATE TABLE IF NOT EXISTS eval.run (
    id               BIGSERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    git_commit       TEXT,
    git_branch       TEXT,
    git_dirty        BOOLEAN,
    llm_model        TEXT,
    entrypoint       TEXT,
    db_schema        TEXT,
    dataset_path     TEXT,
    dataset_sha256   TEXT,
    dataset_json     JSONB,
    difficulty       TEXT,
    limit_n          INTEGER,
    total            INTEGER          NOT NULL,
    correct          INTEGER          NOT NULL,
    accuracy         DOUBLE PRECISION NOT NULL,
    duration_seconds DOUBLE PRECISION,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS eval.result (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT  NOT NULL REFERENCES eval.run(id) ON DELETE CASCADE,
    item_id         TEXT    NOT NULL,
    difficulty      TEXT,
    question        TEXT,
    gold_sql        TEXT,
    pred_sql        TEXT,
    passed          BOOLEAN NOT NULL,
    error           TEXT,
    latency_seconds DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_eval_result_run    ON eval.result(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_result_passed ON eval.result(run_id, passed);
"""


# ── 메타데이터 수집 (순수 함수 — DB 불필요) ────────────────────────────────

def dataset_fingerprint(path: Path | str) -> tuple[str, dict[str, Any]]:
    """데이터셋 파일의 (sha256, 파싱된 원본 dict) 을 반환한다."""
    raw = Path(path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    return sha, json.loads(raw.decode("utf-8"))


def code_version() -> dict[str, Any]:
    """실행 시점의 코드 버전(git 커밋/브랜치/작업트리 dirty 여부)."""
    def _git(*args: str) -> str | None:
        try:
            out = subprocess.check_output(
                ["git", *args], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
            )
            return out.decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 — git 이 없거나 repo 가 아니어도 평가는 계속
            return None

    status = _git("status", "--porcelain")
    return {
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": None if status is None else bool(status.strip()),
    }


# ── 저장 ────────────────────────────────────────────────────────────────────

def ensure_tables(conn) -> None:
    """eval 스키마와 run/result 테이블을 멱등 생성한다."""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def save_run(summary: dict[str, Any], results: list[dict[str, Any]], conn=None) -> int:
    """실행 요약 1건 + 문항별 내역을 적재하고 run id 를 반환한다.

    conn 을 주지 않으면 자체적으로 연결을 열고 닫는다. 어떤 경우든 평가
    본체와 별개의 트랜잭션으로 커밋한다(평가용 연결의 SELECT-rollback 규율과
    엮이지 않도록).
    """
    own = conn is None
    if own:
        conn = runner.get_connection()
    try:
        ensure_tables(conn)
        ver = summary.get("code_version") or {}
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO eval.run
                    (git_commit, git_branch, git_dirty, llm_model, entrypoint,
                     db_schema, dataset_path, dataset_sha256, dataset_json,
                     difficulty, limit_n, total, correct, accuracy,
                     duration_seconds, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    ver.get("commit"),
                    ver.get("branch"),
                    ver.get("dirty"),
                    summary.get("llm_model"),
                    summary.get("entrypoint"),
                    summary.get("db_schema"),
                    summary.get("dataset_path"),
                    summary.get("dataset_sha256"),
                    Json(summary.get("dataset_json")),
                    summary.get("difficulty"),
                    summary.get("limit_n"),
                    summary["total"],
                    summary["correct"],
                    summary["accuracy"],
                    summary.get("duration_seconds"),
                    summary.get("note"),
                ),
            )
            run_id = cur.fetchone()[0]
            for r in results:
                cur.execute(
                    """
                    INSERT INTO eval.result
                        (run_id, item_id, difficulty, question, gold_sql,
                         pred_sql, passed, error, latency_seconds)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        run_id,
                        r["item_id"],
                        r.get("difficulty"),
                        r.get("question"),
                        r.get("gold_sql"),
                        r.get("pred_sql"),
                        r["passed"],
                        r.get("error"),
                        r.get("latency_seconds"),
                    ),
                )
        conn.commit()
        return run_id
    finally:
        if own:
            conn.close()


# ── 조회 (이력 보기) ────────────────────────────────────────────────────────

def recent_runs(limit: int = 20, conn=None) -> list[dict[str, Any]]:
    """최근 실행 이력을 요약해 반환한다 (`--history` 용)."""
    own = conn is None
    if own:
        conn = runner.get_connection()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, accuracy, correct, total, llm_model,
                       git_commit, git_dirty, difficulty, dataset_sha256, note
                FROM eval.run
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        if own:
            conn.close()
