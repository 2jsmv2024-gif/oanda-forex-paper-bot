// Compatibility shim for the SDK's order transport: if the SDK passes a plain
// object as fetch() body, force JSON serialization + the required content type.
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  if (init && init.body && typeof init.body === "object" &&
      !(init.body instanceof ArrayBuffer) &&
      !(ArrayBuffer.isView(init.body)) &&
      !(init.body instanceof URLSearchParams) &&
      !(typeof FormData !== "undefined" && init.body instanceof FormData) &&
      !(typeof Blob !== "undefined" && init.body instanceof Blob)) {
    const headers = new Headers(init.headers || {});
    if (!headers.has("content-type")) headers.set("content-type", "application/json");
    init = { ...init, headers, body: JSON.stringify(init.body) };
    console.log("[SAMPLE FETCH SHIM] serialized object request body as application/json");
  }
  return nativeFetch(input, init);
};

const { MyFundedPerps, PriceStream } = await import("@myfundedperps/sdk");

const key = (process.env.MFP_API_KEY || "").trim();
const market = process.env.MFP_MARKET_ID || "hyperliquid|xyz:GOLD";
const qty = 0.001;
const leverage = +(process.env.MFP_LEVERAGE || "5");

// 100 GOLD pips = $1.00 under our current test convention.
const slDistance = +(process.env.SL_DISTANCE || "1.0");

const live = (process.env.LIVE_TRADING || "false").toLowerCase() === "true";
const dry = (process.env.DRY_RUN_ONLY || "true").toLowerCase() === "true";
const liveConfirm = (process.env.MFP_USER_LIVE_CONFIRMATION || "").trim() === "YES";
const executionEnabled = live && !dry && liveConfirm;

const client = new MyFundedPerps({ apiKey: key });
let accountId = (process.env.MFP_ACCOUNT_ID || "").trim();

const unwrap = x => x?.data ?? x;
const pick = (x, keys) => {
  for (const k of keys) if (x?.[k] != null) return x[k];
  return null;
};

async function account() {
  // Resolve the actual authenticated account every time. This prevents a
  // stale Railway MFP_ACCOUNT_ID reference from breaking order/position calls.
  const x = unwrap(await client.listAccounts());
  const a = Array.isArray(x) ? x : x?.data || [];
  const configured = (process.env.MFP_ACCOUNT_ID || "").trim();

  if (configured) {
    const match = a.find(p =>
      String(pick(p, ["account_id", "id", "accountId"]) || "") === configured
    );
    if (match) {
      accountId = pick(match, ["account_id", "id", "accountId"]);
      return accountId;
    }
    console.warn("[SAMPLE ACCOUNT] configured MFP_ACCOUNT_ID not found; using first authenticated account");
  }

  accountId = pick(a[0], ["account_id", "id", "accountId"]);
  if (!accountId) throw new Error("MFP account identifier unavailable");
  return accountId;
}

async function positions() {
  const x = unwrap(await client.listOpenPositions({ account_id: await account() }));
  return Array.isArray(x) ? x : x?.data || [];
}

function isGoldPosition(p) {
  const m = String(pick(p, ["market_id", "marketId", "market", "symbol", "ticker"]) || "").toUpperCase();
  return m === String(market).toUpperCase() || m.includes("GOLD");
}

function extractPrice(x, depth = 0) {
  if (depth > 5 || x == null) return 0;
  if (typeof x === "number") return Number.isFinite(x) && x > 0 ? x : 0;
  if (typeof x === "string") {
    const n = +x;
    return Number.isFinite(n) && n > 0 ? n : 0;
  }
  if (typeof x !== "object") return 0;
  for (const k of ["price","mid","mark","mark_price","markPrice","last","last_price","lastPrice","value"]) {
    if (x[k] != null) {
      const n = +x[k];
      if (Number.isFinite(n) && n > 0) return n;
    }
  }
  for (const k of ["data","tick","ticker","payload","quote","market","result"]) {
    if (x[k] != null) {
      const n = extractPrice(x[k], depth + 1);
      if (n > 0) return n;
    }
  }
  return 0;
}

function extractTimestamp(x, depth = 0) {
  if (depth > 5 || x == null || typeof x !== "object") return 0;
  for (const k of ["timestamp","time","ts","t"]) {
    if (x[k] != null) {
      const n = +x[k];
      if (Number.isFinite(n) && n > 0) return n < 1e12 ? n * 1000 : n;
    }
  }
  for (const k of ["data","tick","ticker","payload","quote","market","result"]) {
    if (x[k] != null) {
      const n = extractTimestamp(x[k], depth + 1);
      if (n > 0) return n;
    }
  }
  return 0;
}

function entryPrice(p) {
  return +(pick(p, [
    "entry_price", "entryPrice", "avg_entry_price", "avgEntryPrice",
    "average_entry_price", "averageEntryPrice", "price"
  ]) || 0);
}

