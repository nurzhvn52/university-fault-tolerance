"""Client of the external bank API."""

from decimal import Decimal

import httpx


class BankClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def charge(
        self, *, account: str, amount: Decimal, reference: str, idempotency_key: str | None = None
    ) -> dict:
        body = {"account": account, "amount": str(amount), "reference": reference}
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        response = await self._http.post(f"{self._base_url}/charges", json=body)
        response.raise_for_status()
        return response.json()
