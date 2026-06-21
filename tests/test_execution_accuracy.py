"""
실행 정확도(execution accuracy) 통합 테스트.

각 문항마다 SUT 의 generate_sql 로 SQL 을 생성·실행하고, 정답 SQL 의 실행
결과와 비교한다. 구현 내부가 아니라 계약(generate_sql + 결과 동등성)에만
의존하므로 main.py 가 어떻게 바뀌어도 이 파일은 수정할 필요가 없다.

실행 조건(conftest.requires_eval_env): RUN_EVAL=1 + DB 연결 + SUT 임포트.
조건 미충족 시 전체가 skip 되어 테스트 스위트는 항상 통과 상태를 유지한다.

    # 통합 평가 실행 예시 (PowerShell)
    $env:RUN_EVAL = "1"; python -m pytest tests/test_execution_accuracy.py -v
"""
from __future__ import annotations

import pytest

from eval import runner
from tests.conftest import requires_eval_env

ITEMS = runner.load_dataset()


@requires_eval_env
@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_question_execution_accuracy(item, db_conn, schema_text, generate_sql):
    """문항별 실행 정확도 — 생성 SQL 결과가 정답 SQL 결과와 일치해야 한다."""
    gold_rows = runner.run_sql(db_conn, item["gold_sql"])

    pred_sql = generate_sql(item["question"], schema_text)
    assert pred_sql and pred_sql.strip(), f"{item['id']}: 빈 SQL 생성"

    pred_rows = runner.run_sql(db_conn, pred_sql)
    assert runner.results_match(gold_rows, pred_rows), (
        f"{item['id']} 결과 불일치\n"
        f"  질문: {item['question']}\n"
        f"  gold: {item['gold_sql']} -> {gold_rows[:5]}\n"
        f"  pred: {pred_sql} -> {pred_rows[:5]}"
    )


@requires_eval_env
def test_overall_accuracy_threshold(db_conn, schema_text, generate_sql):
    """
    전체 실행 정확도가 임계값 이상인지 검증(회귀 가드).
    임계값은 환경변수 T2S_MIN_ACCURACY 로 조정(기본 0.0 = 측정만, 실패 안 함).
    """
    import os

    threshold = float(os.environ.get("T2S_MIN_ACCURACY", "0.0"))
    correct = 0
    for item in ITEMS:
        try:
            gold_rows = runner.run_sql(db_conn, item["gold_sql"])
            pred_rows = runner.run_sql(db_conn, generate_sql(item["question"], schema_text))
            correct += int(runner.results_match(gold_rows, pred_rows))
        except Exception:  # noqa: BLE001
            pass
    acc = correct / len(ITEMS) if ITEMS else 0.0
    print(f"\n실행 정확도: {correct}/{len(ITEMS)} = {acc:.1%}")
    assert acc >= threshold, f"정확도 {acc:.1%} < 임계값 {threshold:.1%}"
