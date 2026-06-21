# BIRD `financial` 데이터베이스 스키마 (Baseline 테스트용)

> Text-to-SQL baseline 테스트(`main.py`) 대상 스키마 정리 문서.
> BIRD 의 `financial` 데이터베이스에 해당하는 테이블만 추렸습니다.

## 접속 / 환경 정보

| 항목 | 값 |
|------|----|
| DBMS | PostgreSQL (localhost) |
| Database | `bird` |
| 실제 스키마 | `public` (모든 BIRD DB 가 `public` 한 곳에 통합 적재됨) |
| 사용 테이블 | 아래 8개 (financial DB) |

> ⚠️ **주의:** `main.py` 의 `SCHEMA = "financial"` 설정과 달리 실제 DB 에는
> `financial` 스키마가 없고 모든 테이블이 `public` 에 있습니다.
> Baseline 을 돌리려면 `SCHEMA = "public"` 으로 바꾸거나, 스키마 한정 없이
> 테이블명만 사용해야 합니다.

## 테이블 개요

| 테이블 | 설명 | 행 수 | PK |
|--------|------|------:|----|
| `district` | 지점(지역) 정보 — 인구/급여/실업률/범죄 통계 | 77 | `district_id` |
| `account` | 계좌 | 4,500 | `account_id` |
| `client` | 고객 | 5,369 | `client_id` |
| `disp` | 권한(disposition) — 고객·계좌 연결 | 5,369 | `disp_id` |
| `card` | 신용카드 | 892 | `card_id` |
| `loan` | 대출 | 682 | `loan_id` |
| `order` | 영구 이체(자동이체) 지시 | 6,471 | `order_id` |
| `trans` | 거래 내역 (대용량) | 1,056,320 | `trans_id` |

## 관계도 (FK)

```
district (district_id)
   ▲                ▲
   │ district_id    │ district_id
 account          client
   ▲                  ▲
   │ account_id       │ client_id
   ├──────── disp ────┤
   │          ▲
   │          │ disp_id
   │        card
   ├── loan  (account_id)
   ├── order (account_id)
   └── trans (account_id)
```

| 자식 테이블.컬럼 | → 부모 테이블.컬럼 |
|------------------|--------------------|
| `account.district_id` | `district.district_id` |
| `client.district_id` | `district.district_id` |
| `disp.account_id` | `account.account_id` |
| `disp.client_id` | `client.client_id` |
| `card.disp_id` | `disp.disp_id` |
| `loan.account_id` | `account.account_id` |
| `order.account_id` | `account.account_id` |
| `trans.account_id` | `account.account_id` |

---

## 테이블 상세

### `district` — 지점/지역 통계
컬럼명이 `a2`~`a16` 으로 모호하므로 의미를 함께 표기.

| 컬럼 | 타입 | 의미 |
|------|------|------|
| `district_id` (PK) | bigint | 지점 위치 ID |
| `a2` | text | 지역명 (district name) |
| `a3` | text | 광역권(region) |
| `a4` | text | 총 인구수 |
| `a5` | text | 인구 <499 인 지자체 수 |
| `a6` | text | 인구 500–1999 지자체 수 |
| `a7` | text | 인구 2000–9999 지자체 수 |
| `a8` | bigint | 인구 >10000 지자체 수 |
| `a9` | bigint | (사용 안 함 / not useful) |
| `a10` | real | 도시 거주 인구 비율 |
| `a11` | bigint | 평균 급여 |
| `a12` | real | 실업률 1995 |
| `a13` | real | 실업률 1996 |
| `a14` | bigint | 인구 1000명당 사업자 수 |
| `a15` | bigint | 범죄 건수 1995 |
| `a16` | bigint | 범죄 건수 1996 |

### `account` — 계좌
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `account_id` (PK) | bigint | 계좌 ID |
| `district_id` (FK→district) | bigint | 지점 위치 |
| `frequency` | text | 명세서 발급 주기: `POPLATEK MESICNE`=월간, `POPLATEK TYDNE`=주간, `POPLATEK PO OBRATU`=거래 시 |
| `date` | date | 계좌 개설일 |

