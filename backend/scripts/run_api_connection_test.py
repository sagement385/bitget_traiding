from __future__ import annotations

import json
from pathlib import Path

from backend.services.api_snapshot import now_kst
from backend.services.config import BACKEND_ROOT
from backend.services.hrfco_client import HrfcoClient
from backend.services.kwater_client import KwaterClient


def main() -> None:
    report = {
        "tested_at": now_kst(),
        "policy": "No fake data is generated when API keys are absent or APIs fail.",
        "hrfco_station_metadata": HrfcoClient().get_station_metadata(),
        "kwater_dam_codes": KwaterClient().get_dam_codes(),
    }
    out = BACKEND_ROOT / "logs" / "api_connection_test_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
