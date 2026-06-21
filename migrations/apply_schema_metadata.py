"""schema_metadata.json 을 DB 에 적용하는 마이그레이션.

`COMMENT ON TABLE/COLUMN`(코드값 매핑 포함), `PRIMARY KEY`, `FOREIGN KEY`
(NOT VALID — 기존 데이터 검증을 건너뛰어 안전하게 생성)를 심는다. 이렇게
심어둔 메타데이터를 런타임의 `core.schema_introspect.fetch_schema` 가 카탈로그
에서 다시 읽어 LLM 프롬프트로 전달한다.

멱등(idempotent): 제약은 이름을 부여해 이미 있으면 건너뛰고, 코멘트는
덮어쓴다. 일부 제약 추가가 실패(예: 더티 데이터)해도 코멘트 적용은 계속된다.

실행:
    python -m migrations.apply_schema_metadata            # 실제 적용
    python -m migrations.apply_schema_metadata --dry-run  # 실행할 SQL 만 출력
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
META_PATH = REPO_ROOT / "schema_metadata.json"


def _q(ident: str) -> str:
    """식별자 인용 (예약어 테이블 "order" 등 안전 처리)."""
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _render_values(values: Any) -> str:
    if isinstance(values, dict):
        return ", ".join(f"{k}={v}" if v else f"{k}" for k, v in values.items())
    return ", ".join(str(v) for v in values)


def _column_comment(col_meta: dict[str, Any]) -> str:
    """기본 설명에 코드값 매핑을 괄호로 덧붙인 코멘트 문자열."""
    base = col_meta.get("comment", "") or ""
    if col_meta.get("values"):
        rendered = _render_values(col_meta["values"])
        base = f"{base} ({rendered})" if base else f"({rendered})"
    return base


def build_statements(meta: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """메타데이터로부터 (종류, 제약이름, SQL) 목록을 만든다 (순수 함수, 테스트 가능)."""
    schema = meta.get("schema", "public")
    stmts: list[tuple[str, str | None, str]] = []

    # 1) 코멘트 (멱등 — 덮어씀)
    for tname, table in meta["tables"].items():
        qt = f"{_q(schema)}.{_q(tname)}"
        if table.get("comment"):
            stmts.append(("comment", None, f"COMMENT ON TABLE {qt} IS {_lit(table['comment'])}"))
        for cname, col in table["columns"].items():
            text = _column_comment(col)
            if text:
                stmts.append(
                    ("comment", None, f"COMMENT ON COLUMN {qt}.{_q(cname)} IS {_lit(text)}")
                )

    # 2) 기본키 (FK 대상이 되려면 필요)
    for tname, table in meta["tables"].items():
        pk = table.get("primary_key")
        if pk:
            qt = f"{_q(schema)}.{_q(tname)}"
            name = f"pk_{tname}"
            cols = ", ".join(_q(c) for c in pk)
            stmts.append(("pk", name, f"ALTER TABLE {qt} ADD CONSTRAINT {_q(name)} PRIMARY KEY ({cols})"))

    # 3) 외래키 (NOT VALID — 기존 행 검증 생략)
    for tname, table in meta["tables"].items():
        qt = f"{_q(schema)}.{_q(tname)}"
        for cname, col in table["columns"].items():
            if not col.get("fk"):
                continue
            parent, _, pcol = str(col["fk"]).partition(".")
            ref = f"{_q(schema)}.{_q(parent)}"
            name = f"fk_{tname}_{cname}"
            stmts.append(
                (
                    "fk",
                    name,
                    f"ALTER TABLE {qt} ADD CONSTRAINT {_q(name)} "
                    f"FOREIGN KEY ({_q(cname)}) REFERENCES {ref} ({_q(pcol)}) NOT VALID",
                )
            )
    return stmts


def _existing_constraints(cur, schema: str) -> set[str]:
    cur.execute(
        """
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_namespace n ON n.oid = con.connamespace
        WHERE n.nspname = %s
        """,
        (schema,),
    )
    return {row[0] for row in cur.fetchall()}


def apply(conn, statements: list[tuple[str, str | None, str]], schema: str) -> tuple[int, int, int]:
    """SQL 목록을 DB 에 적용한다. (적용, 건너뜀, 실패) 카운트를 반환."""
    conn.autocommit = True
    cur = conn.cursor()
    existing = _existing_constraints(cur, schema)
    applied = skipped = failed = 0
    for kind, name, sql in statements:
        if name and name in existing:
            skipped += 1
            continue
        try:
            cur.execute(sql)
            applied += 1
        except Exception as e:  # noqa: BLE001 — 한 문장 실패가 전체를 막지 않게 한다
            failed += 1
            print(f"  ! [{kind}] 건너뜀: {sql.split('ADD CONSTRAINT')[0].strip()[:60]}… ({e})",
                  file=sys.stderr)
    return applied, skipped, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="schema_metadata.json 을 DB 에 적용")
    parser.add_argument("--dry-run", action="store_true", help="실행할 SQL 만 출력")
    args = parser.parse_args()

    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    schema = meta.get("schema", "public")
    statements = build_statements(meta)

    if args.dry_run:
        for _kind, _name, sql in statements:
            print(sql + ";")
        print(f"\n-- 총 {len(statements)}개 문장", file=sys.stderr)
        return 0

    from main import get_connection  # 지연 임포트 (DB 연결은 실제 적용 시에만)

    conn = get_connection()
    try:
        applied, skipped, failed = apply(conn, statements, schema)
    finally:
        conn.close()
    print(f"마이그레이션 완료 — 적용 {applied}, 건너뜀 {skipped}, 실패 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
