"""self-correction 그래프 테스트 — DB/LLM 없이 도는 단위/통합 테스트.

라우터(route_after_judge)와 judge 파서(parse_judge)는 순수 함수라 그대로
검증한다. 루프 전체는 llm_client.complete 와 DB 커넥션을 페이크로 주입해
"실행 오류 → 피드백 → 수정 → 통과" 경로를 실제 LLM/DB 없이 확인한다.
"""
from __future__ import annotations

import pytest

from core import correction_graph as cg


# ── route_after_judge (조건부 엣지) ─────────────────────────────────────────

def test_route_revise_when_verdict_revise_and_budget_left():
    state = {"verdict": "revise", "attempts": 1, "max_attempts": 3}
    assert cg.route_after_judge(state) == "generate"


def test_route_end_when_pass():
    state = {"verdict": "pass", "attempts": 1, "max_attempts": 3}
    assert cg.route_after_judge(state) == "end"


def test_route_end_when_budget_exhausted_even_if_revise():
    # 한도 도달 시엔 revise 여도 종료 — 유한 종료 보장.
    state = {"verdict": "revise", "attempts": 3, "max_attempts": 3}
    assert cg.route_after_judge(state) == "end"


def test_stop_reason_prefers_pass_over_max_attempts():
    # 같은 시점에 둘 다 성립해도 분석 로그는 pass 를 우선 표시한다.
    state = {"verdict": "pass", "attempts": 3, "max_attempts": 3}
    assert cg._stop_reason(state) == "pass"


# ── parse_judge (관대한 JSON 파싱) ──────────────────────────────────────────

def test_parse_judge_clean_json():
    assert cg.parse_judge('{"verdict":"revise","feedback":"조인 누락"}') == ("revise", "조인 누락")


def test_parse_judge_embedded_json():
    text = "다음과 같이 판단합니다: {\"verdict\": \"pass\", \"feedback\": \"\"} 끝"
    assert cg.parse_judge(text) == ("pass", "")


def test_parse_judge_unparseable_defaults_to_pass():
    # 모호한 출력은 종료(pass)로 — 무한 루프/비용 낭비 방지.
    assert cg.parse_judge("음... 잘 모르겠어요") == ("pass", "")


# ── 트레이싱 config (LangSmith 메타) ────────────────────────────────────────

def test_trace_config_has_run_name_tags_and_metadata():
    cfg = cg.trace_config("부자 동네는?", "gpt-4o-mini", 3, metadata={"item_id": "amb-01"})
    assert cfg["run_name"] == "self_correction"
    assert "self-correction" in cfg["tags"]
    assert "judge:gpt-4o-mini" in cfg["tags"]
    assert "item:amb-01" in cfg["tags"]
    assert cfg["metadata"]["question"] == "부자 동네는?"
    assert cfg["metadata"]["max_attempts"] == 3
    assert cfg["metadata"]["item_id"] == "amb-01"  # 호출부 메타 병합
    assert cfg["recursion_limit"] == 50


def test_rule_based_judge_rejects_multi_statement():
    verdict, feedback = cg._rule_based_judge({"question": "남자가 많아요?", "sql": "SELECT 1; SELECT 2"})
    assert verdict == "revise"
    assert "단일 SQL" in feedback


def test_deterministic_repair_transaction_table_name():
    rr = cg._apply_deterministic_repairs(
        'SELECT * FROM public."transaction" t',
        '오류:  "public.transaction" 이름의 릴레이션(relation)이 없습니다',
    )
    assert "public.trans" in rr.sql
    assert "table:transaction->trans" in rr.applied


def test_deterministic_repair_account_client_join():
    rr = cg._apply_deterministic_repairs(
        "SELECT * FROM public.client c JOIN public.account a ON c.client_id = a.client_id",
        '오류:  a.client_id 칼럼 없음',
    )
    assert "JOIN public.disp" in rr.sql
    assert "join:client->disp->account" in rr.applied


# ── 루프 통합 (페이크 LLM + 페이크 DB) ──────────────────────────────────────

class _FakeCursor:
    def __init__(self, script):
        self._script = script
        self._rows: list = []

    def execute(self, sql):
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self._rows = outcome

    def fetchall(self):
        return self._rows


class _FakeConn:
    """execute 시 스크립트대로 행을 돌려주거나 예외를 던지는 가짜 커넥션."""

    def __init__(self, script):
        self._cur = _FakeCursor(list(script))

    def cursor(self):
        return self._cur

    def rollback(self):
        pass


def test_loop_recovers_from_execution_error(monkeypatch):
    # 1차: 실행 오류 → judge 가 결정적으로 revise → 2차: 성공 → judge pass.
    conn = _FakeConn(script=[
        RuntimeError('column "gender" does not exist'),  # 1차 실행 실패
        [(42,)],                                          # 2차 실행 성공(1행)
    ])

    sqls = iter(["SELECT bad", "SELECT good"])
    judge_calls = {"n": 0}

    def fake_complete(prompt, **kwargs):
        # generate 프롬프트엔 [스키마], judge 프롬프트엔 '평가자' 가 들어간다.
        if "평가자" in prompt:
            judge_calls["n"] += 1
            return '{"verdict":"pass","feedback":""}'
        return next(sqls)

    monkeypatch.setattr(cg.llm_client, "complete", fake_complete)

    state = cg.run("질문", "[스키마] ...", conn, max_attempts=3)

    assert state["sql"] == "SELECT good"
    assert state["attempts"] == 2                  # 초안 + 수정 1회
    assert state["history"][0]["exec_ok"] is False  # 1차는 실행 실패
    assert state["history"][0]["verdict"] == "revise"
    assert state["history"][-1]["verdict"] == "pass"
    assert judge_calls["n"] == 1  # 실행 실패한 1차엔 judge LLM 미호출


def test_loop_stops_at_max_attempts(monkeypatch):
    # 매번 실행은 되지만 judge 가 계속 revise → 한도에서 종료.
    conn = _FakeConn(script=[[(0,)]] * 5)

    def fake_complete(prompt, **kwargs):
        if "평가자" in prompt:
            return '{"verdict":"revise","feedback":"다시"}'
        return "SELECT something"

    monkeypatch.setattr(cg.llm_client, "complete", fake_complete)

    state = cg.run("질문", "[스키마]", conn, max_attempts=2)
    assert state["attempts"] == 2
    assert len(state["history"]) == 2
