"""HTTP clients for the services the student service depends on."""

import httpx


class PaymentClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def tuition_status(self, student_id: int, term: str) -> dict:
        response = await self._http.get(
            f"{self._base_url}/api/tuition/{student_id}", params={"term": term}
        )
        response.raise_for_status()
        return response.json()


class TimetableClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def slots(self, term: str, section_ids: list[int]) -> list[dict]:
        response = await self._http.get(
            f"{self._base_url}/api/timetable/sections",
            params={"term": term, "ids": ",".join(map(str, section_ids))},
        )
        response.raise_for_status()
        return response.json()
