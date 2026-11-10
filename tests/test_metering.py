from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from metering.api import create_app
from metering.service import Conflict, Ledger, period_bounds, rate

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
A = {"Authorization": "Bearer token-alpha"}
B = {"Authorization": "Bearer token-beta"}


def event(**overrides):
    return {
        "event_id": "event-001",
        "meter": "api_calls",
        "quantity": 100000,
        "occurred_at": "2026-10-14T12:00:00Z",
        **overrides,
    }


@pytest.fixture
def client(tmp_path):
    app = create_app(
        str(tmp_path / "meter.db"), {"token-alpha": "alpha", "token-beta": "beta"}, lambda: NOW
    )
    with TestClient(app) as c:
        yield c


def post(client, headers=A, **overrides):
    return client.post("/v1/events", headers=headers, json=event(**overrides))


def summary(client, headers=A, period="2026-10"):
    return client.get(f"/v1/periods/{period}", headers=headers)


def test_health_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "/v1/events" in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}]
)
def test_auth_required(client, headers):
    assert post(client, headers).status_code == 401
    assert summary(client, headers).status_code == 401
    assert client.post("/v1/periods/2026-10/close", headers=headers).status_code == 401


def test_first_write_and_exact_retry(client):
    assert post(client).status_code == 201
    retry = post(client)
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert summary(client).json()["lines"][0]["quantity"] == 100000


def test_timezone_equivalent_retry(client):
    assert post(client).status_code == 201
    assert post(client, occurred_at="2026-10-14T14:00:00+02:00").status_code == 200


@pytest.mark.parametrize(
    "change", [{"quantity": 1}, {"meter": "storage_mb"}, {"occurred_at": "2026-10-15T12:00:00Z"}]
)
def test_conflicting_retry(client, change):
    post(client)
    assert post(client, **change).status_code == 409
    assert summary(client).json()["lines"][0]["quantity"] == 100000


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": True},
        {"quantity": "12"},
        {"quantity": 1000001},
        {"meter": "unknown"},
        {"event_id": ""},
        {"event_id": "has spaces"},
        {"tenant": "beta"},
        {"occurred_at": "2026-10-14T12:00:00"},
        {"occurred_at": "not-a-date"},
        {"occurred_at": "2026-11-10T12:00:01Z"},
    ],
)
def test_validation_rejects_invalid_event(client, change):
    assert post(client, **change).status_code == 422


def test_tenant_isolation_and_scoped_ids(client):
    assert post(client).status_code == 201
    assert summary(client, B).json()["total_eur"] == "0.00"
    assert post(client, B, quantity=5).status_code == 201
    assert summary(client, B).json()["lines"][0]["quantity"] == 5
    assert summary(client).json()["lines"][0]["quantity"] == 100000


def test_exact_decimal_rating_and_per_line_rounding():
    result = rate("2026-10", {"api_calls": 1100, "storage_mb": 1027})
    # EUR .005 and .006 both round half-up to .01, then sum.
    assert [line["amount_eur"] for line in result["lines"]] == ["0.01", "0.01"]
    assert result["total_eur"] == "0.02"


def test_allowance_and_no_negative_charge():
    assert rate("2026-10", {"api_calls": 1000, "storage_mb": 1})["total_eur"] == "0.00"


def test_sample_rating(client):
    post(client)
    result = summary(client).json()
    assert result["total_eur"] == "4.95"
    assert result["lines"][0]["billable"] == 99000
    assert result["status"] == "open"


def test_half_open_utc_boundaries(client):
    post(client, event_id="before", quantity=1, occurred_at="2026-10-01T01:59:59+02:00")
    post(client, event_id="start", quantity=2, occurred_at="2026-10-01T02:00:00+02:00")
    post(client, event_id="last", quantity=3, occurred_at="2026-10-31T23:59:59.999999Z")
    post(client, event_id="next", quantity=4, occurred_at="2026-11-01T00:00:00Z")
    assert summary(client).json()["lines"][0]["quantity"] == 5
    assert summary(client, period="2026-09").json()["lines"][0]["quantity"] == 1
    assert summary(client, period="2026-11").json()["lines"][0]["quantity"] == 4


@pytest.mark.parametrize("period", ["2026-00", "2026-13", "2026-1", "junk", "9999-12", "0000-01"])
def test_invalid_period(client, period):
    assert summary(client, period=period).status_code == 422
    assert client.post(f"/v1/periods/{period}/close", headers=A).status_code == 422


def test_december_rollover_and_leap_year():
    assert period_bounds("2026-12")[1].startswith("2027-01-01")
    assert period_bounds("2024-02")[1].startswith("2024-03-01")


