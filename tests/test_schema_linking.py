"""Schema linking + 스키마 추출/마이그레이션 단위 테스트 — DB/LLM 없이 항상 실행.

generate_sql 정확도 개선의 핵심(관련 테이블 선택 + FK 보강 + 코드값 주입)과
schema_text 렌더/파싱 왕복, 마이그레이션 SQL 생성을 순수 함수 수준에서 검증한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import main
from core import schema_introspect
from migrations import apply_schema_metadata as migration

# core.schema_introspect.render_schema_text 가 만들어내는 것과 동일한 형식.
# (DB 코멘트/FK 가 심긴 상태를 가정한 풍부한 schema_text)
SCHEMA_TEXT = """\
TABLE account -- 계좌
  account_id bigint
  district_id bigint -> district.district_id -- 지점 위치
  frequency text -- 명세서 발급 주기 (POPLATEK MESICNE=월간, POPLATEK TYDNE=주간)
  date date -- 계좌 개설일
TABLE card -- 신용카드
  card_id bigint
  disp_id bigint -> disp.disp_id -- 권한 ID
  type text -- 카드 등급 (junior, classic, gold)
  issued date -- 발급일
TABLE client -- 고객
  client_id bigint
  gender text -- 성별 (F=여성, M=남성)
  birth_date date -- 생년월일
  district_id bigint -> district.district_id -- 지점 위치
TABLE disp -- 권한(고객-계좌 연결)
  disp_id bigint
  client_id bigint -> client.client_id -- 고객 ID
  account_id bigint -> account.account_id -- 계좌 ID
  type text -- 권한 유형 (OWNER=소유자, USER=사용자, DISPONENT)
TABLE district -- 지점/지역 정보
  district_id bigint
  a2 text -- 지역명
  a3 text -- 광역권
  a11 bigint -- 평균 급여
TABLE loan -- 대출
  loan_id bigint
  account_id bigint -> account.account_id -- 계좌 ID
  amount bigint -- 승인 금액
  duration bigint -- 대출 기간 (개월)
  payments real -- 월 상환액
  status text -- 상환 상태 (A=완료/정상, D=진행중/연체)
TABLE order -- 자동이체 지시
  order_id bigint
  account_id bigint -> account.account_id -- 출금 계좌 ID
  k_symbol text -- 목적 (POJISTNE=보험, SIPO=공과금)
  amount real -- 이체 금액
TABLE trans -- 거래 내역
  trans_id bigint
  account_id bigint -> account.account_id -- 계좌 ID
  type text -- 입출 구분 (PRIJEM=입금, VYDAJ=출금)
  amount bigint -- 금액
