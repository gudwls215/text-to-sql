"""Self-correction 루프 (LangGraph).

표준 패턴: 초안 SQL 을 생성 → 실제 DB 에서 실행 → judge 가 평가하고 개선
피드백을 제공 → 모델이 수정본을 생성. 이를 반복하다 통과하거나 시도 한도에
도달하면 멈춘다. 반복 여부는 LangGraph **조건부 엣지**로 분기한다.

    START → generate → execute → judge ─┬─(revise)→ generate (루프)
                                        └─(pass/한도)→ END

두 가지 피드백 신호를 결합한다.
    1) 실행 신호(결정적): 실패하면 PostgreSQL 오류 메시지, 빈 결과면 그 사실.
    2) judge 신호(LLM): 실행이 성공한 경우 결과가 질문에 답하는지 의미적으로
       평가하고 개선 피드백을 만든다.
실행이 '실패'한 경우엔 judge LLM 을 호출하지 않고 결정적으로 revise 로 보낸다
(불필요한 호출/비용 절감, 오류는 항상 재시도 보장).

설계 근거는 docs/self_correction_loop.md 참고.
"""
from __future__ import annotations

import json
import logging
import operator
import os
import re
from dataclasses import dataclass
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

import main  # build_generation_prompt / MODEL / _clean_sql (지연참조 없음 — 아래 주석)
from core import llm_client

# 주: correction_graph 는 main 을 import 하지만, main 은 correction_graph 를
# generate_sql_corrected 안에서 '지연' import 하므로 임포트 순환이 생기지 않는다.

DEFAULT_MAX_ATTEMPTS = int(os.environ.get("T2S_MAX_CORRECTIONS", "3"))
SAMPLE_ROWS = 5  # judge 프롬프트에 실어 보낼 결과 표본 행 수
_LOG = logging.getLogger("t2s.correction_graph")


class CorrectionState(TypedDict, total=False):
    # 주: schema_text 는 의도적으로 state 에 넣지 않는다. 거대한 스키마 텍스트가
    # 모든 노드 run 의 입력/출력에 실려 LangSmith 트레이스를 읽기 어렵게 만들기
    # 때문. 대신 build_graph 클로저로 전달한다(conn 과 동일).
    question: str
    sql: str                       # 현재(최신) SQL
    exec_ok: bool
    exec_error: Optional[str]
    row_count: int
    sample_rows: list
    verdict: str                   # "pass" | "revise"
    feedback: str
    attempts: int
    max_attempts: int
    history: Annotated[list, operator.add]  # 시도별 기록 (노드마다 append)


# ── 읽기 전용 실행 (부작용 차단) ─────────────────────────────────────────────

def run_readonly(conn, sql: str) -> list[tuple]:
    """SELECT 전용 실행 — 항상 rollback 으로 어떤 변경도 남기지 않는다."""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        conn.rollback()
    return rows


# ── 트레이싱 활성화 동기화 ──────────────────────────────────────────────────

_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def _graph_log(message: str, *args: Any) -> None:
    """평가 중 LangGraph 진행 상태를 로그로 남긴다 (env: T2S_GRAPH_LOG=1)."""
    if _is_truthy("T2S_GRAPH_LOG"):
        _LOG.info(message, *args)


def _stop_reason(state: CorrectionState) -> str:
    """루프 종료 사유를 사람이 읽기 쉬운 형태로 반환한다."""
    if state.get("verdict") == "pass":
        return "pass"
    attempts = state.get("attempts", 0)
    max_attempts = state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if attempts >= max_attempts:
        return "max_attempts"
    return "unknown"


@dataclass
class RepairResult:
    sql: str
    applied: list[str]


def _question_contract(question: str) -> dict[str, bool]:
    q = question.lower()
    ratio_like = any(tok in q for tok in ("비율", "어느 정도", "퍼센", "%", "율"))
    count_like = ("몇" in q or "몇 개" in q or "count" in q) and not ratio_like
    where_like = any(tok in q for tok in ("어디", "누구", "언제", "어느"))
    return {"ratio_like": ratio_like, "count_like": count_like, "where_like": where_like}


