# Test Report

Tested at: 2026-07-08 21:50 KST

## 1. API Connection Test

- 한강홍수통제소 API 연결 여부: `missing_api_key`
- K-water API 연결 여부: `missing_api_key`
- 응답 데이터 저장 여부: 키 없음 상태도 `backend/logs/api_snapshots/`에 오류 스냅샷으로 저장됨
- 실패 시 에러 처리 여부: 통과

Current result:

```text
HRFCO_API_KEY가 없어 실제 데이터를 불러오지 않았습니다. 임의 데이터로 대체하지 않습니다.
KWATER_API_KEY가 없어 실제 데이터를 불러오지 않았습니다. 임의 데이터로 대체하지 않습니다.
```

API 키를 입력한 뒤 `python -m backend.scripts.run_api_connection_test`를 다시 실행하면 실제 연결 결과와 원문 스냅샷이 갱신된다.

## 2. Official Event Replay Test

- 오송 사고 공식 타임라인 로딩 여부: 통과
- 각 이벤트 `source_url` 표시 여부: 통과
- `confirmed_levee_collapse` 이벤트가 공식자료에서만 표시되는지 여부: 통과

Executed:

```text
python -m pytest tests/test_disaster_system.py -q
9 passed
```

## 3. No Hardcoding Check

- 임의 강수량 없음: 통과
- 임의 수위 없음: 통과
- 임의 유량 없음: 통과
- 임의 방류량 없음: 통과
- 임의 좌표 없음: 통과
- 임의 제방 붕괴 없음: 통과

Executed source scan:

```text
rg -n "random|dummy|Math\.random|coordinates\s*:\s*\[|sample dam|sample release" backend frontend\src tests\test_disaster_system.py README.md DATA_SOURCES.md .env.example
```

Result: no matches.

Official numeric values found in source are limited to the official Osong timeline and tests that reference that timeline, including `29.02`, `17:20`, `04:10`, `06:40`, `07:50`, `08:09`, and `08:27`.

## 4. LLM Guardrail Test

- LLM이 없는 데이터를 생성하지 않는지 확인: 통과
- `allowed_actions` 외 조치를 생성하지 않는지 확인: 통과
- `official_events` 없이 제방 붕괴를 확정 표현하지 않는지 확인: 통과

Implementation:

- JSON Schema defined in `backend/services/llm_decision.py`
- `allowed_actions` enum enforced
- `source_basis` 없는 action 제거
- official `confirmed_levee_collapse` 없는 collapse status를 `field_confirmation_required`로 치환

## 5. Map Rendering Test

- 좌표 있는 시설만 지도에 표시: 통과
- 좌표 없는 시설은 목록에만 표시: 통과
- 출처 없는 시설은 위험도 계산에서 제외: 통과

Current MVP asset status:

```text
coordinates: null
coordinate_status: source_missing
display_policy: list_only_until_verified_coordinate_available
```

Frontend verification:

```text
pnpm --filter chungbuk-flood-digital-twin build
vite build completed
Node fetch http://127.0.0.1:5173 -> 200
```

Backend verification:

```text
GET http://127.0.0.1:8001/api/health -> {"status":"ok","strict_data_mode":true,"fake_data_policy":"forbidden"}
GET /api/replay/osong_2023_07_15?at=2023-07-15T08:09:00+09:00 -> risk_level critical, official confirmed_levee_collapse included
```
