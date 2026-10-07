# Retirement-account readiness

The strategy is long-only, fully funded, and uses no options, derivatives, margin, or short sales.
Tax effects are intentionally omitted for the retirement-account first pass. Those properties do
not make a particular account operationally ready.

Before any manual pilot:

1. Copy `config/portfolio.example.toml` to the ignored `config/portfolio.toml` and limit
   `allowed_tickers` to the exact plan menu or brokerage-window permissions.
2. Put only the explicitly managed sleeve in `[holdings]`; unrelated retirement assets must stay
   outside this file and are never targeted.
3. Confirm the plan's own trade-frequency, settlement, minimum-order, redemption-fee, and fund
   substitution rules. The software deliberately fails when a target is unavailable and never
   invents a proxy.
4. Run `poetry run swing-trader ticket preview`. It uses the latest research adjusted close only
   to estimate shares. Recheck every price, available cash amount, and restriction at the broker.
5. Do not act unless the independent data gate passed, sufficient prospective decisions matured,
   and the user separately approved a small manual pilot.

Every preview is labeled non-executable, sets both `execution_authorized` and
`automatic_order_placement` to false, and is written under the ignored `reports/private/` path.
The project never receives broker credentials.