def _rule_based_judge(state: CorrectionState) -> tuple[str, str] | None:
    """LLM judge 전에 질문-출력 형식 불일치를 빠르게 걸러낸다."""
    sql = (state.get("sql") or "").strip().lower()
    if not sql:
        return "revise", "SQL 이 비어 있습니다. 질문에 맞는 SQL 을 생성하세요."

    # 두 개 이상의 쿼리를 한 번에 내면 평가 계약과 어긋난다.
    if ";" in sql.rstrip(";"):
        return "revise", "하나의 질문에는 단일 SQL 문만 출력하세요(다중 문장 금지)."

    contract = _question_contract(state.get("question", ""))
    if contract["count_like"] and "count(" not in sql:
        return "revise", "질문이 '몇/몇 개' 형태입니다. 단일 COUNT 집계 SQL 로 답하세요."
    if contract["ratio_like"] and "/" not in sql:
        return "revise", "질문이 비율/어느 정도 형태입니다. 분자/분모를 나누는 비율 식을 포함하세요."

    return None


def _apply_deterministic_repairs(prev_sql: str, exec_error: str | None) -> RepairResult:
    """빈번한 PostgreSQL 오류를 LLM 호출 전에 1차 규칙으로 고친다."""
    sql = prev_sql
    applied: list[str] = []
    err = (exec_error or "")

    # 1) 잘못된 테이블명 transaction -> trans
    if "transaction" in err.lower() and "relation" in err.lower():
        replaced = re.sub(r'public\."?transaction"?', "public.trans", sql, flags=re.IGNORECASE)
        if replaced != sql:
            sql = replaced
            applied.append("table:transaction->trans")

    # 2) account.client_id 직접 조인 오류 -> disp 브리지 조인으로 치환
    if "client_id" in err and "칼럼 없음" in err:
        pattern = re.compile(
            r"JOIN\s+public\.account\s+(\w+)\s+ON\s+(\w+)\.client_id\s*=\s*\1\.client_id",
            flags=re.IGNORECASE,
        )

        def _bridge(m: re.Match[str]) -> str:
            acc_alias = m.group(1)
            left_alias = m.group(2)
            disp_alias = f"disp_fix_{acc_alias}"
            return (
                f"JOIN public.disp {disp_alias} ON {left_alias}.client_id = {disp_alias}.client_id "
                f"JOIN public.account {acc_alias} ON {disp_alias}.account_id = {acc_alias}.account_id"
            )

        replaced = pattern.sub(_bridge, sql)
        if replaced != sql:
            sql = replaced
            applied.append("join:client->disp->account")

    # 3) district 컬럼 오기 -> district.a2
    if ".district" in sql and "district" in err and "칼럼 없음" in err:
        replaced = re.sub(r"\b(\w+)\.district\b", r"\1.a2", sql)
        if replaced != sql:
            sql = replaced
            applied.append("column:district->a2")

    # 4) date 모호성 -> account.date 우선
    if "모호" in err and re.search(r"\bdate\b", sql, flags=re.IGNORECASE):
        replaced = re.sub(r"\bMAX\(\s*date\s*\)", "MAX(account.date)", sql, flags=re.IGNORECASE)
        replaced = re.sub(r"\bMIN\(\s*date\s*\)", "MIN(account.date)", replaced, flags=re.IGNORECASE)
        if replaced != sql:
            sql = replaced
            applied.append("column:date->account.date")

    return RepairResult(sql=sql, applied=applied)


def enable_native_tracing_if_langsmith() -> None:
    """LANGSMITH_TRACING 만 켠 경우 LANGCHAIN_TRACING_V2 도 켠다.

    LangGraph(=langchain-core) 네이티브 트레이싱은 `LANGCHAIN_TRACING_V2` 만 보고,
    langsmith SDK(wrap_openai)는 `LANGSMITH_TRACING` 을 본다. 둘 중 하나만 켜면
    그래프는 트레이싱되지 않고 LLM 호출만 부모 없는 'ChatOpenAI' run 으로 떠버린다
    (그래서 self_correction 루트도, verdict 태그도 안 보인다). 여기서 둘을 맞춰
    그래프 전체가 하나의 self_correction 트리로 트레이싱되게 한다.

    호출부(LANGCHAIN_TRACING_V2)를 사용자가 명시했으면(끄기 포함) 존중한다.
    """
    if _is_truthy("LANGSMITH_TRACING"):
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")


def _trace_outputs(final: CorrectionState) -> dict[str, Any]:
    """fallback 루트 span 출력용 요약."""
    history = final.get("history") or []
    return {
        "attempts": final.get("attempts"),
        "final_verdict": final.get("verdict"),
        "history_len": len(history),
        "final_sql": final.get("sql"),
    }


def _get_ls_trace():
    """langsmith.trace 컨텍스트매니저를 반환(없으면 None)."""
    try:
        from langsmith import trace as ls_trace
    except Exception:  # noqa: BLE001 — langsmith 미설치
        return None
    return ls_trace


