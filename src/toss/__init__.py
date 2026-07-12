"""Official Toss Securities Open API adapter.

The adapter uses OAuth2 Client Credentials with the published REST endpoints.
Toss does not currently publish a WebSocket market-data API, so stock live
charts use bounded REST polling while crypto keeps its Bitget stream.
"""
