import { createServer } from "node:http";

const SERVICE = "mfp-restored-xautusd-copier-dryrun";
const MFP_BASE = (process.env.MFP_REST_BASE_URL || "https://developers.myfundedperpetuals.com/v1").replace(/\/$/, "");
const DELTA_BASE = (process.env.DELTA_BASE_URL || "https://api.india.delta.exchange").replace(/\/$/, "");
const MFP_KEY = (process.env.MFP_API_KEY || "").trim();
const CONFIGURED_ACCOUNT = (process.env.MFP_ACCOUNT_ID || "").trim();
const POLL_MS = Math.max(5000, Number(process.env.POLL_INTERVAL_MS || 5000));
const DELTA_PRODUCT_ID = 131253;
const DELTA_SYMBOL = "XAUTUSD";
const DELTA_TEST_CONTRACTS = Math.max(1, Math.min(1, Math.floor(Number(process.env.DELTA_TEST_CONTRACTS || 1))));

// Explicitly a dry-run monitor. This build contains no Delta order POST code.
const DRY_RUN = (process.env.DRY_RUN_ONLY || "true").toLowerCase() === "true";
const LIVE_EXECUTION = (process.env.LIVE_ORDER_EXECUTION || "false").toLowerCase() === "true";
if (!DRY_RUN || LIVE_EXECUTION) {
  console.error("[SAFETY FATAL] This source only supports DRY_RUN_ONLY=true and LIVE_ORDER_EXECUTION=false.");
  process.exit(1);
}
if (!MFP_KEY) {
  console.error("[CONFIG] MFP_API_KEY is missing. Cannot read source fills.");
}

const DEFAULT_MAP = {
  "hyperliquid|xyz:GOLD": "XAUTUSD",
  "binance|XAUUSDT": "XAUTUSD",
  "binance|XAU": "XAUTUSD",
  "binance|XAU-USD": "XAUTUSD",
  "binance|XAUUSD": "XAUTUSD",
  "XAUUSD": "XAUTUSD",
  "XAUUSDT": "XAUTUSD",
  "XAU-USD": "XAUTUSD",
  "GOLD": "XAUTUSD"
};

let productMap = DEFAULT_MAP;
try {
  const configured = JSON.parse(process.env.MFP_TO_DELTA_SYMBOL_MAP_JSON || "{}");
  productMap = { ...DEFAULT_MAP, ...configured };
} catch {
  throw new Error("MFP_TO_DELTA_SYMBOL_MAP_JSON is invalid JSON");
}

const seen = new Set();
let baselineReady = false;
let accountId = CONFIGURED_ACCOUNT || null;
let lastPollAt = null;
let lastSuccessAt = null;
let lastError = null;
let eventCount = 0;
let baselineCount = 0;
let polling = false;
let deltaProductInfo = null;

const server = createServer((req, res) => {
  if (req.url === "/health" || req.url === "/health/ready") {
    const body = {
      ok: true,
      service: SERVICE,
      mode: "DRY_RUN",
      liveOrderExecution: false,
      source: "MFP XAUUSD 4TF RESTORED fills",
      targetSymbol: DELTA_SYMBOL,
      targetProductId: DELTA_PRODUCT_ID,
      accountConfigured: Boolean(accountId),
      baselineFillCount: baselineCount,
      newFillPlans: eventCount,
      lastPollAt,
      lastSuccessAt,
      lastError
    };
    res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
    res.end(JSON.stringify(body));
    return;
  }
  res.writeHead(404, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: "not_found" }));
});
server.listen(Number(process.env.PORT || 8080), "0.0.0.0", () =>
  console.log("[HEALTH] listening port=" + (process.env.PORT || 8080))
);

async function getJson(url, headers = {}) {
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json", ...headers },
    signal: AbortSignal.timeout(15000)
  });
  const raw = await response.text();
  let body;
  try { body = raw ? JSON.parse(raw) : {}; }
  catch { throw new Error("HTTP " + response.status + ": non-JSON response"); }
  if (!response.ok || body?.success === false) {
    throw new Error("HTTP " + response.status + " " + JSON.stringify(body?.error || body).slice(0, 700));
  }
  return body?.data ?? body;
}