def tag_current_run(**tags: Any) -> None:
    """현재 트레이스 run(=실행 중인 노드)에 `key:value` 태그를 단다.

    LANGCHAIN_TRACING_V2 가 켜져 있으면 노드 실행 컨텍스트에서 run 트리를 얻을 수
    있다. LangSmith run 목록에서 `verdict:revise`·`attempt:2` 로 바로 필터된다.
    트레이싱이 꺼져 있거나 run 트리를 못 얻으면 조용히 no-op 이다.
    """
    try:
        from langsmith import get_current_run_tree
    except Exception:  # noqa: BLE001 — langsmith 미설치
        return
    try:
        rt = get_current_run_tree()
        if rt is None:
            return
        rt.tags = list(getattr(rt, "tags", None) or []) + [f"{k}:{v}" for k, v in tags.items()]
    except Exception:  # noqa: BLE001 — 트레이싱 부가기능이 본 로직을 깨면 안 됨
        return


# ── judge (LLM) ─────────────────────────────────────────────────────────────

def _build_judge_prompt(state: CorrectionState) -> str:
    sample = state.get("sample_rows") or []
    rows_repr = "\n".join(str(r) for r in sample) or "(행 없음)"
    return (
        "당신은 Text-to-SQL 결과를 검수하는 엄격한 평가자입니다.\n"
        "아래 질문에 대해 생성된 SQL 과 그 실행 결과를 보고, SQL 이 질문에 올바르게"
        " 답하는지 판단하세요.\n\n"
        f"[질문] {state['question']}\n\n"
        f"[SQL]\n{state.get('sql','')}\n\n"
        f"[실행 결과] 총 {state.get('row_count', 0)}행, 표본:\n{rows_repr}\n\n"
        "판단 지침:\n"
        "- 결과가 0행이면 필터/조인/코드값 매핑이 틀렸을 가능성이 높습니다."
        " 단, 집계(count 등)나 실제로 해당 데이터가 없는 경우는 0행도 정답일 수 있습니다.\n"
        "- 질문의 의도(엉뚱한 컬럼·잘못된 집계·누락된 조건)를 점검하세요.\n\n"
        "반드시 아래 JSON 형식으로만 답하세요(설명 금지):\n"
        '{"verdict": "pass" 또는 "revise", "feedback": "수정이 필요하면 구체적 개선 지시, 통과면 빈 문자열"}'
    )


def parse_judge(text: str) -> tuple[str, str]:
    """judge 응답에서 (verdict, feedback) 을 관대하게 파싱한다.

    파싱 실패 시엔 'pass' 로 본다 — 무한 루프 대신 종료를 택한다(시도 한도가
    별도로 보호하지만, 모호한 출력으로 비용을 낭비하지 않게 한다).
    """
    obj: dict[str, Any] | None = None
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                obj = None
    if not isinstance(obj, dict):
        return "pass", ""
    verdict = str(obj.get("verdict", "")).strip().lower()
    feedback = str(obj.get("feedback", "")).strip()
    verdict = "revise" if verdict.startswith("revise") else "pass"
    return verdict, feedback


def _llm_judge(state: CorrectionState, model: str) -> tuple[str, str]:
    text = llm_client.complete(_build_judge_prompt(state), model=model, temperature=0)
    return parse_judge(text)


# ── 조건부 엣지 라우터 (순수 함수 — DB/LLM 불필요, 단위 테스트 대상) ────────

def route_after_judge(state: CorrectionState) -> str:
    """judge 이후 분기: 수정 루프로 돌아갈지(generate) 종료할지(end).

    종료 조건: verdict 가 pass 이거나 시도 한도에 도달했을 때. 한도가 루프를
    유한하게 보장하므로 그래프는 항상 종료한다.
    """
    if _stop_reason(state) == "max_attempts":
        return "end"
    if state.get("verdict") == "revise":
        return "generate"
    return "end"


# ── 그래프 빌드 ─────────────────────────────────────────────────────────────

