"""Client of the external bank API."""

from decimal import Decimal

import httpx


class BankClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def charge(self, *, account: str, amount: Decimal, reference: str) -> dict:
        response = await self._http.post(
            f"{self._base_url}/charges",
            json={"account": account, "amount": str(amount), "reference": reference},
        )
        response.raise_for_status()
        return response.json()
