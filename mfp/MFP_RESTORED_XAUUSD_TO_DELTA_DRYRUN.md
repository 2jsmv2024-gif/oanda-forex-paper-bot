# MFP XAUUSD RESTORED → Delta XAUTUSD (dry-run only)

This is an isolated first-stage copier for the Railway `MFP XAUUSD 4TF RESTORED` service. It does not modify the strategy or the original MFP competition-to-Delta reverse copier.

## Safety contract

- `DRY_RUN_ONLY=true` is required.
- `LIVE_ORDER_EXECUTION=false` is required.
- The source code contains no Delta order-submission function.
- It reads the MFP REST `GET /fills` endpoint, establishes a startup baseline, and ignores fill IDs already present at startup to avoid replaying old fills after deployment.
- Only newly observed, identifiable filled events are planned. An order request/acknowledgement or `[MFP NOT FILLED]` log is not a fill and is not copied.
- This branch uses the **same side as the actual MFP fill** (BUY→BUY, SELL→SELL). It does not apply another reverse toggle; the MFP restored strategy's emitted side already reflects its own strategy settings.
- Delta target: XAUTUSD, product ID 131253.
- The plan is capped at a one-contract preview and explicitly is **not notional-matched** to the MFP quantity. No actual Delta order is sent.

## Separate Railway service configuration

If Railway resources permit, create a separate service in Splendid Eagerness production:

- Repository: `2jsmv2024-gif/oanda-forex-paper-bot`
- Branch: `mfp-restored-xautusd-copier-dryrun`
- Start command: `node mfp/mfp_restored_xautusd_copier_dryrun.js`
- Healthcheck path: `/health`
- Keep one replica.
- Use Railway's variable-reference picker to reference `MFP_API_KEY` and `MFP_ACCOUNT_ID` from the existing `MFP XAUUSD 4TF RESTORED` service. Do not copy secrets into chat.
- Set `MFP_REST_BASE_URL=https://developers.myfundedperpetuals.com/v1`
- Set `DELTA_BASE_URL=https://api.india.delta.exchange`
- Set `DRY_RUN_ONLY=true`
- Set `LIVE_ORDER_EXECUTION=false`
- Set `POLL_INTERVAL_MS=5000`

The code's built-in map recognizes `hyperliquid|xyz:GOLD`, common Binance XAU identifiers, `XAUUSD`, `XAUUSDT`, and `GOLD` as source aliases for Delta XAUTUSD. Review the actual fill market IDs in logs before trusting this mapping.

## Not production-ready for live copying

A genuine live copier must separately implement and validate Delta order authentication, IP allowlisting for Railway egress, notional/contract conversion, deduplication persistent across restarts, position and partial-fill reconciliation, close/reduction logic, and exchange-side stop-loss creation/verification. Those features are intentionally not in this dry-run file.

The latest inspected MFP restored service log contained an `[MFP NOT FILLED]` event with no position armed. That event must not be mirrored as a filled trade. The existing Delta account screenshot showed only about $0.12 available margin after a one-contract XAUTUSD position; a one-contract plan is merely a test preview and may still be rejected if ever submitted.
