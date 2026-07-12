# Chungbuk Flood Disaster Digital Twin

Official-data-first digital twin for replaying and supporting disaster response decisions during Chungbuk heavy-rain events.

The first MVP focuses on the 2023-07-15 Osong Gungpyeong 2 underpass flood accident. The system never inserts invented rainfall, water level, discharge, dam release, collapse events, or coordinates. Missing data is returned as `unknown`, `not_available`, `source_missing`, or an explicit API error.

## What Is Included

- FastAPI backend under `backend/`
- React/Vite frontend under `frontend/`
- Official Osong replay data at `backend/data/events/osong_2023_official.json`
- No-fake-data API clients for HRFCO and K-water
- Mechanical calculation module
- Data validation module
- LLM decision guardrails with JSON Schema and allowed actions
- Map UI that renders only verified coordinates

## Required Accounts And Keys

- HRFCO OpenAPI key: apply through the Han River Flood Control Office OpenAPI page.
- K-water public data key: apply through data.go.kr for `한국수자원공사_수문 운영 정보` and `한국수자원공사_댐코드 조회`.
- OpenAI API key: optional. Without it, the backend uses a deterministic guarded fallback decision.
- GitHub account/repository: needed only when you want to publish this workspace.

Put real keys in `.env`, never in `.env.example`.

```env
HRFCO_API_KEY=
KWATER_API_KEY=
OPENAI_API_KEY=
ENABLE_LLM=true
SAVE_API_SNAPSHOTS=true
STRICT_DATA_MODE=true
```

## Backend

```powershell
cd D:\bitget_quant_system
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

Open API docs:

```text
http://127.0.0.1:8001/docs
```

The quant UI defaults to Bitget Futures only. Stock/Toss panels and the surge
scanner stay disabled unless `ENABLE_STOCK_MARKETS=true` and, separately,
`ENABLE_SURGE_SCANNER=true` are set in `.env`.

Key endpoints:

- `GET /api/replay/osong_2023_07_15`
- `GET /api/hrfco/stations`
- `GET /api/hrfco/water-level`
- `GET /api/hrfco/rainfall`
- `GET /api/hrfco/discharge`
- `GET /api/hrfco/flood-warning`
- `GET /api/kwater/dam-codes`
- `GET /api/kwater/dams/{dam_code}/observations`

## Frontend

If global Node is unavailable, use the bundled Node/pnpm paths shown by Codex workspace dependencies.

```powershell
cd D:\bitget_quant_system\frontend
pnpm install
pnpm dev
```

Open:

```text
http://127.0.0.1:5173
```

## API Connection Test

```powershell
cd D:\bitget_quant_system
python -m backend.scripts.run_api_connection_test
```

If keys are absent or an external API fails, the report uses explicit error objects and does not create fake data.

## Test

```powershell
cd D:\bitget_quant_system
pytest tests/test_disaster_system.py -q
```

SQLite schema maintenance can be run explicitly with:

```powershell
python -m src.main migrate
```

## Data Policy

- Every observation used for risk calculation must include `source_name`, `source_url`, `observed_at`, `unit`, and a numeric `value`.
- Coordinates are not guessed. Assets without verified coordinates appear in the list panel only.
- Dam codes are validated through the official K-water dam-code API before observations are requested.
- LLM actions without `source_basis` are removed when `STRICT_DATA_MODE=true`.
- `confirmed_levee_collapse` is shown only when it exists in official event data.
