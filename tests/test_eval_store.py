"""eval.store 의 순수 헬퍼 테스트 — DB 없이 항상 실행된다.

이력 저장 SQL(save_run/recent_runs) 은 DB 가 필요하므로 통합 테스트
(test_execution_accuracy 흐름)에서 다뤄지고, 여기서는 DB 없이 검증 가능한
지문(fingerprint)·코드버전·DDL 만 확인한다.
"""
from __future__ import annotations

from eval import runner, store


def test_dataset_fingerprint_is_deterministic_and_parses():
    sha1, data1 = store.dataset_fingerprint(runner.DATASET_PATH)
    sha2, data2 = store.dataset_fingerprint(runner.DATASET_PATH)
    assert sha1 == sha2                      # 같은 파일 → 같은 해시
    assert len(sha1) == 64                   # sha256 hex 길이
    assert data1 == data2
    assert "items" in data1 and data1["items"]  # 원본 전체가 보존됨


def test_code_version_has_expected_keys():
    ver = store.code_version()
    assert set(ver) == {"commit", "branch", "dirty"}
    # git repo 안에서 실행되므로 커밋 해시는 채워져 있어야 한다.
    assert ver["commit"] and len(ver["commit"]) >= 7
    assert isinstance(ver["dirty"], bool)


def test_ddl_targets_isolated_eval_schema():
    # public 오염 방지: 이력 테이블은 반드시 별도 eval 스키마에 둔다.
    assert "CREATE SCHEMA IF NOT EXISTS eval" in store.DDL
    assert "eval.run" in store.DDL and "eval.result" in store.DDL
