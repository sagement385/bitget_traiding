# Data Sources

## Official Accident Replay

- Source: 국무조정실 오송 궁평2지하차도 침수사고 감찰조사 결과
- URL: https://www.opm.go.kr/opm/news/press1.do?articleNo=154431&attachNo=136402&mode=download
- Usage: Static official timeline events for the Osong 2023 replay, including warning issuance, design flood level reached, overflow, confirmed temporary levee collapse, and underpass inflow.

## Han River Flood Control Office

- Source: 기후에너지환경부 한강홍수통제소_표준수문DB
- URL: https://www.data.go.kr/data/3040409/openapi.do
- API reference: https://www.hrfco.go.kr/web/openapiPage/reference.do
- Usage: Rainfall, water level, discharge field from water-level data, flood forecast/warning, and station metadata.
- Key required: `HRFCO_API_KEY`

## K-water

- Source: 한국수자원공사_수문 운영 정보
- URL: https://www.data.go.kr/data/15099110/openapi.do
- Usage: Dam level, rainfall, inflow, total release, storage, and storage rate.
- Key required: `KWATER_API_KEY`

- Source: 한국수자원공사_댐코드 조회
- URL: https://www.data.go.kr/data/15099105/openapi.do
- Usage: Official dam-code validation before dam observations are requested.
- Key required: `KWATER_API_KEY`

## OpenAI LLM Guardrail Reference

- Source: OpenAI Structured Outputs documentation
- URL: https://developers.openai.com/api/docs/guides/structured-outputs
- Usage: JSON Schema based structured output for optional decision-card generation.

## Coordinates

No verified official GIS/public-data coordinates were added in this MVP. The asset file intentionally stores `coordinates: null` and `coordinate_status: source_missing` for Osong-related assets until official GIS coordinates or user-provided verified coordinates are supplied.
