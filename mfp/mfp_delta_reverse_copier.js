import { createServer } from "node:http";

const SERVICE = "mfp-delta-reverse-copier";
const MFP_BASE = (process.env.MFP_REST_BASE_URL || "https://developers.myfundedperpetuals.com/v1").replace(/\/$/, "");
const DELTA_BASE = (process.env.DELTA_BASE_URL || "https://api.india.delta.exchange").replace(/\/$/, "");
const MFP_KEY = (process.env.MFP_API_KEY || "").trim();
const ACCOUNT_ID = (process.env.MFP_ACCOUNT_ID || "").trim();
const POLL_MS = Math.max(5000, Number(process.env.POLL_INTERVAL_MS || 10000));
const DRY_RUN = (process.env.DRY_RUN_ONLY || "true").toLowerCase() === "true";
const LIVE_EXECUTION = (process.env.LIVE_ORDER_EXECUTION || "false").toLowerCase() === "true";

// This build intentionally has no Delta order-placement function. Even if a
// Railway variable is accidentally changed, it cannot submit an order.
if (!DRY_RUN || LIVE_EXECUTION) {
  console.error("[SAFETY FATAL] This build requires DRY_RUN_ONLY=true and LIVE_ORDER_EXECUTION=false. Refusing to start.");
  process.exit(1);
}
if (!MFP_KEY) {
  console.error("[CONFIG] MFP_API_KEY missing. Service will stay healthy but cannot read the master account.");
}

const seen = new Set();
let lastPollAt = null;
let lastSuccessAt = null;
let lastError = null;
let masterAccountId = ACCOUNT_ID || null;
let eventCount = 0;
let polling = false;

const server = createServer((req, res) => {
  if (req.url === "/health" || req.url === "/health/ready") {
    const body = {
      ok: true, service: SERVICE, mode: "DRY_RUN",
      liveOrderExecution: false, deltaEnvironment: "production",
      masterAccountConfigured: Boolean(masterAccountId),
      lastPollAt, lastSuccessAt, eventCount, lastError
    };
    res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
    res.end(JSON.stringify(body));
    return;
  }
  res.writeHead(404, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: "not_found" }));
});
server.listen(Number(process.env.PORT || 8080), "0.0.0.0", () =>
  console.log("[HEALTH] listening on " + (process.env.PORT || 8080))
);

async function mfpGet(path) {
  const response = await fetch(MFP_BASE + path, {
    method: "GET",
    headers: { Authorization: "Bearer " + MFP_KEY, Accept: "application/json" },
    signal: AbortSignal.timeout(15000)
  });
  const raw = await response.text();
  let body;
  try { body = raw ? JSON.parse(raw) : {}; } catch { body = { raw: raw.slice(0, 500) }; }
  if (!response.ok) throw new Error("MFP GET " + path + " HTTP " + response.status + " " + JSON.stringify(body).slice(0, 800));
  return body?.data ?? body;
}

function rowsOf(value) {
  if (Array.isArray(value)) return value;
  for (const key of ["data", "fills", "results", "items", "orders"]) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  return [];
}
function first(obj, keys) {
  for (const key of keys) if (obj?.[key] !== undefined && obj?.[key] !== null) return obj[key];
  return null;
}
function normalSide(value) {
  const side = String(value ?? "").trim().toLowerCase();
  if (["buy", "long", "bid"].includes(side)) return "BUY";
  if (["sell", "short", "ask"].includes(side)) return "SELL";
  return null;
}
function stableId(fill) {
  return String(first(fill, ["fill_id", "fillId", "trade_id", "tradeId", "id", "execution_id", "executionId"]) ?? "");
}
function inverse(side) { return side === "BUY" ? "SELL" : "BUY"; }

