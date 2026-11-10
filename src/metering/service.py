"""SQLite ledger and pure monthly rating rules."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# Included units and integer millionths of EUR per additional unit.
METERS = {"api_calls": (1000, 50), "storage_mb": (1024, 2000)}


class Conflict(Exception):
    pass


def period_bounds(period: str) -> tuple[str, str]:
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    if start.strftime("%Y-%m") != period or start.year >= 9999:
        raise ValueError("Use YYYY-MM with a year before 9999")
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start.isoformat(timespec="microseconds"), end.isoformat(timespec="microseconds")


def rate(period: str, quantities: dict[str, int]) -> dict:
    lines = []
    total = Decimal(0)
    for meter, (included, micros) in METERS.items():
        quantity = quantities.get(meter, 0)
        billable = max(0, quantity - included)
        price = Decimal(micros) / Decimal(1_000_000)
        amount = (billable * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append(
            {
                "meter": meter,
                "quantity": quantity,
                "included": included,
                "billable": billable,
                "unit_price_eur": str(price),
                "amount_eur": str(amount),
            }
        )
        total += amount
    return {"period": period, "currency": "EUR", "lines": lines, "total_eur": f"{total:.2f}"}


class Ledger:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    tenant TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    meter TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (tenant, event_id)
                );
                CREATE INDEX IF NOT EXISTS events_period
                    ON events(tenant, occurred_at);
                CREATE TABLE IF NOT EXISTS closed_periods (
                    tenant TEXT NOT NULL,
                    period TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY (tenant, period)
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ingest(self, tenant: str, event: dict) -> tuple[dict, bool]:
        """Serialize duplicate checks, closed-period checks and insertion."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT event_id, meter, quantity, occurred_at FROM events "
                "WHERE tenant=? AND event_id=?",
                (tenant, event["event_id"]),
            ).fetchone()
            if existing:
                if dict(existing) != event:
                    raise Conflict("Event ID already exists with a different payload")
                return event, False
            period = event["occurred_at"][:7]
            if conn.execute(
                "SELECT 1 FROM closed_periods WHERE tenant=? AND period=?", (tenant, period)
            ).fetchone():
                raise Conflict("Period is closed; new events require a separate adjustment")
            conn.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (
                    tenant,
                    event["event_id"],
                    event["meter"],
                    event["quantity"],
                    event["occurred_at"],
                ),
            )
            return event, True

    def _summary(self, conn, tenant: str, period: str) -> dict:
        start, end = period_bounds(period)
        rows = conn.execute(
            "SELECT meter, SUM(quantity) AS quantity FROM events "
            "WHERE tenant=? AND occurred_at>=? AND occurred_at<? GROUP BY meter",
            (tenant, start, end),
        )
        return rate(period, {row["meter"]: row["quantity"] for row in rows})

    def summary(self, tenant: str, period: str) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT snapshot FROM closed_periods WHERE tenant=? AND period=?", (tenant, period)
            ).fetchone()
            return (
                json.loads(row["snapshot"])
                if row
                else {**self._summary(conn, tenant, period), "status": "open"}
            )

    def close(self, tenant: str, period: str, now: datetime) -> dict:
        _, end = period_bounds(period)
        if now < datetime.fromisoformat(end):
            raise Conflict("Only an ended UTC month can be closed")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT snapshot FROM closed_periods WHERE tenant=? AND period=?", (tenant, period)
            ).fetchone()
            if row:
                return json.loads(row["snapshot"])
            snapshot = {
                **self._summary(conn, tenant, period),
                "status": "closed",
                "closed_at": now.astimezone(UTC).isoformat(timespec="microseconds"),
            }
            conn.execute(
                "INSERT INTO closed_periods VALUES (?, ?, ?)",
                (tenant, period, json.dumps(snapshot, sort_keys=True)),
            )
            return snapshot
