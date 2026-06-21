import os
import re
from collections import deque

import psycopg2
from dotenv import load_dotenv

from core import llm_client, schema_introspect

# .env 파일의 값을 환경변수로 로드
load_dotenv()

SCHEMA = "public"
MODEL = "gpt-4o-mini"   # 원하는 OpenAI 모델로
_LAST_CORRECTION_STATE: dict | None = None

# 외부 LLM 호출은 core.llm_client 경유 (CLAUDE.md 규약). 모델은 MODEL 로 고정해
# 전달하므로 eval 의 resolve_model_name(main.MODEL) 이 정확히 동작한다.


def get_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "bird"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ["DB_PASSWORD"],
    )


def get_schema_text(conn, schema: str) -> str:
    """카탈로그(컬럼·코멘트·FK)에서 풍부한 스키마 텍스트를 만든다.

    도메인 지식은 DB 에 심긴 코멘트/FK 에서 온다(코드 하드코딩 없음).
    심어두려면 `python -m migrations.apply_schema_metadata` 를 한 번 실행한다.
    """
    return schema_introspect.render_schema_text(schema_introspect.fetch_schema(conn, schema))


# ── Schema linking ───────────────────────────────────────────────────────────
# 질문과 관련된 테이블만 골라(linking) FK 로 연결 테이블을 보강한 뒤, 해당
# 테이블 블록만 프롬프트에 싣는다. 매칭 단서·코드값·FK 는 schema_text(=DB
# 카탈로그) 에서 그대로 가져오므로 main.py 에는 도메인 지식이 없다.

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def _table_terms(table: dict) -> set[str]:
    """테이블을 가리키는 매칭 토큰 — 테이블/컬럼 코멘트와 컬럼명에서 추출."""
    blob = [table.get("comment") or ""]
    for col in table["columns"]:
        blob.append(col["name"])
        if col.get("comment"):
            blob.append(col["comment"])
    return {tok.lower() for tok in _TOKEN.findall(" ".join(blob)) if len(tok) >= 2}


def _score(question: str, terms: set[str]) -> int:
    q = question.lower()
    return sum(1 for term in terms if term in q)


def _fk_graph(schema: dict) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {t: set() for t in schema}
    for tname, table in schema.items():
        for col in table["columns"]:
            fk = col.get("fk")
            if fk and fk[0] in adj:
                adj[tname].add(fk[0])
                adj[fk[0]].add(tname)
    return adj


def _shortest_path(adj: dict[str, set[str]], src: str, dst: str) -> list[str]:
    prev: dict[str, str | None] = {src: None}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        if node == dst:
            break
        for nxt in adj[node]:
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    if dst not in prev:
        return []
    path, cur = [], dst
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    return list(reversed(path))


def _fk_connectors(selected: set[str], schema: dict) -> set[str]:
    """선택된 테이블들을 잇는 FK 경로상의 중간 테이블을 보강 (조인 가능하도록)."""
    adj = _fk_graph(schema)
    sel = list(selected)
    extra: set[str] = set()
    for i in range(len(sel)):
        for j in range(i + 1, len(sel)):
            extra.update(_shortest_path(adj, sel[i], sel[j]))
    return extra - selected


def link_tables(question: str, schema: dict) -> list[str]:
    """질문과 관련된 테이블만 선택하고 FK 연결 테이블을 보강해 반환한다.

    아무 테이블도 매칭되지 않으면 전체 스키마로 폴백한다(정확도 손실 방지).
    """
    selected = {name for name, table in schema.items()
                if _score(question, _table_terms(table)) > 0}
    if not selected:
        return list(schema)
    selected |= _fk_connectors(selected, schema)
    return [t for t in schema if t in selected]  # schema_text 등장 순서 유지


def build_focused_schema(question: str, schema_text: str) -> str:
    """질문 관련 테이블 블록만 추려낸 스키마 텍스트 (코멘트·코드값·FK 포함)."""
    schema = schema_introspect.parse_schema_text(schema_text)
    if not schema:
        return schema_text  # 파싱 실패 시 원본 그대로 사용
    selected = link_tables(question, schema)
    chosen = set(selected)
    focused: dict = {}
    for name in selected:
        table = schema[name]
        cols = []
        for col in table["columns"]:
            # 선택되지 않은 테이블로의 FK 는 숨겨 프롬프트를 깔끔히 유지
            if col.get("fk") and col["fk"][0] not in chosen:
                col = {**col, "fk": None}
            cols.append(col)
        focused[name] = {"comment": table.get("comment"), "columns": cols}
    return schema_introspect.render_schema_text(focused)


