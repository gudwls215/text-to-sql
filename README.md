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
| `eval/` | 한국어 Text-to-SQL 평가 세트 (데이터셋 + 러너) |
| `tests/` | 데이터셋 무결성 + 실행 정확도 테스트 |

## 사전 준비

1. 의존성 설치 (uv 사용)
   ```bash
   uv sync
   ```
   > 사내/프록시 SSL 검사 환경에서는 `native-tls` 가 필요하며, `pyproject.toml`
   > 의 `[tool.uv] native-tls = true` 로 자동 적용된다.

   새 의존성 추가 시:
   ```bash
   uv add <패키지>          # 런타임
   uv add --dev <패키지>    # 개발/테스트용
   ```
2. 환경변수 설정 — `.env.example` 을 `.env` 로 복사 후 키 입력
   ```powershell
   Copy-Item .env.example .env
   # .env 안의 OPENAI_API_KEY 값을 채운다
   ```
3. PostgreSQL `bird` 데이터베이스가 localhost 에 적재되어 있어야 함.

## 실행

```bash
uv run python main.py
```

## 테스트 / 평가

```powershell
uv run pytest tests/test_dataset.py -v     # 데이터셋 무결성 (DB/LLM 불필요)
$env:RUN_EVAL = "1"; uv run pytest          # 실행 정확도 통합 테스트 포함
uv run python -m eval.evaluate              # 한국어 성능 리포트
```
자세한 내용은 `eval/README.md` 참고.

## 참고

- DB 접속 정보(host/dbname/user/password)는 현재 `main.py` 에 하드코딩되어 있다.
  운영 시에는 `.env` 로 분리 권장.
- 스키마 관련 주의사항은 `financial_schema.md` 참고
  (실제 테이블은 `public` 스키마에 적재됨).