let armed = false;
let entry = null;
let positionId = null;
let stopTriggered = false;

async function sendImmediateBuy(currentPrice) {
  console.log(`[SAMPLE SIGNAL] IMMEDIATE BUY qty=0.001 currentPrice=${currentPrice} SL_DISTANCE=${slDistance}`);

  if (!executionEnabled) {
    console.log(`[SAMPLE DRY] order NOT submitted executionEnabled=${executionEnabled}`);
    return;
  }

  const result = unwrap(await client.createOrder({
    account_id: await account(),
    market_id: market,
    side: "buy",
    type: "market",
    size: qty,
    leverage,
    margin_mode: "cross"
  }));

  console.log("[SAMPLE ORDER ACCEPTED] BUY qty=0.001");
  console.log(JSON.stringify(result));

  positionId = pick(result, ["position_id", "positionId", "id"]) || null;
  entry = entryPrice(result) || currentPrice;
  armed = true;

  const sl = entry - slDistance;
  console.log(`[SAMPLE SL] entry=${entry} stop=${sl} distance=${slDistance}`);

  // If the order response does not include a position id/entry,
  // refresh from the account so the stop monitor is anchored to the actual position.
  try {
    const ps = await positions();
    const p = ps.find(isGoldPosition);
    if (p) {
      positionId = pick(p, ["position_id", "positionId", "id"]) || positionId;
      entry = entryPrice(p) || entry;
      console.log(`[SAMPLE POSITION] id=${positionId} entry=${entry}`);
      console.log(`[SAMPLE SL] active stop=${entry - slDistance}`);
    }
  } catch (e) {
    console.error("[SAMPLE POSITION LOOKUP ERROR] " + (e?.message || e));
  }
}

async function closeForStop(currentPrice) {
  if (stopTriggered || !armed || entry == null) return;

  const stop = entry - slDistance;
  if (currentPrice > stop) return;

  stopTriggered = true;
  console.log(`[SAMPLE STOP HIT] current=${currentPrice} stop=${stop}`);

  if (!executionEnabled) {
    console.log(`[SAMPLE DRY] stop close NOT submitted executionEnabled=${executionEnabled}`);
    return;
  }

  const ps = await positions();
  const p = ps.find(x =>
    (positionId && String(pick(x, ["position_id", "positionId", "id"])) === String(positionId)) ||
    isGoldPosition(x)
  );

  if (!p) {
    console.log("[SAMPLE STOP] no open GOLD position found");
    return;
  }

  const pid = pick(p, ["position_id", "positionId", "id"]);
  await client.closePosition({
    account_id: await account(),
    position_id: pid
  });

  console.log(`[SAMPLE STOP CLOSE ACCEPTED] position=${pid} current=${currentPrice} stop=${stop}`);
}

if (!Number.isFinite(slDistance) || slDistance <= 0) {
  throw new Error("Invalid SL_DISTANCE; expected positive price distance");
}

console.log(`[SAMPLE BOT] market=${market} qty=0.001 IMMEDIATE_BUY=true SL_DISTANCE=${slDistance}`);
console.log(`[SAMPLE BOT] LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${liveConfirm} EXECUTION_ENABLED=${executionEnabled}`);

await account();
console.log("[SAMPLE AUTH] authenticated; account ready");

const mi = unwrap(await client.getMarket({ market_id: market }));
const streamSymbol = pick(mi, ["symbol", "stream_symbol", "market_symbol", "ticker", "name"]);
if (!streamSymbol) throw new Error("MFP stream symbol unavailable");
console.log("[SAMPLE MARKET] stream symbol=" + streamSymbol);

while (true) {
  try {
    const stream = new PriceStream({ symbols: [streamSymbol] });
    console.log("[SAMPLE STREAM] GOLD connected");

    for await (const tick of stream) {
      const price = extractPrice(tick);
      const ts0 = extractTimestamp(tick) || Date.now();
      if (!Number.isFinite(price) || price <= 0) {
        console.log("[SAMPLE TICK UNPARSED] " + JSON.stringify(tick).slice(0, 2000));
        continue;
      }

      // On startup/reconnect, first recover an existing GOLD position.
      if (!armed && !stopTriggered) {
        const ps = await positions();
        const existing = ps.find(isGoldPosition);

        if (existing) {
          positionId = pick(existing, ["position_id", "positionId", "id"]);
          entry = entryPrice(existing) || price;
          armed = true;
          console.log(`[SAMPLE EXISTING POSITION] id=${positionId} entry=${entry} stop=${entry - slDistance}`);
        } else {
          // Immediate market BUY on the first valid live price.
          await sendImmediateBuy(price);
        }
      }

      if (armed) await closeForStop(price);
    }
  } catch (err) {
    console.error("[SAMPLE STREAM ERROR] " + (err?.message || err) + "; reconnecting in 3000ms");
    await new Promise(r => setTimeout(r, 3000));
  }
}