def _clean_sql(text: str) -> str:
    """LLM 응답에서 코드펜스/언어태그/후행 세미콜론을 제거해 순수 SQL 만 남긴다."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    return s.removeprefix("sql").strip().rstrip(";").strip()


def build_generation_prompt(
    question: str, schema_text: str, correction: dict | None = None
) -> str:
    """SQL 생성 프롬프트를 만든다.

    correction 이 주어지면(=재시도) 직전 SQL·실행 피드백·judge 피드백을 덧붙여
    "고쳐서 다시 출력하라"는 self-correction 지시를 추가한다. self-correction
    그래프(core.correction_graph)의 generate 노드가 이 경로를 사용한다.
    """
    focused = build_focused_schema(question, schema_text)
    q = question.lower()
    ratio_like = any(tok in q for tok in ("비율", "어느 정도", "퍼센", "%", "율"))
    count_like = ("몇" in q or "몇 개" in q) and not ratio_like
    where_like = any(tok in q for tok in ("어디", "누구", "언제", "어느"))

    output_contract = ["- 한 질문에는 SQL 한 문장만 출력하세요(세미콜론으로 다중문장 금지)."]
    if count_like:
        output_contract.append("- 질문이 '몇/몇 개' 형태면 COUNT 집계로 단일 수치(1행 1열)를 반환하세요.")
    if ratio_like:
        output_contract.append("- 질문이 비율/어느 정도면 분자/분모를 나눈 비율 식을 포함하세요.")
    if where_like:
        output_contract.append("- 질문이 '어디/누구/언제/어느'면 식별 가능한 대상 컬럼(예: 이름/지역/날짜)을 반환하세요.")
    output_contract_text = "\n".join(output_contract)

    prompt = (
        "당신은 PostgreSQL 전문가입니다. 아래는 질문과 관련된 테이블만 추린 스키마입니다.\n"
        "각 컬럼 뒤 '-- 설명 (코드값)' 과 '-> 부모.컬럼'(FK) 을 참고하세요.\n\n"
        f"[스키마]\n{focused}\n\n"
        f"[질문] {question}\n\n"
        "규칙:\n"
        "- SQL 쿼리만 출력하세요. 설명/마크다운/코드펜스 금지.\n"
        f'- 테이블은 {SCHEMA}.table_name 으로 한정하고, 예약어 테이블은 큰따옴표로 감싸세요 (예: {SCHEMA}."order").\n'
        "- 코드값 매핑을 WHERE 절에 정확히 사용하세요.\n"
        "- 여러 테이블이 필요하면 제공된 FK 경로(->)를 따라 조인하세요.\n"
        "출력 계약:\n"
        f"{output_contract_text}"
    )
    if correction:
        prev_sql = correction.get("prev_sql") or ""
        feedback = correction.get("feedback") or ""
        prompt += (
            "\n\n[직전 시도]\n"
            f"{prev_sql}\n\n"
            "[피드백]\n"
            f"{feedback}\n\n"
            "위 피드백을 반영해 문제를 고친 SQL 을 다시 출력하세요. "
            "설명 없이 SQL 만 출력합니다."
        )
    return prompt


def generate_sql(question: str, schema_text: str) -> str:
    """단일샷 SQL 생성 (안정 계약). self-correction 은 generate_sql_corrected 참고."""
    prompt = build_generation_prompt(question, schema_text)
    return _clean_sql(llm_client.complete(prompt, model=MODEL, temperature=0))


def generate_sql_corrected(question: str, schema_text: str) -> str:
    """self-correction 루프로 SQL 을 생성한다 (생성→실행→judge→수정).

    안정 계약 generate_sql 과 동일한 시그니처라 평가의 진입점으로 바로 쓸 수
    있다(T2S_ENTRYPOINT="main:generate_sql_corrected"). 실행 검증을 위해 자체
    DB 연결을 열고 닫는다.
    """
    from core import correction_graph  # 지연 임포트로 순환참조 방지

    conn = get_connection()
    try:
        state = correction_graph.run(question, schema_text, conn=conn)
    finally:
        conn.close()
    global _LAST_CORRECTION_STATE
    _LAST_CORRECTION_STATE = dict(state)
    return state["sql"]


def get_last_correction_state() -> dict | None:
    """가장 최근 self-correction 최종 state (없으면 None)."""
    return _LAST_CORRECTION_STATE


def run_sql(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def main() -> None:
    conn = get_connection()
    schema_text = get_schema_text(conn, SCHEMA)
    q = "How many accounts are there?"
    sql = generate_sql(q, schema_text)
    print("SQL:", sql)
    print("RESULT:", run_sql(conn, sql))


# 관통 테스트 — 스크립트로 직접 실행할 때만 동작 (import 시 부작용 없음)
if __name__ == "__main__":
    main()