def build_graph(conn, schema_text: str, judge_model: str):
    """conn(실행용)·schema_text·judge_model 을 묶어 컴파일된 그래프를 만든다.

    schema_text 는 클로저로만 들고 state 에는 넣지 않는다(트레이스 가독성).
    """

    def generate_node(state: CorrectionState) -> dict:
        attempts = state.get("attempts", 0)
        correction = None
        prev_sql = state.get("sql", "")

        # 실행 오류 케이스는 LLM 재호출 전에 규칙 기반 1차 수선으로 빠르게 교정.
        if attempts > 0 and not state.get("exec_ok"):
            repaired = _apply_deterministic_repairs(prev_sql, state.get("exec_error"))
            if repaired.applied and repaired.sql.strip() and repaired.sql.strip() != prev_sql.strip():
                tag_current_run(node="generate", attempt=attempts + 1, repaired="|".join(repaired.applied))
                _graph_log(
                    "generate: attempt=%s repaired=%s sql=%s",
                    attempts + 1,
                    ",".join(repaired.applied),
                    repaired.sql[:140],
                )
                return {"sql": repaired.sql, "attempts": attempts + 1}

        if attempts > 0:  # 재시도: 직전 SQL + 피드백을 프롬프트에 실어 교정 유도
            correction = {
                "prev_sql": prev_sql,
                "feedback": state.get("feedback", ""),
            }
        prompt = main.build_generation_prompt(state["question"], schema_text, correction)
        sql = main._clean_sql(llm_client.complete(prompt, model=main.MODEL, temperature=0))

        # no-op 루프 차단: 재시도 SQL 이 직전과 동일하면 강한 제약으로 한 번 더 재작성.
        if attempts > 0 and sql.strip() == prev_sql.strip():
            force_prompt = (
                f"{prompt}\n\n"
                "추가 규칙: 직전 SQL 을 그대로 반복하면 실패입니다. "
                "반드시 조인 경로/필터/집계 중 최소 1가지를 실질적으로 바꿔 SQL만 출력하세요."
            )
            retried = main._clean_sql(llm_client.complete(force_prompt, model=main.MODEL, temperature=0))
            if retried.strip():
                sql = retried

        tag_current_run(node="generate", attempt=attempts + 1)
        _graph_log("generate: attempt=%s sql=%s", attempts + 1, sql[:140])
        return {"sql": sql, "attempts": attempts + 1}

    def execute_node(state: CorrectionState) -> dict:
        try:
            rows = run_readonly(conn, state["sql"])
            out = {
                "exec_ok": True, "exec_error": None,
                "row_count": len(rows), "sample_rows": rows[:SAMPLE_ROWS],
            }
        except Exception as e:  # noqa: BLE001 — 실패는 피드백으로 모델에 전달된다
            out = {
                "exec_ok": False, "exec_error": str(e),
                "row_count": 0, "sample_rows": [],
            }
        tag_current_run(node="execute", exec_ok=out["exec_ok"])
        _graph_log(
            "execute: ok=%s rows=%s err=%s",
            out["exec_ok"],
            out["row_count"],
            (out["exec_error"] or "")[:140],
        )
        return out

    def judge_node(state: CorrectionState) -> dict:
        if not state.get("exec_ok"):
            verdict = "revise"
            feedback = (
                f"SQL 실행이 실패했습니다. PostgreSQL 오류: {state.get('exec_error')}\n"
                "오류 메시지를 근거로 컬럼명/조인 경로/예약어 따옴표/코드값 매핑을 "
                "점검하고 수정하세요."
            )
        else:
            rb = _rule_based_judge(state)
            if rb is not None:
                verdict, feedback = rb
            else:
                verdict, feedback = _llm_judge(state, judge_model)
            if verdict == "revise" and not feedback.strip():
                feedback = (
                    "결과가 질문 의도와 맞지 않습니다. 필터/조인/집계 대상을 다시 점검하고 "
                    "질문의 핵심 조건을 명시적으로 반영하세요."
                )
        attempt = state.get("attempts")
        record = {
            "attempt": attempt,
            "sql": state.get("sql"),
            "exec_ok": state.get("exec_ok"),
            "exec_error": state.get("exec_error"),
            "row_count": state.get("row_count"),
            "verdict": verdict,
            "feedback": feedback,
        }
        # run 목록에서 바로 보이도록 verdict/attempt 를 judge run 의 태그로 단다.
        tag_current_run(node="judge", verdict=verdict, attempt=attempt)
        _graph_log("judge: attempt=%s verdict=%s feedback=%s", attempt, verdict, feedback)
        return {"verdict": verdict, "feedback": feedback, "history": [record]}

    g = StateGraph(CorrectionState)
    g.add_node("generate", generate_node)
    g.add_node("execute", execute_node)
    g.add_node("judge", judge_node)
    g.add_edge(START, "generate")
    g.add_edge("generate", "execute")
    g.add_edge("execute", "judge")
    g.add_conditional_edges("judge", route_after_judge, {"generate": "generate", "end": END})
    return g.compile()


