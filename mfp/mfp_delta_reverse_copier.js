import { createServer } from "node:http";
import fs from "node:fs";
import path from "node:path";
import https from "node:https";
import crypto from "node:crypto";

const SERVICE = "mfp-delta-reverse-copier";
const MFP_BASE = (process.env.MFP_REST_BASE_URL || "https://developers.myfundedperpetuals.com/v1").replace(/\/+$/, "");
const DELTA_BASE = (process.env.DELTA_BASE_URL || "https://api.india.delta.exchange").replace(/\/+$/, "");
const MFP_KEY = (process.env.MFP_API_KEY || "").trim();
const ACCOUNT_ID = (process.env.MFP_ACCOUNT_ID || "").trim();
const DELTA_KEY = (process.env.DELTA_API_KEY || "").trim();
const DELTA_SECRET = (process.env.DELTA_API_SECRET || "").trim();
const POLL_MS = Math.max(5000, Number(process.env.POLL_INTERVAL_MS || 10000));
const DRY_RUN = (process.env.DRY_RUN_ONLY || "true").toLowerCase() === "true";
const LIVE_EXECUTION = (process.env.LIVE_ORDER_EXECUTION || "false").toLowerCase() === "true";
const LIVE_CONFIRM = (process.env.DELTA_LIVE_CONFIRMATION || "").trim() === "YES";
const FIXED_CONTRACTS = Math.max(1, Math.floor(Number(process.env.DELTA_FIXED_CONTRACTS || 1)));
const PRODUCT_ID = Number(process.env.DELTA_PRODUCT_ID || 131253);
const PRODUCT_SYMBOL = process.env.DELTA_PRODUCT_SYMBOL || "XAUTUSD";
const STATE_FILE = process.env.COPIER_STATE_PATH || "/data/mfp_delta_reverse_copier_state.json";
const executionRequested = !DRY_RUN && LIVE_EXECUTION && LIVE_CONFIRM;
// Live mode requires all three explicit operator settings above.
// This copier only trades one Delta contract and only from source position transitions.
const executionEnabled = executionRequested;
const MAX_SEEN = 20000;

let lastPollAt = null;
let lastSuccessAt = null;
let lastError = null;
let eventCount = 0;
let polling = false;
let baselineInitialized = false;
let sourceSnapshotInitialized = false;
let lastSourceSnapshot = null;
let liveActivationBaselineDone = false;
let lastDeltaPosition = null;
let productMeta = null;
let leverage = null;
let liveReady = false;

const mapping = JSON.parse(process.env.DELTA_PRODUCT_MAP_JSON || "{\"binance|XAUUSDT\":\"XAUTUSD\"}");
const seen = new Set();
const attempted = new Set();