"""


# ── core.schema_introspect: 렌더/파싱 왕복 ──────────────────────────────────

def test_parse_extracts_comment_type_and_fk():
    schema = schema_introspect.parse_schema_text(SCHEMA_TEXT)
    assert set(schema) == {
        "account", "card", "client", "disp", "district", "loan", "order", "trans",
    }
    account = schema["account"]
    assert account["comment"] == "계좌"
    district_id = account["columns"][1]
    assert district_id["name"] == "district_id"
    assert district_id["type"] == "bigint"
    assert district_id["fk"] == ("district", "district_id")
    assert district_id["comment"] == "지점 위치"


def test_render_parse_round_trip():
    schema = schema_introspect.parse_schema_text(SCHEMA_TEXT)
    assert schema_introspect.parse_schema_text(
        schema_introspect.render_schema_text(schema)
    ) == schema


# ── main: schema linking ────────────────────────────────────────────────────

@pytest.mark.parametrize("question, expected_subset", [
    ("전체 계좌는 몇 개인가요?", {"account"}),
    ("여성(F) 고객은 몇 명인가요?", {"client"}),
    ("골드(gold) 등급 신용카드는 몇 장 발급되었나요?", {"card"}),
    ("연체(status 가 D) 상태인 대출은 몇 건인가요?", {"loan"}),
    ("보험(POJISTNE) 목적의 자동이체 지시는 몇 건인가요?", {"order"}),
    ("출금(VYDAJ) 거래 중 가장 큰 금액은 얼마인가요?", {"trans"}),
    ("평균 급여(a11)가 가장 높은 지역의 지역명(a2)은 무엇인가요?", {"district"}),
])
def test_link_selects_relevant_tables(question, expected_subset):
    schema = schema_introspect.parse_schema_text(SCHEMA_TEXT)
    selected = set(main.link_tables(question, schema))
    assert expected_subset <= selected, f"{question}: 기대 테이블 누락, 선택={selected}"


def test_link_adds_fk_connector_for_join():
    # gold 카드 → 고객: card 와 client 사이의 연결 테이블 disp 가 보강돼야 한다.
    schema = schema_introspect.parse_schema_text(SCHEMA_TEXT)
    selected = set(main.link_tables("gold 카드를 소유한 고객은 몇 명인가요?", schema))
    assert {"card", "disp", "client"} <= selected


def test_link_falls_back_to_full_schema_when_no_match():
    schema = schema_introspect.parse_schema_text(SCHEMA_TEXT)
    selected = main.link_tables("zzz qqq 매칭없음", schema)
    assert set(selected) == set(schema)


def test_focused_schema_injects_values_and_joins():
    focused = main.build_focused_schema("gold 카드를 소유한 고객은 몇 명인가요?", SCHEMA_TEXT)
    assert "card" in focused and "disp" in focused and "client" in focused
    assert "gold" in focused                            # 코드값 매핑 주입
    assert "disp_id bigint -> disp.disp_id" in focused  # FK 조인 경로 주입
    assert "trans" not in focused                       # 관련 없는 테이블 제외


def test_focused_schema_explains_cryptic_district_columns():
    focused = main.build_focused_schema(
        "평균 급여(a11)가 가장 높은 지역의 지역명(a2)은 무엇인가요?", SCHEMA_TEXT,
    )
    assert "a2 text -- 지역명" in focused
    assert "평균 급여" in focused


def test_focused_schema_hides_fk_to_unselected_table():
    # district 만 선택되면 다른 테이블로의 FK 화살표는 노출되지 않아야 한다.
    focused = main.build_focused_schema("지역명(a2)은?", SCHEMA_TEXT)
    assert "->" not in focused


@pytest.mark.parametrize("raw, expected", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),
    ("SELECT count(*) FROM account;", "SELECT count(*) FROM account"),
    ("sql SELECT 1", "SELECT 1"),
])
def test_clean_sql_strips_fences_and_tags(raw, expected):
    assert main._clean_sql(raw) == expected


# ── migrations: SQL 생성 (DB 없이 순수 검증) ────────────────────────────────

def _load_meta() -> dict:
    path = Path(__file__).resolve().parent.parent / "schema_metadata.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_migration_generates_comment_pk_fk():
    stmts = migration.build_statements(_load_meta())
    sqls = [sql for _kind, _name, sql in stmts]
    kinds = {kind for kind, _name, _sql in stmts}
    assert kinds == {"comment", "pk", "fk"}
    # 코드값이 코멘트에 포함된다
    assert any('COMMENT ON COLUMN "public"."client"."gender"' in s and "F=여성" in s
               for s in sqls)
    # 예약어 테이블은 인용된다
    assert any('"public"."order"' in s for s in sqls)
    # FK 는 NOT VALID 로 안전하게 생성
    assert any("FOREIGN KEY" in s and "NOT VALID" in s for s in sqls)
    # PK 가 FK 대상으로 추가된다
    assert any("PRIMARY KEY" in s for s in sqls)


def test_migration_constraints_are_named_for_idempotency():
    stmts = migration.build_statements(_load_meta())
    names = [name for kind, name, _sql in stmts if kind in ("pk", "fk")]
    assert all(names), "PK/FK 제약에는 멱등성을 위한 이름이 있어야 한다"
    assert len(names) == len(set(names)), "제약 이름은 유일해야 한다"
