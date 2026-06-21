import os
import psycopg2
from openai import OpenAI
from dotenv import load_dotenv

# .env 파일의 값을 환경변수로 로드
load_dotenv()

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ.get("DB_NAME", "bird"),
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ["DB_PASSWORD"],
)
SCHEMA = "public"

# OpenAI API 사용: .env 의 OPENAI_API_KEY 에서 키를 읽는다.
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MODEL = "gpt-4o-mini"   # 원하는 OpenAI 모델로

def get_schema_text(conn, schema):
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
    """, (schema,))
    tables = {}
    for t, c, dt in cur.fetchall():
        tables.setdefault(t, []).append(f"{c} {dt}")
    return "\n".join(f"TABLE {t} ({', '.join(cols)})"
                     for t, cols in tables.items())

def generate_sql(question, schema_text):
    prompt = (f"PostgreSQL schema:\n{schema_text}\n\n"
              f"Question: {question}\n"
              f"Return ONLY a SQL query, no explanation. "
              f"Use schema-qualified names like {SCHEMA}.table_name.")
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}])
    return resp.choices[0].message.content.strip().strip("`").removeprefix("sql").strip()

def run_sql(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()

# 관통 테스트
schema_text = get_schema_text(conn, SCHEMA)
q = "How many accounts are there?"
sql = generate_sql(q, schema_text)
print("SQL:", sql)
print("RESULT:", run_sql(conn, sql))
