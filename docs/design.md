# Design notes

## Ledger versus derived totals

Events are append-only through the HTTP API. Monthly totals are derived from the ledger until close, then retained as a JSON snapshot. The snapshot records line quantities, included amounts, prices, rated amounts and close time. A repeated close returns that exact snapshot. Rates are deliberately static; changing plans or retroactive rates are outside this demo.

An event's canonical identity includes its ID, meter, integer quantity and UTC timestamp. Reusing an ID with altered data is a conflict, including when the old event belongs to another month. A duplicate lookup happens before the closed-period check so that legitimate retries still succeed after close. Tenant A and tenant B may reuse the same event ID independently.

## Transaction boundary

Ingestion acquires the SQLite write reservation before looking up duplicates or checking the monthly close. Closing acquires the same reservation before aggregating and inserting the snapshot. Therefore either ingestion commits first and is included, or close commits first and rejects the new event. A summary uses an explicit read transaction, so snapshot lookup and aggregation share a consistent SQLite snapshot.

WAL allows concurrent readers but does not turn SQLite into a distributed store. Writes serialize and use a ten-second busy timeout. Tests exercise multiple connections against a file-backed database, not only mocked persistence. There is no retry policy or 503 translation for sustained lock contention.

## Authentication and authorization

Token-to-tenant mappings are supplied by environment configuration. Requests compare tokens with `hmac.compare_digest`. Missing or invalid credentials get 401, and tenant selection cannot be overridden by JSON input. Every tenant token can close that tenant's ended periods; there are no separate billing-admin permissions. This simplifies the example but is not a suitable enterprise authorization model.

Do not expose the example Compose configuration publicly. A real deployment needs HTTPS, a credential lifecycle with hashed server-side key records, granular roles, rate limits, audit events, managed secrets, backup/restore tests, and request-body limits. Application logs must not capture bearer credentials. The database and its volume must be protected as tenant data.

## Known limits and next steps

- No real invoices, payments, tax, discounts, currency conversion or financial compliance.
- No adjustments or reversal ledger. A wrong closed period cannot be reopened through this API; a real service needs explicit versioned adjustments.
- No billable-time storage gauges: `storage_mb` is a submitted count of units.
- Client timestamps and quantities are trusted after validation. No signed provider events or anti-fraud controls.
- No enforced historical ingestion window; old open periods may receive events.
- No dynamic pricing versioning, allowance rollover, daily proration or seat billing.
- SQLite integer aggregate capacity and single-node write throughput bound scale. No distributed idempotency guarantee is claimed.
- Liveness is not readiness. Metrics, tracing, lock-error handling and schema migrations remain production work.
- Concurrent provisioning and live credential rotation are outside this factory-based example.

The implementation intentionally keeps money calculation pure and storage queries parameterized so those parts can be reviewed without running the web server.
