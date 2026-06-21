# 한국어 Text-to-SQL 평가

`main.py` 의 Text-to-SQL 성능을 한국어 질문으로 측정하는 평가 세트.

## 설계 원칙 — 코드가 바뀌어도 테스트는 그대로

테스트는 구현 내부가 아니라 **안정 계약**에만 의존한다.

| 계약 | 내용 |
|------|------|
| 생성 함수 | `generate_sql(question: str, schema_text: str) -> str` |
| 진입점 | 기본 `main:generate_sql` — `T2S_ENTRYPOINT="모듈:함수"` 로 교체 |
| 지표 | execution accuracy (정답 SQL 결과 == 생성 SQL 결과) |

`main.py` 내부 프롬프트/모델/파이프라인을 바꿔도, 위 함수 시그니처만 유지하면
`tests/`, `eval/` 코드는 수정할 필요가 없다. 구조가 바뀌면 `T2S_ENTRYPOINT`
환경변수만 조정한다.

## 구성

| 파일 | 설명 |
|------|------|
| `eval/dataset/financial_ko.json` | 한국어 질문 + 정답 SQL (난이도 easy/medium/hard) |
| `eval/runner.py` | DB 연결·스키마 추출·진입점 해석·결과 비교 (안정 인프라) |
| `eval/evaluate.py` | 정확도 리포트 CLI |
| `tests/test_dataset.py` | 데이터셋 무결성 (DB/LLM 불필요, 항상 실행) |
| `tests/test_execution_accuracy.py` | 실행 정확도 통합 테스트 (조건부 실행) |

## 실행

> 의존성은 uv 로 관리한다. 최초 1회: `uv sync` (사내 SSL 환경은 자동으로
> `native-tls` 적용 — `pyproject.toml` 의 `[tool.uv]` 참고).

### 1) 데이터셋 무결성 테스트 (DB/LLM 불필요)

```powershell
uv run pytest tests/test_dataset.py -v
```

### 2) 실행 정확도 통합 테스트 (DB + LLM 필요)

`.env` 에 `DB_*`, `OPENAI_API_KEY` 를 채우고 `bird` DB 가 적재된 상태에서:

```powershell
$env:RUN_EVAL = "1"
uv run pytest tests/test_execution_accuracy.py -v
```

조건(`RUN_EVAL=1` + DB 연결 + 진입점 임포트)이 안 맞으면 자동 skip 된다.

회귀 가드로 최소 정확도를 강제하려면:

```powershell
$env:RUN_EVAL = "1"; $env:T2S_MIN_ACCURACY = "0.7"
uv run pytest tests/test_execution_accuracy.py::test_overall_accuracy_threshold -v
```

### 3) 정확도 리포트 (CLI)

```powershell
uv run python -m eval.evaluate              # 전체
uv run python -m eval.evaluate --difficulty easy
uv run python -m eval.evaluate --limit 5
```

## 데이터셋 확장

`eval/dataset/financial_ko.json` 의 `items` 에 항목을 추가한다.

```json
{ "id": "medium-08", "difficulty": "medium",
  "question": "한국어 질문",
  "gold_sql": "SELECT ...;",
  "tables": ["loan"] }
```

`tables` 화이트리스트(`district, account, client, disp, card, loan, order, trans`)
밖의 테이블을 쓰면 `test_dataset.py` 가 잡아낸다. 예약어 테이블 `order` 는
SQL 에서 `"order"` 로 따옴표 처리한다.
