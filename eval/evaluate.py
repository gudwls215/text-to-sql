"""
Text-to-SQL 성능 평가 CLI.

사용법:
    python -m eval.evaluate                 # 전체 데이터셋 평가 (결과 DB 저장)
    python -m eval.evaluate --difficulty easy
    python -m eval.evaluate --limit 5
    python -m eval.evaluate --note "프롬프트 v2 실험"
    python -m eval.evaluate --no-store      # DB 저장 없이 리포트만
    python -m eval.evaluate --history       # 과거 실행 이력 보기

요건:
    - PostgreSQL `bird` DB 적재 + .env 의 DB_* 변수
    - OPENAI_API_KEY (또는 SUT 가 사용하는 키)
    - SUT 진입점: 기본 main:generate_sql (T2S_ENTRYPOINT 로 교체 가능)

지표: execution accuracy = (정답 SQL 결과 == 생성 SQL 결과) 비율.
이력: 매 실행마다 정확도/LLM 모델/코드 버전/데이터셋 원본/틀린 내역을
      eval.run · eval.result 테이블에 저장한다 (eval/store.py).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from eval import runner, store


def evaluate(
    difficulty: str | None = None,
    limit: int | None = None,
    *,
    do_store: bool = True,
    note: str | None = None,
) -> int:
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

    results: list[dict] = []
    correct = 0
    t_start = time.time()
    print(f"\n{'='*70}\nText-to-SQL 평가 — {len(items)}개 문항 (schema={schema})\n{'='*70}")
    for it in items:
        qid, question, gold_sql = it["id"], it["question"], it["gold_sql"]
        try:
            gold_rows = runner.run_sql(conn, gold_sql)
        except Exception as e:  # noqa: BLE001
            print(f"[{qid}] ⚠️  정답 SQL 실행 실패 — 데이터셋 점검 필요: {e}")
            # 채점 대상에서는 제외하되(정확도 분모 불변) 이력에는 남긴다.
            results.append({
                "item_id": qid, "difficulty": it["difficulty"], "question": question,
                "gold_sql": gold_sql, "pred_sql": None, "passed": False,
                "error": f"gold SQL 실행 실패: {e}", "latency_seconds": None,
            })
            continue

        t0 = time.time()
        err: str | None = None
        pred_sql = "<생성 실패>"
        try:
            pred_sql = generate_sql(question, schema_text)
            pred_rows = runner.run_sql(conn, pred_sql)
            ok = runner.results_match(gold_rows, pred_rows)
        except Exception as e:  # noqa: BLE001
            ok = False
            err = str(e)
            print(f"[{qid}] ❌ 실행 오류: {e}")
        dt = time.time() - t0

        correct += int(ok)
        mark = "✅" if ok else "❌"
        print(f"[{qid}] {mark}  {question}  ({dt:.1f}s)")
        if not ok:
            print(f"        gold: {gold_sql}")
            print(f"        pred: {pred_sql}")

        results.append({
            "item_id": qid, "difficulty": it["difficulty"], "question": question,
            "gold_sql": gold_sql, "pred_sql": pred_sql, "passed": ok,
            "error": err, "latency_seconds": dt,
        })

    total = len(items)  # 채점 분모: 필터링된 문항 수 (gold 실패분도 오답 처리)
    acc = correct / total if total else 0.0
    duration = time.time() - t_start
    print(f"{'='*70}\n실행 정확도(execution accuracy): {correct}/{total} = {acc:.1%}\n{'='*70}")

    if do_store:
        sha, dataset_json = store.dataset_fingerprint(runner.DATASET_PATH)
        summary = {
            "code_version": store.code_version(),
            "llm_model": runner.resolve_model_name(),
            "entrypoint": os.environ.get("T2S_ENTRYPOINT", runner.DEFAULT_ENTRYPOINT),
            "db_schema": schema,
            "dataset_path": str(runner.DATASET_PATH),
            "dataset_sha256": sha,
            "dataset_json": dataset_json,
            "difficulty": difficulty,
            "limit_n": limit,
            "total": total,
            "correct": correct,
            "accuracy": acc,
            "duration_seconds": duration,
            "note": note,
        }
        try:
            run_id = store.save_run(summary, results)
            print(f"📁 이력 저장됨 — eval.run id={run_id} "
                  f"(eval.result {len(results)}건). --history 로 조회.")
        except Exception as e:  # noqa: BLE001 — 저장 실패가 평가 자체를 망치지 않도록
            print(f"⚠️  이력 저장 실패(평가는 정상 완료): {e}", file=sys.stderr)

    conn.close()
    return 0


def show_history(limit: int) -> int:
    runs = store.recent_runs(limit=limit)
    if not runs:
        print("저장된 평가 이력이 없습니다. 먼저 `python -m eval.evaluate` 를 실행하세요.")
        return 0
    print(f"\n{'='*92}\n최근 평가 이력 (최대 {limit}건)\n{'='*92}")
    print(f"{'id':>4}  {'when':<19}  {'acc':>6}  {'n':>7}  {'model':<14}  {'commit':<9}  note")
    print(f"{'-'*92}")
    for r in runs:
        when = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        acc = f"{r['accuracy']:.0%}"
        n = f"{r['correct']}/{r['total']}"
        model = (r["llm_model"] or "-")[:14]
        commit = (r["git_commit"] or "-")[:8]
        dirty = "*" if r["git_dirty"] else ""
        note = r["note"] or ""
        print(f"{r['id']:>4}  {when:<19}  {acc:>6}  {n:>7}  {model:<14}  {commit:<8}{dirty:<1} {note}")
    print(f"{'='*92}")
    print("commit 뒤 '*' = 작업트리에 커밋되지 않은 변경 있음(dirty).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Text-to-SQL 한국어 평가")
    p.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    p.add_argument("--limit", type=int)
    p.add_argument("--note", help="이 실행에 대한 메모 (이력에 함께 저장)")
    p.add_argument("--no-store", action="store_true", help="결과를 DB 에 저장하지 않음")
    p.add_argument("--history", action="store_true", help="과거 실행 이력을 출력하고 종료")
    args = p.parse_args()

    if args.history:
        return show_history(limit=args.limit or 20)

    return evaluate(
        difficulty=args.difficulty,
        limit=args.limit,
        do_store=not args.no_store,
        note=args.note,
    )


if __name__ == "__main__":
    sys.exit(main())
