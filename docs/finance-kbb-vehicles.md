# Finance: KBB Vehicle Valuations

The Financial module retrieves KBB private-party valuations through
`backend/tasks/finance_worker.py`.

Active vehicles:

- 2025 Kia Telluride: `vehicle_telluride`
- 2026 RAM 2500 Big Horn: `vehicle_ram`

The 2014 Toyota Camry was sold and must not be included.

Widget URLs are configured in the external production configuration:

`/opt/remihub-agent/config/application.ini`

Under `[Finance]`:

- `vehicle_kia_url`
- `vehicle_ram_url`

The URLs come from the `data` attribute of KBB's Price Advisor
`<object>` element.

The worker automatically updates `requesteddataversiondate`.
Mileage remains embedded in the configured URL and must be
updated periodically.

Each vehicle should have a manual fallback asset using the same
`valuation_key` as its KBB entry. Matching keys prevent double counting.

Do not rewrite historical snapshots when replacing a vehicle.

A failed Plaid mortgage connection can leave a partial snapshot
with an overstated net worth. Verify liabilities before trusting totals.
