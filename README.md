# text-to-sql

BIRD 데이터셋 기반 Text-to-SQL baseline 테스트.

자연어 질문을 OpenAI 모델로 SQL 로 변환한 뒤 PostgreSQL(`bird` DB)에서 실행한다.

## 구성

| 파일 | 설명 |
|------|------|
| `main.py` | baseline 실행 스크립트 (스키마 추출 → SQL 생성 → 실행) |
| `financial_schema.md` | 테스트 대상 `financial` DB 스키마 정리 문서 |
| `.env.example` | 환경변수 템플릿 |
| `minidev/` | BIRD mini-dev 데이터셋 (git 미포함) |

## 사전 준비

1. 의존성 설치
   ```bash
   pip install openai psycopg2-binary python-dotenv
   ```
2. 환경변수 설정 — `.env.example` 을 `.env` 로 복사 후 키 입력
   ```powershell
   Copy-Item .env.example .env
   # .env 안의 OPENAI_API_KEY 값을 채운다
   ```
3. PostgreSQL `bird` 데이터베이스가 localhost 에 적재되어 있어야 함.

## 실행

```bash
python main.py
```

## 참고

- DB 접속 정보(host/dbname/user/password)는 현재 `main.py` 에 하드코딩되어 있다.
  운영 시에는 `.env` 로 분리 권장.
- 스키마 관련 주의사항은 `financial_schema.md` 참고
  (실제 테이블은 `public` 스키마에 적재됨).