def trace_config(
    question: str,
    judge_model: str,
    max_attempts: int,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """LangGraph invoke config(run_name/tags/metadata)를 만든다.

    `run()` 이 이 config 를 `app.invoke(..., config=...)` 로 전달해 네이티브
    트레이싱 루트(`self_correction`)를 식별한다. recursion_limit 은 invoke 안전망
    이고, 실제 종료는 attempts 한도가 보장한다.
    """
    item_id = str((metadata or {}).get("item_id", "")).strip()
    tags = ["self-correction", f"judge:{judge_model}"]
    if item_id:
        tags.append(f"item:{item_id}")

    return {
        "recursion_limit": 50,
        "run_name": "self_correction",
        "tags": tags,
        "metadata": {
            "question": question,
            "max_attempts": max_attempts,
            "judge_model": judge_model,
            **(metadata or {}),
        },
    }


def run(
    question: str,
    schema_text: str,
    conn,
    *,
    max_attempts: int | None = None,
    judge_model: str | None = None,
    metadata: dict | None = None,
) -> CorrectionState:
    """self-correction 루프를 끝까지 돌리고 최종 상태를 반환한다.

    최종 SQL 은 state["sql"], 시도 내역은 state["history"] 에 담긴다. 통과하지
    못하고 한도에 도달하면 마지막 수정본(best-effort)을 그대로 반환한다.

    트레이싱: LangGraph 가 네이티브로 트레이싱하도록 invoke config 에 run_name
    (=self_correction)·tags·metadata 를 싣는다. 이때 그래프 루트 아래 generate/
    execute/judge 노드가 자식 run 으로 나오고, 각 노드 안의 LLM 호출(wrap_openai)
    까지 그 밑으로 nesting 된다. 단, 네이티브 트레이싱은 LANGCHAIN_TRACING_V2 를
    봐야 켜지므로 LANGSMITH_TRACING 만 켠 경우를 위해 먼저 동기화한다. 트레이싱
    미설정 시엔 config 키들이 무해하게 무시된다.
    """
    enable_native_tracing_if_langsmith()
    max_attempts = max_attempts or DEFAULT_MAX_ATTEMPTS
    judge_model = judge_model or os.environ.get("T2S_JUDGE_MODEL") or main.MODEL

    merged_metadata = dict(metadata or {})
    item_id = os.environ.get("T2S_EVAL_ITEM_ID")
    if item_id and "item_id" not in merged_metadata:
        merged_metadata["item_id"] = item_id

    app = build_graph(conn, schema_text, judge_model)
    init: CorrectionState = {
        "question": question,
        "attempts": 0,
        "max_attempts": max_attempts,
        "history": [],
    }
    cfg = trace_config(question, judge_model, max_attempts, merged_metadata)

    _graph_log(
        "run:start item_id=%s max_attempts=%s tracing=%s/%s",
        merged_metadata.get("item_id"),
        max_attempts,
        os.environ.get("LANGSMITH_TRACING"),
        os.environ.get("LANGCHAIN_TRACING_V2"),
    )

    # 사용자가 LANGCHAIN_TRACING_V2=false 를 명시한 경우에도 self_correction 루트를
    # 남기기 위한 fallback. (네이티브 노드 트리는 꺼지지만 루트 span 은 확보)
    if _is_truthy("LANGSMITH_TRACING") and not _is_truthy("LANGCHAIN_TRACING_V2"):
        ls_trace = _get_ls_trace()
        if ls_trace is not None:
            with ls_trace(
                name=cfg["run_name"],
                run_type="chain",
                inputs={"question": question, "max_attempts": max_attempts},
                tags=cfg["tags"],
                metadata=cfg["metadata"],
            ) as rt:
                final = app.invoke(init, config=cfg)
                _graph_log(
                    "run:end attempts=%s verdict=%s stop=%s history_len=%s",
                    final.get("attempts"),
                    final.get("verdict"),
                    _stop_reason(final),
                    len(final.get("history") or []),
                )
                try:
                    rt.outputs = _trace_outputs(final)
                except Exception:  # noqa: BLE001
                    pass
                return final

    final = app.invoke(init, config=cfg)
    _graph_log(
        "run:end attempts=%s verdict=%s stop=%s history_len=%s",
        final.get("attempts"),
        final.get("verdict"),
        _stop_reason(final),
        len(final.get("history") or []),
    )
    return final