function deltaSymbolFor(fill) {
  const raw = String(first(fill, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market"]) ?? "").trim();
  let map = {};
  try { map = JSON.parse(process.env.DELTA_PRODUCT_MAP_JSON || "{}"); }
  catch { throw new Error("DELTA_PRODUCT_MAP_JSON is invalid JSON"); }
  return map[raw] || null;
}

function describeFill(fill) {
  const id = stableId(fill);
  const side = normalSide(first(fill, ["side", "order_side", "orderSide", "direction"]));
  const size = first(fill, ["size", "quantity", "qty", "filled_size", "filledSize"]);
  const symbol = deltaSymbolFor(fill);
  if (!id) return { skip: "fill has no stable ID", id: null };
  if (!side) return { skip: "unrecognized fill side", id };
  if (!symbol) return { skip: "no explicit Delta product mapping for MFP market", id, mfpMarket: first(fill, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market"]) };
  if (!(Number(size) > 0)) return { skip: "fill size missing or non-positive", id };
  return {
    id,
    source: "MFP competition demo",
    mfpSide: side,
    plannedDeltaSide: inverse(side),
    deltaProduct: symbol,
    size,
    sourceOrderId: first(fill, ["order_id", "orderId"]),
    sourceTimestamp: first(fill, ["created_at", "createdAt", "timestamp", "time"]),
    action: "DRY_RUN_ONLY — no Delta request will be sent"
  };
}

async function discoverAccount() {
  if (masterAccountId) return masterAccountId;
  const response = await mfpGet("/accounts");
  const accounts = rowsOf(response);
  if (accounts.length !== 1) {
    throw new Error("MFP_ACCOUNT_ID must be set unless exactly one account is returned; found " + accounts.length);
  }
  masterAccountId = String(first(accounts[0], ["account_id", "accountId", "id"]) ?? "");
  if (!masterAccountId) throw new Error("Could not identify the sole MFP account");
  console.log("[MFP MASTER] one account resolved; ID=" + masterAccountId);
  return masterAccountId;
}

async function pollOnce() {
  if (polling) return;
  polling = true;
  lastPollAt = new Date().toISOString();
  try {
    if (!MFP_KEY) throw new Error("MFP_API_KEY missing");
    const accountId = await discoverAccount();
    // Read-only source endpoint. No MFP or Delta write endpoints are called.
    const result = await mfpGet("/fills?account_id=" + encodeURIComponent(accountId) + "&limit=100");
    const fills = rowsOf(result);
    for (const fill of fills) {
      const id = stableId(fill);
      if (!id || seen.has(id)) continue;
      // Mark seen before logging so repeated polls cannot print the same event.
      seen.add(id);
      const plan = describeFill(fill);
      eventCount++;
      console.log("[COPY PLAN] " + JSON.stringify(plan));
    }
    // Bound in-memory dedupe state for long-running Railway process.
    if (seen.size > 20000) {
      const keep = [...seen].slice(-10000);
      seen.clear();
      for (const id of keep) seen.add(id);
    }
    lastSuccessAt = new Date().toISOString();
    lastError = null;
    console.log("[POLL OK] fillsReturned=" + fills.length + " uniquePlans=" + eventCount + " mode=DRY_RUN deltaBase=" + DELTA_BASE);
  } catch (error) {
    lastError = String(error?.message || error).slice(0, 1000);
    console.error("[POLL ERROR] " + lastError);
  } finally {
    polling = false;
  }
}

console.log("[START] " + JSON.stringify({
  service: SERVICE,
  master: "MFP competition demo",
  deltaEnvironment: "production",
  deltaBase: DELTA_BASE,
  dryRun: DRY_RUN,
  liveOrderExecution: LIVE_EXECUTION,
  pollIntervalMs: POLL_MS,
  hasMfpKey: Boolean(MFP_KEY),
  hasAccountId: Boolean(ACCOUNT_ID),
  productMappingConfigured: Boolean(process.env.DELTA_PRODUCT_MAP_JSON)
}));
console.log("[SAFETY] This code contains no Delta order submission call. It only logs inverse-side plans.");
await pollOnce();
setInterval(() => { pollOnce().catch(error => console.error("[UNHANDLED POLL]", error)); }, POLL_MS);
