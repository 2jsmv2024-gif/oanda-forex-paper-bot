# MFP Competition Demo → Delta Reverse Copier

## Current safety state
- Railway service: `MFP Competition to Delta Reverse Copier DRY RUN`.
- `DRY_RUN_ONLY=true` and `LIVE_ORDER_EXECUTION=false` are the intended operating settings.
- No live test orders are authorized or required for this stage.
- The MFP XAUUSD 4TF RESTORED service and its strategy branch are separate and must remain unchanged.
- This copier uses the documented MFP REST API; it does not scrape the MFP website or call private website endpoints.

## What the current copier does
- Polls MFP `GET /v1/positions?account_id=...&status=open&limit=100`.
- Filters positions through the explicit mapping in `DELTA_PRODUCT_MAP_JSON`.
- Builds a signed net-position snapshot and requires the changed snapshot to remain stable for two polls.
- Logs dry-run plans for source open, close, and direction-flip transitions with BUY/SELL reversed.
- Persists the snapshot and idempotency state under `/data` on the attached Railway volume.
- Uses a fixed Delta contract count; it does not scale Delta quantity proportionally to the MFP position size.
- Same-direction source size changes are logged but do not add to the Delta position. The copier is therefore position-transition based, not a per-fill proportional copier.

## Required configuration
- `MFP_API_KEY`: MFP REST API key.
- `MFP_ACCOUNT_ID`: exact MFP competition account ID.
- `MFP_REST_BASE_URL=https://developers.myfundedperpetuals.com/v1`
- `DRY_RUN_ONLY=true`
- `LIVE_ORDER_EXECUTION=false`
- `DELTA_BASE_URL=https://api.india.delta.exchange`
- `DELTA_PRODUCT_MAP_JSON`: explicit JSON mapping from the actual MFP market identifier to a verified Delta symbol. Current example mapping is `{"binance|XAUUSDT":"XAUTUSD"}`; do not assume this mapping is correct for live trading until Delta product metadata confirms it.
- Optional `POLL_INTERVAL_MS` (minimum 5000 ms).

The live code path also requires `DELTA_API_KEY`, `DELTA_API_SECRET`, `DELTA_LIVE_CONFIRMATION=YES`, `LIVE_ORDER_EXECUTION=true`, and `DRY_RUN_ONLY=false`. These switches are deliberately not being enabled here.

## Pre-live blockers
1. Verify the actual Delta gold product symbol and numeric product ID together. The copier now checks the documented Delta product-by-symbol endpoint during live preflight and blocks if the returned ID, symbol, state, or trading status do not match.
2. Confirm Delta's permitted order-size unit and minimum size. The current default is **1 Delta contract**, not 0.001 lots; do not enable live mode until this matches the intended quantity.
3. Verify the MFP position response's signed-side/size semantics and confirm the source account is the intended competition account.
4. Reconcile any existing Delta position or open orders before enabling execution. Live preflight blocks if the target product already has a position or open orders.
5. Perform read-only validation first. A dry-run plan is not proof that an actual Delta order would be accepted or filled.

## Health endpoint
The process serves `/health` and `/health/ready`. The Railway service currently has no public domain, so these endpoints are not publicly reachable unless a domain is intentionally configured.

## Strategy preservation
This branch is only for the MFP-to-Delta copier. Do not edit the locked MFP strategy logic or deploy changes to the separate `MFP XAUUSD 4TF RESTORED` service as part of copier work.