function readState() {
  try {
    const s = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    for (const id of Array.isArray(s.seen) ? s.seen : []) seen.add(String(id));
    for (const id of Array.isArray(s.attempted) ? s.attempted : []) attempted.add(String(id));
    baselineInitialized = s.baselineInitialized === true;
    sourceSnapshotInitialized = s.sourceSnapshotInitialized === true;
    lastSourceSnapshot = s.lastSourceSnapshot && typeof s.lastSourceSnapshot === "object"
      ? s.lastSourceSnapshot : null;
  } catch {}
}
function saveState() {
  const dir = path.dirname(STATE_FILE);
  fs.mkdirSync(dir, { recursive: true });
  const payload = {
    baselineInitialized,
    sourceSnapshotInitialized,
    lastSourceSnapshot,
    seen: [...seen].slice(-MAX_SEEN),
    attempted: [...attempted].slice(-MAX_SEEN),
    savedAt: new Date().toISOString()
  };
  const temp = STATE_FILE + ".tmp";
  fs.writeFileSync(temp, JSON.stringify(payload));
  fs.renameSync(temp, STATE_FILE);
}
function first(obj, keys) {
  for (const k of keys) if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  return null;
}
function rowsOf(value) {
  if (Array.isArray(value)) return value;
  for (const k of ["data", "fills", "results", "items", "orders", "positions"]) {
    if (Array.isArray(value && value[k])) return value[k];
  }
  return [];
}
function normalSide(value) {
  const s = String(value || "").trim().toLowerCase();
  if (["buy", "long", "bid"].includes(s)) return "BUY";
  if (["sell", "short", "ask"].includes(s)) return "SELL";
  return null;
}
function reverseSide(side) { return side === "BUY" ? "SELL" : "BUY"; }
function stableOrderKey(fill) {
  const order = first(fill, ["order_id", "orderId"]);
  const client = first(fill, ["client_order_id", "clientOrderId", "order_client_id"]);
  const fillId = first(fill, ["fill_id", "fillId", "trade_id", "tradeId", "id", "execution_id", "executionId"]);
  return String(client || order || fillId || "");
}
function fillTime(fill) {
  const value = first(fill, ["created_at", "createdAt", "timestamp", "time", "filled_at", "filledAt"]);
  const n = typeof value === "number" ? value : Date.parse(String(value || ""));
  return Number.isFinite(n) ? n : 0;
}
function closeFill(fill) {
  const raw = first(fill, ["client_order_id", "clientOrderId", "order_client_id"]);
  const nested = fill && (fill.order || fill.order_info || fill.orderInfo);
  const client = String(raw || first(nested, ["client_order_id", "clientOrderId"]) || "").toLowerCase();
  const flag = first(fill, ["reduce_only", "reduceOnly", "is_close", "isClose", "close_position", "closePosition"]);
  return flag === true || String(flag).toLowerCase() === "true" ||
    client.startsWith("xau4tf-close:") || client.includes(":close:");
}
function deltaSymbolFor(fill) {
  const raw = String(first(fill, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market"]) || "").trim();
  return mapping[raw] || mapping[raw.toUpperCase()] || null;
}
function positionRowsOf(value) {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value && value.positions)) return value.positions;
  if (Array.isArray(value && value.data)) return value.data;
  if (Array.isArray(value && value.results)) return value.results;
  if (Array.isArray(value && value.items)) return value.items;
  if (value && value.data && typeof value.data === "object") return positionRowsOf(value.data);
  if (value && value.result && typeof value.result === "object") return positionRowsOf(value.result);
  return [];
}
function buildSourcePositionSnapshot(response) {
  const rows = positionRowsOf(response);
  const relevant = [];
  let signed = 0;
  for (const row of rows) {
    const status = String(first(row, ["status", "position_status", "positionStatus"]) || "open").toLowerCase();
    if (status && !["open", "active"].includes(status)) continue;
    const rawMarket = String(first(row, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market"]) || "").trim();
    const target = mapping[rawMarket] || mapping[rawMarket.toUpperCase()] || null;
    if (target !== PRODUCT_SYMBOL) continue;
    const side = normalSide(first(row, ["side", "position_side", "positionSide", "direction"]));
    const size = Number(first(row, ["size", "quantity", "qty", "position_size", "positionSize"]));
    if (!side || !(Number.isFinite(size) && size > 0)) continue;
    const id = String(first(row, ["position_id", "positionId", "id"]) || "");
    const signedSize = side === "BUY" ? size : -size;
    signed += signedSize;
    relevant.push({ id, side, size, market: rawMarket });
  }
  const rounded = Math.round(signed * 1e8) / 1e8;
  relevant.sort((a, b) => a.id.localeCompare(b.id));
  return {
    signedSize: rounded,
    positionCount: relevant.length,
    signature: relevant.map(p => [p.id, p.side, p.size, p.market].join(":")).join("|"),
    updatedAt: new Date().toISOString()
  };
}
function positionPlan(snapshot, transition) {
  const sourceSide = snapshot.signedSize > 0 ? "BUY" : "SELL";
  const isClose = transition === "CLOSE";
  const plannedDeltaSide = isClose ? sourceSide : reverseSide(sourceSide);
  const digest = crypto.createHash("sha256")
    .update(transition + "|" + snapshot.signature + "|" + snapshot.signedSize + "|" + String(snapshot.updatedAt))
    .digest("hex").slice(0, 20);
  return {
    id: "mfp-position-" + transition.toLowerCase() + ":" + digest,
    sourceSide,
    plannedDeltaSide,
    sourceMarket: "binance|XAUUSDT",
    deltaProduct: PRODUCT_SYMBOL,
    sourceSize: Math.abs(snapshot.signedSize),
    deltaContracts: FIXED_CONTRACTS,
    isClose,
    sourceClientOrderId: null,
    sourceTimestamp: snapshot.updatedAt,
    policy: "dry-run position snapshot transition; fixed Delta size; does not infer MFP filled_size"
  };
}
function mfpGet(endpoint) {
  if (!MFP_KEY) throw new Error("MFP_API_KEY missing in copier service variables");
  return fetchJson(MFP_BASE + endpoint, "GET", {
    Authorization: "Bearer " + MFP_KEY,
    Accept: "application/json",
    "User-Agent": "MFP-Delta-Reverse-Copier/1.0"
  });
}
function fetchJson(url, method, headers, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = https.request(u, { method, headers, timeout: 15000 }, res => {
      let raw = "";
      res.setEncoding("utf8");
      res.on("data", chunk => { raw += chunk; });
      res.on("end", () => {
        let data;
        try { data = raw ? JSON.parse(raw) : {}; }
        catch { return reject(new Error("HTTP " + res.statusCode + ": non-JSON response")); }
        if (res.statusCode < 200 || res.statusCode >= 300 || data.success === false) {
          return reject(new Error("HTTP " + res.statusCode + ": " + JSON.stringify(data.error || data).slice(0, 1200)));
        }
        resolve(data);
      });
    });
    req.on("timeout", () => req.destroy(new Error("Request timeout")));
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}
function deltaSigned(method, endpoint, bodyObject) {
  if (!DELTA_KEY || !DELTA_SECRET) throw new Error("DELTA_API_KEY and DELTA_API_SECRET must be set on the copier service");
  const body = bodyObject === undefined || bodyObject === null ? "" : JSON.stringify(bodyObject);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = crypto.createHmac("sha256", DELTA_SECRET)
    .update(method + timestamp + endpoint + body).digest("hex");
  return fetchJson(DELTA_BASE + endpoint, method, {
    Accept: "application/json",
    "Content-Type": "application/json",
    "User-Agent": "MFP-Delta-Reverse-Copier/1.0",
    "api-key": DELTA_KEY,
    signature,
    timestamp
  }, body);
}
async function deltaPublic(endpoint) {
  return fetchJson(DELTA_BASE + endpoint, "GET", {
    Accept: "application/json",
    "User-Agent": "MFP-Delta-Reverse-Copier/1.0"
  });
}
function deltaResult(response) { return response && (response.result !== undefined ? response.result : response.data !== undefined ? response.data : response); }
async function deltaPosition() {
  const r = deltaResult(await deltaSigned("GET", "/v2/positions?product_id=" + PRODUCT_ID));
  const rows = Array.isArray(r) ? r : (r && typeof r === "object" && ("size" in r || "product_id" in r) ? [r] : rowsOf(r));
  const p = rows.find(x => Number(x.product_id || PRODUCT_ID) === PRODUCT_ID && Number(x.size) !== 0);
  if (!p) return { signedSize: 0, raw: null };
  let size = Number(p.size || 0);
  const side = String(first(p, ["side", "position_side", "positionSide"]) || "").toLowerCase();
  if (size > 0 && (side.includes("short") || side === "sell")) size = -size;
  if (size < 0 && (side.includes("long") || side === "buy")) size = Math.abs(size);
  return { signedSize: size, raw: p };
}
async function deltaOpenOrders() {
  const r = deltaResult(await deltaSigned("GET", "/v2/orders?product_id=" + PRODUCT_ID + "&state=open"));
  return rowsOf(r).filter(o => Number(o.product_id || PRODUCT_ID) === PRODUCT_ID);
}
async function deltaUsdAvailable() {
  const r = deltaResult(await deltaSigned("GET", "/v2/wallet/balances"));
  const rows = rowsOf(r);
  const w = rows.find(x => String(x.asset_symbol || x.asset || "").toUpperCase() === "USD");
  return w ? Number(w.available_balance) : null;
}
async function preflightDelta() {
  if (liveReady) return;
  liveReady = false;
  if (!executionEnabled) return;
  if (!DELTA_KEY || !DELTA_SECRET) {
    lastError = "DELTA_API_KEY or DELTA_API_SECRET missing";
    console.error("[LIVE BLOCKED] " + lastError);
    return;
  }
  try {
    const p = deltaResult(await deltaPublic("/v2/products/" + PRODUCT_ID));
    if (!p || Number(p.id) !== PRODUCT_ID || p.symbol !== PRODUCT_SYMBOL ||
        p.state !== "live" || p.trading_status !== "operational") {
      lastError = "Delta product not live/operational";
      console.error("[LIVE BLOCKED] " + lastError);
      return;
    }
    productMeta = p;
    const l = deltaResult(await deltaSigned("GET", "/v2/products/" + PRODUCT_ID + "/orders/leverage"));
    leverage = Number(l && l.leverage);
    if (!(leverage > 0)) {
      lastError = "Could not verify Delta leverage";
      console.error("[LIVE BLOCKED] " + lastError);
      return;
    }
    const pos = await deltaPosition();
    const orders = await deltaOpenOrders();
    lastDeltaPosition = pos.signedSize;
    if (pos.signedSize !== 0 || orders.length > 0) {
      lastError = "Existing XAUTUSD position/order detected; close or reconcile it before live copying";
      console.error("[LIVE BLOCKED] " + lastError + " positionSize=" + pos.signedSize + " openOrders=" + orders.length);
      return;
    }
    const usd = await deltaUsdAvailable();
    if (!(Number.isFinite(usd) && usd > 0)) {
      lastError = "No positive available USD balance";
      console.error("[LIVE BLOCKED] " + lastError);
      return;
    }
    liveReady = true;
    lastError = null;
    console.log("[DELTA PREFLIGHT OK] product=" + PRODUCT_SYMBOL + " id=" + PRODUCT_ID +
      " tick=" + p.tick_size + " contractValue=" + p.contract_value +
      " leverage=" + leverage + "x availableUSD=" + usd +
      " fixedContracts=" + FIXED_CONTRACTS + " maxNetPosition=1 contract");
  } catch (e) {
    lastError = String(e && e.message || e).slice(0, 1000);
    console.error("[DELTA PREFLIGHT ERROR] " + lastError);
  }
}
function describeFill(fill) {
  const id = stableOrderKey(fill);
  const sourceSide = normalSide(first(fill, ["side", "order_side", "orderSide", "direction"]));
  const targetSymbol = deltaSymbolFor(fill);
  const status = String(first(fill, ["status", "order_status", "orderStatus"]) || "").toLowerCase();
  const quantityKeys = [
    "filled_size", "filledSize", "filled_quantity", "filledQuantity",
    "executed_size", "executedSize", "quantity_filled", "quantityFilled",
    "fill_size", "fillSize"
  ];
  const quantityKey = quantityKeys.find(k =>
    Object.prototype.hasOwnProperty.call(fill || {}, k) &&
    fill[k] !== null && fill[k] !== undefined && fill[k] !== ""
  );
  const reportedFilled = quantityKey ? Number(fill[quantityKey]) : null;
  const size = quantityKey ? fill[quantityKey] : first(fill, ["size", "quantity", "qty"]);
  if (!id) return { skip: "fill has no stable order/fill ID" };
  if (!sourceSide) return { id, skip: "unrecognized source side" };
  if (!targetSymbol) return { id, skip: "no explicit Delta mapping for MFP market", mfpMarket: first(fill, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market"]) };
  if (status && !["filled", "partially_filled", "partial", "completed", "executed", "settled"].includes(status)) {
    return { id, skip: "MFP record is not in a completed/partially-filled state", status };
  }
  // Fail closed: if the API explicitly reports zero filled quantity, never infer
  // an execution from requested order size. This prevents copying ambiguous rows
  // such as status=filled with filled_size=0.
  if (quantityKey && !(reportedFilled > 0)) {
    return {
      id,
      skip: "MFP API reports zero/non-positive executed quantity; refusing to infer from order size",
      status,
      reportedSize: first(fill, ["size", "quantity", "qty"]),
      reportedFilledSize: fill[quantityKey],
      quantityField: quantityKey
    };
  }
  if (!(Number(size) > 0)) return { id, skip: "source execution size missing or non-positive" };
  const isClose = closeFill(fill);
  return {
    id,
    sourceSide,
    plannedDeltaSide: reverseSide(sourceSide),
    sourceMarket: first(fill, ["market_id", "marketId", "symbol", "product_symbol", "productSymbol", "ticker", "market"]),
    deltaProduct: targetSymbol,
    sourceSize: size,
    deltaContracts: FIXED_CONTRACTS,
    isClose,
    reportedStatus: status || null,
    quantityField: quantityKey || "size/quantity fallback (no filled-quantity field)",
    sourceOrderId: first(fill, ["order_id", "orderId"]),
    sourceClientOrderId: first(fill, ["client_order_id", "clientOrderId", "order_client_id"]),
    sourceTimestamp: first(fill, ["created_at", "createdAt", "timestamp", "time"]),
    policy: "fixed small Delta size; not proportional to MFP source size; explicit zero fill quantity is skipped"
  };
}
async function copyFill(fill, plan) {
  if (!executionEnabled) {
    console.log("[COPY PLAN] " + JSON.stringify({ ...plan, mode: "DRY_RUN_ONLY", deltaOrderSent: false }));
    return;
  }
  if (!liveReady) {
    console.error("[COPY BLOCKED] Delta preflight not ready; fill=" + plan.id + " no order sent");
    return;
  }
  const current = await deltaPosition();
  const signedSize = current.signedSize;
  const wantedSign = plan.plannedDeltaSide === "BUY" ? 1 : -1;
  const close = plan.isClose === true;
  if (close) {
    if (signedSize === 0 || Math.sign(signedSize) !== -wantedSign) {
      console.warn("[CLOSE SKIPPED] no matching inverse Delta position for source close; fill=" + plan.id + " currentDeltaSize=" + signedSize);
      return;
    }
  } else {
    if (signedSize !== 0) {
      console.warn("[ENTRY SKIPPED] one-contract cap/current position prevents adding another entry; fill=" + plan.id + " currentDeltaSize=" + signedSize);
      return;
    }
  }
  const usd = await deltaUsdAvailable();
  const priceResult = deltaResult(await deltaPublic("/v2/tickers/" + PRODUCT_SYMBOL));
  const ticker = priceResult && (priceResult.quotes || priceResult);
  const price = Number(ticker && (ticker.best_ask || ticker.ask || priceResult.mark_price || priceResult.close));
  const contractValue = Number(productMeta && productMeta.contract_value);
  const marginEstimate = Number.isFinite(price) && contractValue > 0 && leverage > 0
    ? price * contractValue * FIXED_CONTRACTS / leverage : null;
  const feeRate = Number(productMeta && productMeta.taker_commission_rate) || 0.001;
  const reserve = marginEstimate === null ? Infinity :
    marginEstimate + price * contractValue * FIXED_CONTRACTS * feeRate + 0.005;
  if (!close && (!(Number.isFinite(usd) && usd >= reserve))) {
    console.error("[ENTRY BLOCKED] available USD does not cover estimated margin/fee buffer; fill=" + plan.id + " available=" + usd + " requiredApprox=" + reserve);
    return;
  }
  const clientId = "mfp-rev-" + crypto.createHash("sha256").update(String(plan.id)).digest("hex").slice(0, 24);
  const body = {
    product_id: PRODUCT_ID,
    size: close ? Math.min(FIXED_CONTRACTS, Math.abs(signedSize)) : FIXED_CONTRACTS,
    side: plan.plannedDeltaSide.toLowerCase(),
    order_type: "market_order",
    client_order_id: clientId
  };
  if (close) body.reduce_only = true;
  console.log("[DELTA ORDER REQUEST] " + JSON.stringify({
    sourceFillId: plan.id, sourceSide: plan.sourceSide,
    deltaSide: plan.plannedDeltaSide, product: PRODUCT_SYMBOL,
    contracts: body.size, reduceOnly: close, clientOrderId: clientId,
    approximateMarginUSD: marginEstimate, availableUSD: usd
  }));
  try {
    // A single POST only. A timeout/error is ambiguous: never automatically retry.
    const response = deltaResult(await deltaSigned("POST", "/v2/orders", body));
    console.log("[DELTA ORDER RESPONSE] " + JSON.stringify({
      sourceFillId: plan.id, success: true,
      orderId: first(response, ["id", "order_id", "orderId"]),
      state: first(response, ["state", "status", "order_status"]),
      side: first(response, ["side"]),
      size: first(response, ["size", "quantity"]),
      reduceOnly: close
    }));
  } catch (e) {
    console.error("[DELTA ORDER ERROR - DO NOT AUTO-RETRY] sourceFillId=" + plan.id + " error=" + String(e && e.message || e).slice(0, 1200));
    throw e;
  }
}
const server = createServer((req, res) => {
  if (req.url === "/health" || req.url === "/health/ready") {
    const body = {
      ok: true, service: SERVICE,
      mode: executionEnabled ? (liveReady ? "LIVE_ARMED" : "LIVE_BLOCKED") : "DRY_RUN",
      liveOrderExecution: executionEnabled,
      deltaLiveReady: liveReady,
      mfpKeyConfigured: Boolean(MFP_KEY),
      deltaKeyConfigured: Boolean(DELTA_KEY && DELTA_SECRET),
      accountConfigured: Boolean(ACCOUNT_ID),
      product: PRODUCT_SYMBOL,
      fixedDeltaContracts: FIXED_CONTRACTS,
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

async function pollOnce() {
  if (polling) return;
  polling = true;
  lastPollAt = new Date().toISOString();
  try {
    if (!MFP_KEY) throw new Error("MFP_API_KEY missing in copier service variables");
    if (!ACCOUNT_ID) throw new Error("MFP_ACCOUNT_ID missing in copier service variables");
    const response = await mfpGet("/positions?account_id=" + encodeURIComponent(ACCOUNT_ID) + "&status=open&limit=100");
    const current = buildSourcePositionSnapshot(response);
    // On the first poll after process start, live activation always refreshes the
    // baseline and sends no order for a pre-existing source position.
    if (executionEnabled && !liveActivationBaselineDone) {
      lastSourceSnapshot = current;
      sourceSnapshotInitialized = true;
      liveActivationBaselineDone = true;
      saveState();
      lastSuccessAt = new Date().toISOString();
      lastError = null;
      console.log("[LIVE ARM BASELINE] current source position recorded; no catch-up order sent; openGoldPositions=" +
        current.positionCount + " netSourceSize=" + current.signedSize +
        " netSide=" + (current.signedSize > 0 ? "LONG" : current.signedSize < 0 ? "SHORT" : "FLAT"));
      console.log("[POLL OK] mode=LIVE_ARMED source=positions deltaProduct=" + PRODUCT_SYMBOL + "; waiting for next source transition");
      return;
    }
    if (!sourceSnapshotInitialized || !lastSourceSnapshot) {
      lastSourceSnapshot = current;
      sourceSnapshotInitialized = true;
      saveState();
      lastSuccessAt = new Date().toISOString();
      lastError = null;
      console.log("[POSITION BASELINE] openGoldPositions=" + current.positionCount +
        " netSourceSize=" + current.signedSize +
        " netSide=" + (current.signedSize > 0 ? "LONG" : current.signedSize < 0 ? "SHORT" : "FLAT") +
        "; existing position not copied");
      console.log("[POLL OK] mode=DRY_RUN source=positions deltaProduct=" + PRODUCT_SYMBOL);
      return;
    }

    const previous = lastSourceSnapshot;
    const prevSize = Number(previous.signedSize || 0);
    const currSize = Number(current.signedSize || 0);
    const prevSign = Math.sign(prevSize);
    const currSign = Math.sign(currSize);

    if (prevSign === 0 && currSign !== 0) {
      eventCount++;
      const plan = positionPlan(current, "OPEN");
      console.log("[POSITION CHANGE] flat -> " + (currSign > 0 ? "LONG" : "SHORT") + " source net=" + currSize);
      await copyFill(null, plan);
    } else if (prevSign !== 0 && currSign === 0) {
      eventCount++;
      const plan = positionPlan(previous, "CLOSE");
      console.log("[POSITION CHANGE] " + (prevSign > 0 ? "LONG" : "SHORT") + " -> flat source");
      await copyFill(null, plan);
    } else if (prevSign !== 0 && currSign !== 0 && prevSign !== currSign) {
      eventCount++;
      const closePlan = positionPlan(previous, "CLOSE");
      const openPlan = positionPlan(current, "OPEN");
      console.log("[POSITION FLIP] source direction changed; dry-run emits close then opposite entry plans");
      await copyFill(null, closePlan);
      await copyFill(null, openPlan);
    } else if (prevSign === currSign && prevSign !== 0 && prevSize !== currSize) {
      console.log("[POSITION SIZE CHANGE] source net changed from " + prevSize + " to " + currSize +
        "; target remains fixed at " + FIXED_CONTRACTS + " contract and no extra entry is planned");
    }

    lastSourceSnapshot = current;
    saveState();
    lastSuccessAt = new Date().toISOString();
    lastError = null;
    console.log("[POLL OK] mode=DRY_RUN source=positions openGoldPositions=" +
      current.positionCount + " netSourceSize=" + currSize +
      " netSide=" + (currSign > 0 ? "LONG" : currSign < 0 ? "SHORT" : "FLAT") +
      " eventCount=" + eventCount + " deltaProduct=" + PRODUCT_SYMBOL);
  } catch (error) {
    lastError = String(error && error.message || error).slice(0, 1000);
    console.error("[POLL ERROR] " + lastError);
  } finally {
    polling = false;
  }
}

readState();
console.log("[START] " + JSON.stringify({
  service: SERVICE,
  source: "MFP XAUUSD 4TF fills",
  deltaEnvironment: "production",
  deltaBase: DELTA_BASE,
  productId: PRODUCT_ID,
  productSymbol: PRODUCT_SYMBOL,
  dryRun: DRY_RUN,
  liveOrderExecution: LIVE_EXECUTION,
  liveConfirmation: LIVE_CONFIRM,
  executionRequested,
  executionEnabled,
  pollIntervalMs: POLL_MS,
  hasMfpKey: Boolean(MFP_KEY),
  hasAccountId: Boolean(ACCOUNT_ID),
  hasDeltaKey: Boolean(DELTA_KEY && DELTA_SECRET),
  fixedDeltaContracts: FIXED_CONTRACTS,
  maxNetPositionContracts: 1,
  baselineInitialized,
  stateFile: STATE_FILE,
  productMappingConfigured: Boolean(process.env.DELTA_PRODUCT_MAP_JSON)
}));
console.log("[SAFETY] Position-transition dry-run: MFP LONG -> Delta SELL; MFP SHORT -> Delta BUY. Existing source position is baselined, never replayed. Fixed one Delta contract maximum. Live order submission is hard-disabled in this revision.");
if (executionEnabled && (!DELTA_KEY || !DELTA_SECRET)) {
  console.error("[LIVE BLOCKED] Add DELTA_API_KEY and DELTA_API_SECRET to this Railway service.");
}
await pollOnce();
setInterval(() => { pollOnce().catch(error => console.error("[UNHANDLED POLL]", error)); }, POLL_MS);