def test_close_freezes_and_remains_retryable(client):
    post(client)
    closed = client.post("/v1/periods/2026-10/close", headers=A)
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert closed.json() == summary(client).json()
    assert client.post("/v1/periods/2026-10/close", headers=A).json() == closed.json()
    assert post(client).status_code == 200
    assert post(client, event_id="late").status_code == 409
    assert post(client, quantity=9).status_code == 409
    assert summary(client).json() == closed.json()


def test_close_is_tenant_scoped(client):
    client.post("/v1/periods/2026-10/close", headers=A)
    assert post(client, B).status_code == 201
    assert summary(client, B).json()["status"] == "open"


@pytest.mark.parametrize("period", ["2026-11", "2026-12"])
def test_cannot_close_current_or_future_month(client, period):
    assert client.post(f"/v1/periods/{period}/close", headers=A).status_code == 409


def test_close_empty_month(client):
    result = client.post("/v1/periods/2026-10/close", headers=A).json()
    assert result["total_eur"] == "0.00"
    assert result["status"] == "closed"


def normalized(**overrides):
    return event(occurred_at="2026-10-14T12:00:00.000000+00:00", **overrides)


def test_duplicate_race_has_one_winner(tmp_path):
    ledger = Ledger(str(tmp_path / "race.db"))
    barrier = Barrier(8)

    def insert(_):
        barrier.wait()
        return ledger.ingest("alpha", normalized())[1]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(insert, range(8)))
    assert sum(results) == 1
    assert ledger.summary("alpha", "2026-10")["lines"][0]["quantity"] == 100000


def test_close_ingest_race_is_consistent(tmp_path):
    # Either ingest wins and appears in the snapshot, or close wins and rejects it.
    for iteration in range(10):
        ledger = Ledger(str(tmp_path / f"race-{iteration}.db"))
        barrier = Barrier(2)

        def insert(ledger=ledger, barrier=barrier):
            barrier.wait()
            try:
                ledger.ingest("alpha", normalized())
                return True
            except Conflict:
                return False

        def close(ledger=ledger, barrier=barrier):
            barrier.wait()
            return ledger.close("alpha", "2026-10", NOW)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(insert), pool.submit(close)
            accepted, snapshot = first.result(), second.result()
        assert snapshot["lines"][0]["quantity"] == (100000 if accepted else 0)
        assert ledger.summary("alpha", "2026-10") == snapshot


def test_restart_preserves_snapshot(tmp_path):
    path = str(tmp_path / "persistent.db")
    ledger = Ledger(path)
    ledger.ingest("alpha", normalized())
    snapshot = ledger.close("alpha", "2026-10", NOW)
    restarted = Ledger(path)
    assert restarted.summary("alpha", "2026-10") == snapshot
    assert restarted.ingest("alpha", normalized())[1] is False


@pytest.mark.parametrize("keys", [{}, [], {"": "alpha"}, {"key": ""}, {"key": 5}])
def test_explicit_valid_credentials_required(tmp_path, keys):
    with pytest.raises(ValueError):
        create_app(str(tmp_path / "config.db"), keys)


def test_environment_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("METER_API_KEYS", '{"environment-token":"tenant-one"}')
    monkeypatch.setenv("METER_DB", str(tmp_path / "env.db"))
    with TestClient(create_app(clock=lambda: NOW)) as client:
        assert post(client, {"Authorization": "Bearer environment-token"}).status_code == 201


@pytest.mark.parametrize(
    "timestamp",
    [
        "0001-01-01T00:00:00+14:00",
        "0999-12-31T00:00:00Z",
        "9999-12-31T23:59:59-12:00",
    ],
)
def test_extreme_dates_are_validation_errors(client, timestamp):
    assert post(client, occurred_at=timestamp).status_code == 422


def test_out_of_order_and_multiple_meters(client):
    post(client, event_id="newer", quantity=2000, occurred_at="2026-10-20T12:00:00Z")
    post(client, event_id="older", quantity=1000, occurred_at="2026-10-01T12:00:00Z")
    post(client, event_id="storage", meter="storage_mb", quantity=2024)
    result = summary(client).json()
    assert result["lines"][0]["quantity"] == 3000
    assert result["lines"][0]["amount_eur"] == "0.10"
    assert result["lines"][1]["amount_eur"] == "2.00"
    assert result["total_eur"] == "2.10"


def test_missing_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("METER_API_KEYS", raising=False)
    with pytest.raises(ValueError):
        create_app(str(tmp_path / "missing.db"))
