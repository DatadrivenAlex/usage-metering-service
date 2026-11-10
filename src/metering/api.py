"""HTTP boundary: bearer credential selects the tenant; clients cannot select it."""

import hmac
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from metering.service import Conflict, Ledger, period_bounds


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
    meter: Literal["api_calls", "storage_mb"]
    quantity: Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
    occurred_at: AwareDatetime


def create_app(
    db_path: str | None = None,
    api_keys: dict[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Factory usable by tests and `uvicorn --factory`. No default credentials."""
    keys = api_keys if api_keys is not None else json.loads(os.getenv("METER_API_KEYS", "{}"))
    if (
        not isinstance(keys, dict)
        or not keys
        or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in keys.items()
        )
    ):
        raise ValueError("METER_API_KEYS must map nonempty bearer tokens to tenant names")
    ledger = Ledger(db_path or os.getenv("METER_DB", "data/metering.db"))
    now = clock or (lambda: datetime.now(UTC))
    app = FastAPI(title="Usage Metering Demo", version="0.1.0")
    bearer = HTTPBearer(auto_error=False)

    def tenant(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
        if credentials is not None:
            for token, tenant_id in keys.items():
                if hmac.compare_digest(credentials.credentials.encode(), token.encode()):
                    return tenant_id
        raise HTTPException(
            401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )

    def valid_period(period: str) -> str:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", period):
                raise ValueError
            period_bounds(period)
            return period
        except ValueError as exc:
            raise HTTPException(422, "Use YYYY-MM, a valid month, and a year before 9999") from exc

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/events", status_code=201)
    def ingest(event: Event, response: Response, tenant_id: Annotated[str, Depends(tenant)]):
        try:
            timestamp = event.occurred_at.astimezone(UTC)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, "Timestamp is outside the supported UTC range") from exc
        if not 1000 <= timestamp.year < 9999:
            raise HTTPException(422, "Timestamp year must be between 1000 and 9998")
        if timestamp > now():
            raise HTTPException(422, "Future events are not accepted")
        normalized = {
            "event_id": event.event_id,
            "meter": event.meter,
            "quantity": event.quantity,
            "occurred_at": timestamp.isoformat(timespec="microseconds"),
        }
        try:
            stored, created = ledger.ingest(tenant_id, normalized)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        response.status_code = 201 if created else 200
        return {**stored, "duplicate": not created}

    @app.get("/v1/periods/{period}")
    def summary(period: str, tenant_id: Annotated[str, Depends(tenant)]):
        return ledger.summary(tenant_id, valid_period(period))

    @app.post("/v1/periods/{period}/close")
    def close(period: str, tenant_id: Annotated[str, Depends(tenant)]):
        try:
            return ledger.close(tenant_id, valid_period(period), now())
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
