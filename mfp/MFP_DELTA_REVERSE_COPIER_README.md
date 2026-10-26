# MFP Competition Demo → Delta Production Reverse Copier (Dry Run)

## Safety state
- `DRY_RUN_ONLY=true` required.
- `LIVE_ORDER_EXECUTION=false` required.
- This version contains **no Delta order-placement function**. It cannot place orders.
- Delta base URL is production (`https://api.india.delta.exchange`) for future integration configuration only.
- The MFP RESTORED Gold service is not changed by this branch.

## Required Railway variables
- `MFP_API_KEY`: MFP REST API key (secret).
- `MFP_ACCOUNT_ID`: explicit ID for the MFP competition demo account; recommended to prevent account ambiguity.
- `DRY_RUN_ONLY=true`
- `LIVE_ORDER_EXECUTION=false`
- `DELTA_BASE_URL=https://api.india.delta.exchange`
- `DELTA_PRODUCT_MAP_JSON`: JSON map from exact MFP market identifier/symbol to Delta product symbol, for example `{"MFP_SYMBOL":"BTCUSD"}`. Only configure mappings after verifying both venues' actual symbols.
- Optional `POLL_INTERVAL_MS=10000`, minimum 5000 ms.

## Current behavior
- Polls the read-only MFP `GET /v1/fills` endpoint for the configured master account.
- Deduplicates fills by stable fill ID in memory.
- Logs the planned inverse-side action (MFP BUY → planned Delta SELL; MFP SELL → planned Delta BUY) only when a product mapping exists.
- Unknown symbols or incomplete fill rows are logged as skipped plans.
- Exposes `/health` and `/health/ready`.

## Important limitation before live-copy capability
This is a safe dry-run scaffold, not a complete position copier. It logs inverse-side fill plans; it does not yet reconcile partial fills, reductions, exits, restarts, or Delta positions. Do not enable live execution or add order permissions until MFP fill schema and position semantics, Delta product sizing/contract rules, and position reconciliation have been verified end-to-end. A production URL does not mean orders are enabled.
