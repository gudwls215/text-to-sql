"""DB 카탈로그 기반 스키마 추출 (공유 인프라).

`main.py`(SUT)와 `eval/runner.py`(평가 계층)가 모두 이 모듈을 통해 동일한
형식의 풍부한 `schema_text` 를 얻는다. 도메인 지식(컬럼 의미·코드값·FK)은
코드에 하드코딩하지 않고 **DB 카탈로그**에서 읽어온다:

  - 컬럼 의미/코드값 → `pg_description`(COMMENT ON TABLE/COLUMN)
  - 외래키 관계      → `information_schema` 제약 카탈로그

코멘트/FK 는 `migrations/apply_schema_metadata.py` 가 `schema_metadata.json`
을 근거로 DB 에 심어둔다. 마이그레이션을 돌리지 않은 DB 에서도 컬럼/타입만으로
동작하도록 모든 보강 정보는 선택적이다.

스키마 텍스트 형식(렌더/파싱 왕복 가능):

    TABLE client -- 고객
      client_id bigint
      gender text -- 성별 (F=여성, M=남성)
      district_id bigint -> district.district_id -- 지점 위치
"""
from __future__ import annotations

import re
from typing import Any

# {table: {"comment": str|None, "columns": [{"name","type","comment","fk"}]}}
Schema = dict[str, dict[str, Any]]


def fetch_schema(conn, schema: str = "public") -> Schema:
    """카탈로그에서 컬럼·타입·코멘트·FK 를 읽어 구조화한다."""
    cur = conn.cursor()

    cur.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    columns = cur.fetchall()

    cur.execute(
        """
        SELECT c.relname, obj_description(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r'
        """,
        (schema,),
    )
    table_comment = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        """
        SELECT c.relname, a.attname, col_description(c.oid, a.attnum)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = %s AND c.relkind = 'r'
        """,
        (schema,),
    )
    col_comment = {(row[0], row[1]): row[2] for row in cur.fetchall()}

    cur.execute(
        """
        SELECT kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.constraint_schema = kcu.constraint_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
         AND tc.constraint_schema = ccu.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
        """,
        (schema,),
    )
    fks = {(row[0], row[1]): (row[2], row[3]) for row in cur.fetchall()}

    tables: Schema = {}
    for tname, cname, dtype in columns:
        table = tables.setdefault(
            tname, {"comment": table_comment.get(tname), "columns": []}
        )
        table["columns"].append(
            {
                "name": cname,
                "type": dtype,
                "comment": col_comment.get((tname, cname)),
                "fk": fks.get((tname, cname)),
            }
        )
    return tables


def render_schema_text(tables: Schema) -> str:
    """구조화된 스키마를 `schema_text` 문자열로 직렬화한다."""
    lines: list[str] = []
    for tname, table in tables.items():
        head = f"TABLE {tname}"
        if table.get("comment"):
            head += f" -- {table['comment']}"
        lines.append(head)
        for col in table["columns"]:
            seg = f"  {col['name']} {col['type']}"
            if col.get("fk"):
                seg += f" -> {col['fk'][0]}.{col['fk'][1]}"
            if col.get("comment"):
                seg += f" -- {col['comment']}"
            lines.append(seg)
    return "\n".join(lines)


_HEAD = re.compile(r'^TABLE\s+"?(\w+)"?(?:\s+--\s+(.*))?$')


def parse_schema_text(text: str) -> Schema:
    """`schema_text` 문자열을 구조화된 스키마로 되돌린다 (render 의 역연산)."""
    tables: Schema = {}
    current: str | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            m = _HEAD.match(raw.strip())
            if not m:
                current = None
                continue
            current = m.group(1)
            tables[current] = {"comment": m.group(2), "columns": []}
        elif current is not None:
            line = raw.strip()
            comment = None
            if " -- " in line:
                line, comment = line.split(" -- ", 1)
            fk = None
            if " -> " in line:
                line, ref = line.split(" -> ", 1)
                parent, _, pcol = ref.strip().partition(".")
                fk = (parent, pcol)
            parts = line.split()
            if not parts:
                continue
            tables[current]["columns"].append(
                {
                    "name": parts[0],
                    "type": " ".join(parts[1:]),
                    "comment": comment,
                    "fk": fk,
                }
            )
    return tables
