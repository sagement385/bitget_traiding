from __future__ import annotations

from src.toss.auth import TossApiConfigurationError


class TossWebSocketClient:
    """Compatibility boundary for a future Toss streaming API.

    The published Toss Securities Open API currently provides REST only. The
    UI uses low-rate REST polling for live stock candles instead of opening a
    guessed WebSocket connection.
    """

    async def close(self) -> None:
        return None

    async def stream_candles(self, *args, **kwargs) -> None:
        raise TossApiConfigurationError(
            "Toss Securities Open API currently provides REST only; stock live data uses the built-in REST polling transport."
        )
