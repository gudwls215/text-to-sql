"""
Text-to-SQL 성능 평가 CLI.

사용법:
    python -m eval.evaluate                 # 전체 데이터셋 평가
    python -m eval.evaluate --difficulty easy
    python -m eval.evaluate --limit 5

요건:
    - PostgreSQL `bird` DB 적재 + .env 의 DB_* 변수
    - OPENAI_API_KEY (또는 SUT 가 사용하는 키)
    - SUT 진입점: 기본 main:generate_sql (T2S_ENTRYPOINT 로 교체 가능)

지표: execution accuracy = (정답 SQL 결과 == 생성 SQL 결과) 비율.
"""
from __future__ import annotations

import argparse
import sys
import time

from eval import runner


def evaluate(difficulty: str | None = None, limit: int | None = None) -> int:
    items = runner.load_dataset()
    if difficulty:
        items = [it for it in items if it["difficulty"] == difficulty]
    if limit:
        items = items[:limit]

    meta = runner.dataset_meta()
    schema = meta.get("schema", "public")

    generate_sql = runner.resolve_generate_sql()
    conn = runner.get_connection()
    schema_text = runner.get_schema_text(conn, schema)

    correct = 0
    print(f"\n{'='*70}\nText-to-SQL 평가 — {len(items)}개 문항 (schema={schema})\n{'='*70}")
    for it in items:
        qid, question, gold_sql = it["id"], it["question"], it["gold_sql"]
        try:
            gold_rows = runner.run_sql(conn, gold_sql)
        except Exception as e:  # noqa: BLE001
            print(f"[{qid}] ⚠️  정답 SQL 실행 실패 — 데이터셋 점검 필요: {e}")
            continue

        t0 = time.time()
        try:
            pred_sql = generate_sql(question, schema_text)
            pred_rows = runner.run_sql(conn, pred_sql)
            ok = runner.results_match(gold_rows, pred_rows)
        except Exception as e:  # noqa: BLE001
            pred_sql = locals().get("pred_sql", "<생성 실패>")
            ok = False
            print(f"[{qid}] ❌ 실행 오류: {e}")
        dt = time.time() - t0

        correct += int(ok)
        mark = "✅" if ok else "❌"
        print(f"[{qid}] {mark}  {question}  ({dt:.1f}s)")
        if not ok:
            print(f"        gold: {gold_sql}")
            print(f"        pred: {pred_sql}")

    total = len(items)
    acc = correct / total if total else 0.0
    print(f"{'='*70}\n실행 정확도(execution accuracy): {correct}/{total} = {acc:.1%}\n{'='*70}")
    conn.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Text-to-SQL 한국어 평가")
    p.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    return evaluate(difficulty=args.difficulty, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