### `client` — 고객
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `client_id` (PK) | bigint | 고객 ID |
| `gender` | text | 성별: `F`=여성, `M`=남성 |
| `birth_date` | date | 생년월일 |
| `district_id` (FK→district) | bigint | 지점 위치 |

### `disp` — 권한(disposition)
고객과 계좌를 연결하는 다대다 연결 테이블.

| 컬럼 | 타입 | 의미 |
|------|------|------|
| `disp_id` (PK) | bigint | 권한 레코드 ID |
| `client_id` (FK→client) | bigint | 고객 ID |
| `account_id` (FK→account) | bigint | 계좌 ID |
| `type` | text | 권한 유형: `OWNER` / `USER` / `DISPONENT` (영구이체·대출 신청 권한은 OWNER 만) |

### `card` — 신용카드
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `card_id` (PK) | bigint | 카드 ID |
| `disp_id` (FK→disp) | bigint | 권한 ID |
| `type` | text | 카드 등급: `junior` / `classic` / `gold` |
| `issued` | date | 발급일 |

### `loan` — 대출
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `loan_id` (PK) | bigint | 대출 ID |
| `account_id` (FK→account) | bigint | 계좌 ID |
| `date` | date | 대출 승인일 |
| `amount` | bigint | 승인 금액 (USD) |
| `duration` | bigint | 대출 기간 (개월) |
| `payments` | real | 월 상환액 |
| `status` | text | 상환 상태: `A`=완료/정상, `B`=완료/미상환, `C`=진행중/정상, `D`=진행중/연체 |

### `order` — 영구 이체(자동이체) 지시
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `order_id` (PK) | bigint | 이체 지시 ID |
| `account_id` (FK→account) | bigint | 출금 계좌 ID |
| `bank_to` | text | 수취 은행 (2자리 코드) |
| `account_to` | bigint | 수취 계좌 |
| `amount` | real | 이체 금액 |
| `k_symbol` | text | 목적: `POJISTNE`=보험, `SIPO`=공과금, `LEASING`=리스, `UVER`=대출상환 |

### `trans` — 거래 내역 (대용량, 약 105만 행)
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `trans_id` (PK) | bigint | 거래 ID |
| `account_id` (FK→account) | bigint | 계좌 ID |
| `date` | date | 거래일 |
| `type` | text | 입출 구분: `PRIJEM`=입금, `VYDAJ`=출금 |
| `operation` | text | 거래 수단: `VYBER KARTOU`=카드출금, `VKLAD`=현금입금, `PREVOD Z UCTU`=타행수금, `VYBER`=현금출금, `PREVOD NA UCET`=타행송금 |
| `amount` | bigint | 금액 (USD) |
| `balance` | bigint | 거래 후 잔액 (USD) |
| `k_symbol` | text | 거래 성격: `POJISTNE`=보험, `SLUZBY`=명세서료, `UROK`=이자, `SANKC. UROK`=연체이자, `SIPO`=공과금, `DUCHOD`=연금, `UVER`=대출상환 |
| `bank` | text | 상대 은행 (2자리 코드) |
| `account` | bigint | 상대 계좌 |

---

## Baseline 쿼리 작성 시 참고

- **고객 → 계좌** 연결은 항상 `disp` 를 경유: `client → disp → account`.
- **카드 소유 고객**: `card → disp → client`.
- **지역 통계 질문**: `account`/`client` 의 `district_id` 로 `district` 조인.
- 대량 집계(`trans`)는 행이 100만+ 이므로 `account_id`/`date` 조건을 먼저 거는 것이 유리.
- 코드성 컬럼(`frequency`, `type`, `status`, `k_symbol`, `operation`)은 위 값 매핑을 그대로 SQL `WHERE` 에 사용.