async function mfpGet(path) {
  if (!MFP_KEY) throw new Error("MFP_API_KEY missing");
  const response = await fetch(MFP_BASE + path, {
    method: "GET",
    headers: { Authorization: "Bearer " + MFP_KEY, Accept: "application/json" },
    signal: AbortSignal.timeout(15000)
  });
  const raw = await response.text();
  let body;
  try { body = raw ? JSON.parse(raw) : {}; }
  catch { throw new Error("MFP returned non-JSON HTTP " + response.status); }
  if (!response.ok) throw new Error("MFP GET " + path + " HTTP " + response.status + " " + JSON.stringify(body).slice(0, 700));
  return body?.data ?? body;
}

function rowsOf(value) {
  if (Array.isArray(value)) return value;
  for (const key of ["fills", "results", "items", "orders", "result"]) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  if (Array.isArray(value?.data)) return value.data;
  return [];
}
function first(obj, keys) {
  for (const key of keys) if (obj?.[key] !== undefined && obj?.[key] !== null) return obj[key];
  return null;
}
function stableId(fill) {
  return String(first(fill, ["fill_id", "fillId", "trade_id", "tradeId", "execution_id", "executionId", "id"]) ?? "");
}
function normalSide(value) {
  const side = String(value ?? "").trim().toLowerCase();
  if (["buy", "long", "bid"].includes(side)) return "BUY";
  if (["sell", "short", "ask"].includes(side)) return "SELL";
  return null;
}
function sourceMarket(fill) {
  return String(first(fill, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market", "coin"]) ?? "").trim();
}
function mappedSymbol(fill) {
  const market = sourceMarket(fill);
  return productMap[market] || productMap[market.toUpperCase()] || null;
}
function filledSize(fill) {
  const n = Number(first(fill, ["filled_size", "filledSize", "filled_quantity", "filledQuantity", "size", "quantity", "qty", "executed_size", "executedSize"]));
  return Number.isFinite(n) && n > 0 ? n : null;
}
function fillPrice(fill) {
  const n = Number(first(fill, ["price", "fill_price", "fillPrice", "average_price", "averagePrice", "avg_price", "avgPrice", "execution_price", "executionPrice"]));
  return Number.isFinite(n) && n > 0 ? n : null;
}
function sourceFillTime(fill) {
  return first(fill, ["created_at", "createdAt", "timestamp", "time", "filled_at", "filledAt"]);
}

async function discoverAccount() {
  if (accountId) return accountId;
  const response = await mfpGet("/accounts");
  const accounts = rowsOf(response);
  if (accounts.length !== 1) {
    throw new Error("MFP_ACCOUNT_ID is required unless exactly one account is returned; count=" + accounts.length);
  }
  accountId = String(first(accounts[0], ["account_id", "accountId", "id"]) ?? "");
  if (!accountId) throw new Error("Could not identify the sole MFP account");
  console.log("[MFP ACCOUNT] one account resolved");
  return accountId;
}

async function listFills() {
  const aid = await discoverAccount();
  const response = await mfpGet("/fills?account_id=" + encodeURIComponent(aid) + "&limit=100");
  return rowsOf(response);
}

async function deltaQuote() {
  if (!deltaProductInfo) {
    const p = await getJson(DELTA_BASE + "/v2/products/" + DELTA_PRODUCT_ID);
    const info = p?.result || p;
    if (Number(info?.id) !== DELTA_PRODUCT_ID || info?.symbol !== DELTA_SYMBOL ||
        info?.state !== "live" || info?.trading_status !== "operational") {
      throw new Error("Expected Delta XAUTUSD product is not operational");
    }
    deltaProductInfo = {
      id: Number(info.id),
      symbol: info.symbol,
      contractValue: Number(info.contract_value),
      tickSize: Number(info.tick_size),
      state: info.state,
      tradingStatus: info.trading_status
    };
    console.log("[DELTA PRODUCT OK] " + JSON.stringify(deltaProductInfo));
  }
  const t = await getJson(DELTA_BASE + "/v2/tickers/" + DELTA_SYMBOL);
  const q = t?.result || t;
  const bid = Number(q?.quotes?.best_bid);
  const ask = Number(q?.quotes?.best_ask);
  if (q?.symbol !== DELTA_SYMBOL || !Number.isFinite(bid) || !Number.isFinite(ask) || ask < bid) {
    throw new Error("Invalid Delta XAUTUSD best bid/ask");
  }
  return { bid, ask, spread: ask - bid };
}

function planFor(fill, quote) {
  const id = stableId(fill);
  const side = normalSide(first(fill, ["side", "order_side", "orderSide", "direction"]));
  const mkt = sourceMarket(fill);
  const symbol = mappedSymbol(fill);
  const size = filledSize(fill);
  const price = fillPrice(fill);

  if (!id) return { skip: "fill has no stable ID" };
  if (!side) return { id, skip: "unrecognized fill side" };
  if (!symbol || symbol !== DELTA_SYMBOL) return { id, skip: "no explicit XAUTUSD mapping", mfpMarket: mkt };
  if (!size) return { id, skip: "missing/non-positive fill size", mfpMarket: mkt };

  const contractValue = deltaProductInfo?.contractValue;
  const oneContractNotional = quote.ask * contractValue;
  const estimatedSourceNotional = price ? Math.abs(size * price) : null;

  return {
    signal: "NEW FILLED MFP EVENT",
    source: "MFP XAUUSD 4TF RESTORED",
    fill_id: id,
    mfp_market: mkt,
    mfp_side: side,
    planned_delta_side: side,
    target_symbol: DELTA_SYMBOL,
    target_product_id: DELTA_PRODUCT_ID,
    source_fill_size: size,
    source_fill_price: price,
    estimated_source_notional_usd: estimatedSourceNotional,
    delta_quote_bid: quote.bid,
    delta_quote_ask: quote.ask,
    delta_one_contract_notional_usd: Number(oneContractNotional.toFixed(6)),
    delta_contracts_preview: DELTA_TEST_CONTRACTS,
    sizing_note: "DRY RUN ONLY: one-contract test preview; not a notional-matched copy",
    source_order_id: first(fill, ["order_id", "orderId"]),
    source_fill_time: sourceFillTime(fill),
    order_sent: false
  };
}

async function seedBaseline() {
  const existing = await listFills();
  for (const fill of existing) {
    const id = stableId(fill);
    if (id) seen.add(id);
  }
  baselineCount = seen.size;
  console.log("[BASELINE] marked " + baselineCount + " existing fill IDs as historical; none will be copied");
}

async function pollOnce() {
  if (polling) return;
  polling = true;
  lastPollAt = new Date().toISOString();
  try {
    if (!baselineReady) throw new Error("Historical fill baseline not established; refusing to plan copy events");
    const fills = await listFills();
    const newFills = fills.filter(fill => {
      const id = stableId(fill);
      return Boolean(id) && !seen.has(id);
    });

    if (newFills.length) {
      const quote = await deltaQuote();
      for (const fill of newFills) {
        const id = stableId(fill);
        const plan = planFor(fill, quote);
        console.log("[COPY PLAN ONLY] " + JSON.stringify(plan));
        console.log("No Delta order sent.");
        if (id) seen.add(id);
        eventCount++;
      }
    }

    if (seen.size > 20000) {
      const keep = [...seen].slice(-10000);
      seen.clear();
      for (const id of keep) seen.add(id);
    }
    lastSuccessAt = new Date().toISOString();
    lastError = null;
    console.log("[POLL OK] fillsReturned=" + fills.length + " newFillPlans=" + newFills.length + " mode=DRY_RUN; no order endpoint exists in this build");
  } catch (error) {
    lastError = String(error?.message || error).slice(0, 1000);
    console.error("[POLL ERROR] " + lastError);
  } finally {
    polling = false;
  }
}

console.log("[START] " + JSON.stringify({
  service: SERVICE,
  source: "MFP XAUUSD 4TF RESTORED fills",
  target: DELTA_SYMBOL,
  targetProductId: DELTA_PRODUCT_ID,
  dryRun: DRY_RUN,
  liveOrderExecution: LIVE_EXECUTION,
  pollIntervalMs: POLL_MS,
  hasMfpKey: Boolean(MFP_KEY),
  hasAccountId: Boolean(CONFIGURED_ACCOUNT),
  sizingMode: "one-contract preview only; no quantity equivalence claimed"
}));
console.log("[SAFETY] This code contains no Delta order submission function.");

try {
  await discoverAccount();
  await seedBaseline();
  baselineReady = true;
  try {
    await deltaQuote();
  } catch (quoteError) {
    console.warn("[DELTA QUOTE CHECK WARNING] " + String(quoteError?.message || quoteError).slice(0, 600));
  }
  lastSuccessAt = new Date().toISOString();
} catch (error) {
  lastError = String(error?.message || error).slice(0, 1000);
  baselineReady = false;
  console.error("[STARTUP PREFLIGHT FAILED] " + lastError);
}

await pollOnce();
setInterval(() => { pollOnce().catch(error => console.error("[UNHANDLED POLL]", error)); }, POLL_MS);
