"""
데이터셋 무결성 테스트 — DB/LLM 없이 항상 실행된다.

이 테스트들은 "코드가 바뀌어도" 영향받지 않는다. 평가 데이터셋(JSON)이
깨지지 않았는지, gold SQL 이 안전한 읽기 전용 쿼리인지, 스키마에 존재하는
테이블만 참조하는지를 검증한다.
"""
from __future__ import annotations

import re

import pytest

from eval import runner

# financial_schema.md 기준 화이트리스트 (public 스키마)
KNOWN_TABLES = {
    "district", "account", "client", "disp",
    "card", "loan", "order", "trans",
}
ALLOWED_DIFFICULTY = {"easy", "medium", "hard"}
WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)
HANGUL = re.compile(r"[가-힣]")
# FROM/JOIN 뒤의 테이블 식별자 추출 (선택적 큰따옴표 허용)
TABLE_REF = re.compile(r'\b(?:FROM|JOIN)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?', re.IGNORECASE)
# EXTRACT(YEAR FROM col) 등 함수식 내부의 FROM 은 테이블 참조가 아님 → 제거 후 스캔
FUNC_CALL = re.compile(r"\b\w+\s*\([^()]*\)")


def _table_refs(sql: str) -> set[str]:
    cleaned = FUNC_CALL.sub(" ", sql)
    return {t.lower() for t in TABLE_REF.findall(cleaned)}

ITEMS = runner.load_dataset()


def test_dataset_not_empty():
    assert len(ITEMS) >= 10, "평가 문항이 충분해야 한다"


def test_ids_unique():
    ids = [it["id"] for it in ITEMS]
    assert len(ids) == len(set(ids)), f"중복 id: {ids}"


@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_item_has_required_fields(item):
    for field in ("id", "difficulty", "question", "gold_sql", "tables"):
        assert field in item and item[field], f"{item.get('id')}: '{field}' 누락/빈 값"


@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_difficulty_valid(item):
    assert item["difficulty"] in ALLOWED_DIFFICULTY


@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_question_is_korean(item):
    assert HANGUL.search(item["question"]), f"{item['id']}: 한국어 질문이 아님"


@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_gold_sql_is_select_only(item):
    sql = item["gold_sql"].strip()
    assert re.match(r"(?is)^\s*(SELECT|WITH)\b", sql), f"{item['id']}: SELECT 로 시작해야 함"
    assert not WRITE_KEYWORDS.search(sql), f"{item['id']}: 쓰기성 키워드 포함 금지"


@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_gold_sql_references_known_tables(item):
    refs = _table_refs(item["gold_sql"])
    assert refs, f"{item['id']}: 참조 테이블을 찾을 수 없음"
    unknown = refs - KNOWN_TABLES
    assert not unknown, f"{item['id']}: 알 수 없는 테이블 {unknown}"


@pytest.mark.parametrize("item", ITEMS, ids=[it["id"] for it in ITEMS])
def test_declared_tables_are_known(item):
    unknown = {t.lower() for t in item["tables"]} - KNOWN_TABLES
    assert not unknown, f"{item['id']}: tables 필드에 알 수 없는 테이블 {unknown}"


def test_difficulty_coverage():
    seen = {it["difficulty"] for it in ITEMS}
    assert seen == ALLOWED_DIFFICULTY, f"난이도 전 구간 포함 필요, 현재: {seen}"
