const { MyFundedPerps, PriceStream } = await import("@myfundedperps/sdk");

const key = (process.env.MFP_API_KEY || "").trim();
const market = process.env.MFP_MARKET_ID || "hyperliquid|xyz:GOLD";
const qty = 0.003;
const leverage = +(process.env.MFP_LEVERAGE || "5");

// 100 GOLD pips = $1.00 under our current test convention.
const slDistance = +(process.env.SL_DISTANCE || "1.0");
const fixedSlPriceRaw = (process.env.FIXED_SL_PRICE || "").trim();
const fixedSlPrice = fixedSlPriceRaw ? +fixedSlPriceRaw : 0;
function activeStop() {
  return fixedSlPrice > 0 ? fixedSlPrice : (entry - slDistance);
}

const live = (process.env.LIVE_TRADING || "false").toLowerCase() === "true";
const dry = (process.env.DRY_RUN_ONLY || "true").toLowerCase() === "true";
const liveConfirm = (process.env.MFP_USER_LIVE_CONFIRMATION || "").trim() === "YES";
const executionEnabled = live && !dry && liveConfirm;
const cutoffIst = (process.env.SAMPLE_CUTOFF_IST || "02:55").trim();

function istMinutesNow() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false
  }).formatToParts(new Date());
  const h = +(parts.find(x => x.type === "hour")?.value || 0);
  const m = +(parts.find(x => x.type === "minute")?.value || 0);
  return h * 60 + m;
}

function entriesDisabledByCutoff() {
  const [h, m] = cutoffIst.split(":").map(Number);
  return Number.isFinite(h) && Number.isFinite(m) && istMinutesNow() >= h * 60 + m;
}


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
  if (!accountId) console.log("[SAMPLE ACCOUNTS] " + JSON.stringify(a).slice(0,6000));
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
let latestMarketPrice = 0;
let latestMarketPriceAt = 0;

async function sendImmediateBuy(currentPrice) {
  console.log(`[SAMPLE SIGNAL] IMMEDIATE BUY qty=${qty} currentPrice=${currentPrice} SL_DISTANCE=${slDistance}`);

  if (entriesDisabledByCutoff()) {
    console.log(`[SAMPLE CUTOFF] IST cutoff ${cutoffIst} reached; new entries disabled`);
    return;
  }

  if (!executionEnabled) {
    console.log(`[SAMPLE DRY] order NOT submitted executionEnabled=${executionEnabled}`);
    return;
  }

  let result;
  try {
    result = unwrap(await client.createOrder({ body: {
    account_id: await account(),
    market_id: market,
    side: "buy",
    type: "market",
    size: qty,
    leverage,
    margin_mode: "cross",
    expected_price: currentPrice
  }}));
  } catch (e) {
    console.error("[SAMPLE ORDER ERROR] message=" + (e?.message || e) + " status=" + (e?.status ?? "") + " code=" + String(e?.code ?? "") + " errorKeys=" + Object.keys(e?.error || {}).join(",") + " errorVals=" + Object.values(e?.error || {}).map(v => String(v)).join("|"));
    throw e;
  }

  console.log(`[SAMPLE ORDER ACCEPTED] BUY qty=${qty}`);
  console.log(JSON.stringify(result));

  positionId = pick(result, ["position_id", "positionId", "id"]) || null;
  entry = entryPrice(result) || currentPrice;
  armed = true;

  const sl = activeStop();
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
      console.log(`[SAMPLE SL] active stop=${activeStop()}`);
    }
  } catch (e) {
    console.error("[SAMPLE POSITION LOOKUP ERROR] " + (e?.message || e));
  }
}

async function closeForStop(currentPrice) {
  if (stopTriggered || !armed || entry == null) return;

  const stop = activeStop();
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
  const rawSize = pick(p, ["size", "quantity", "qty", "position_size", "positionSize"]);
  const closeSize = Math.abs(+(rawSize || qty));
  const posSide = String(pick(p, ["side", "position_side", "positionSide"]) || "long").toLowerCase();
  const closeSide = posSide.includes("short") ? "buy" : "sell";

  console.log(`[SAMPLE STOP CLOSE] reduce-only ${closeSide} qty=${closeSize} position=${pid} price=${currentPrice}`);

  try {
    const closeResult = unwrap(await client.createOrder({ body: {
      account_id: await account(),
      market_id: market,
      side: closeSide,
      type: "market",
      size: closeSize,
      leverage,
      margin_mode: "cross",
      reduce_only: true,
      expected_price: currentPrice
    }}));

    console.log(`[SAMPLE STOP CLOSE ACCEPTED] position=${pid} current=${currentPrice} stop=${stop}`);
    console.log("[SAMPLE STOP CLOSE RESULT] " + JSON.stringify(closeResult));
  } catch (e) {
    stopTriggered = false;
    console.error("[SAMPLE STOP CLOSE ERROR] message=" + (e?.message || e) +
      " status=" + (e?.status ?? "") +
      " code=" + String(e?.code ?? "") +
      " error=" + JSON.stringify(e?.error ?? null) +
      " details=" + JSON.stringify(e?.details ?? null));
    throw e;
  }
}

function positionMarkPrice(p) {
  return +(pick(p, [
    "mark_price", "markPrice", "mark", "current_price", "currentPrice",
    "last_price", "lastPrice", "price"
  ]) || 0);
}

async function monitorStopLoop() {
  console.log("[SAMPLE SL MONITOR] started; polling open GOLD position every 1000ms");
  let lastNoPosition = 0;
  while (true) {
    try {
      const ps = await positions();
      const p = ps.find(isGoldPosition);

      if (!p) {
        if (armed && !stopTriggered) {
          console.log("[SAMPLE SL MONITOR] GOLD position no longer open");
          armed = false;
        }
        if (Date.now() - lastNoPosition > 10000) {
          console.log("[SAMPLE SL MONITOR] no open GOLD position");
          lastNoPosition = Date.now();
        }
        if (entriesDisabledByCutoff()) {
          console.log(`[SAMPLE CUTOFF] no open position; exiting cleanly after ${cutoffIst} IST`);
          process.exit(0);
        }
        await new Promise(r => setTimeout(r, 1000));
        continue;
      }

      const pid = pick(p, ["position_id", "positionId", "id"]);
      const liveEntry = entryPrice(p);
      const streamPrice = latestMarketPrice;

      if (!armed || positionId !== pid) {
        positionId = pid;
        entry = liveEntry || entry;
        armed = true;
        stopTriggered = false;
        console.log(`[SAMPLE SL MONITOR] position=${pid} entry=${entry} stop=${activeStop()}`);
      }

      if (streamPrice > 0) {
        const stop = activeStop();
        console.log(`[SAMPLE SL MONITOR] streamPrice=${streamPrice} stop=${stop} ageMs=${Date.now() - latestMarketPriceAt}`);
        if (streamPrice <= stop) await closeForStop(streamPrice);
      } else {
        console.log("[SAMPLE SL MONITOR] waiting for GOLD PriceStream price");
      }

      await new Promise(r => setTimeout(r, 1000));
    } catch (e) {
      console.error("[SAMPLE SL MONITOR ERROR] " + (e?.message || e));
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}

if (fixedSlPrice <= 0 && (!Number.isFinite(slDistance) || slDistance <= 0)) {
  throw new Error("Invalid SL_DISTANCE; expected positive price distance");
}

console.log(`[SAMPLE BOT] market=${market} qty=${qty} IMMEDIATE_BUY=true SL_DISTANCE=${slDistance} FIXED_SL_PRICE=${fixedSlPrice || "none"} CUTOFF_IST=${cutoffIst}`);
console.log(`[SAMPLE BOT] LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${liveConfirm} EXECUTION_ENABLED=${executionEnabled}`);

await account();
console.log("[SAMPLE AUTH] authenticated; account ready");
monitorStopLoop().catch(e => console.error("[SAMPLE SL MONITOR FATAL] " + (e?.message || e)));

const mi = unwrap(await client.getMarket({ market_id: market }));
console.log("[SAMPLE MARKET META] " + JSON.stringify(mi).slice(0,4000));
const marketUiSymbol = pick(mi, ["symbol", "market_symbol", "ticker", "name"]);
const streamSymbol = pick(mi, ["stream_symbol", "streamSymbol", "price_stream_symbol", "priceStreamSymbol", "coin"]) || "xyz:GOLD";
if (!marketUiSymbol) throw new Error("MFP market metadata unavailable");
console.log("[SAMPLE MARKET] API symbol=" + marketUiSymbol + " PriceStream symbol=" + streamSymbol);

while (true) {
  try {
    const stream = new PriceStream({ symbols: [streamSymbol] });
    console.log("[SAMPLE STREAM] GOLD connected");

    let receivedTicks = 0;
    for await (const tick of stream) {
      receivedTicks++;
      const price = extractPrice(tick);
      const ts0 = extractTimestamp(tick) || Date.now();
      if (Number.isFinite(price) && price > 0) {
        latestMarketPrice = price;
        latestMarketPriceAt = Date.now();
      }

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
          console.log(`[SAMPLE EXISTING POSITION] id=${positionId} entry=${entry} stop=${activeStop()}`);
        } else if (entriesDisabledByCutoff()) {
          console.log(`[SAMPLE CUTOFF] ${cutoffIst} IST reached; no new BUY will be submitted`);
          // No new entry after cutoff. Keep the process alive only for monitoring/recovery.
          await new Promise(r => setTimeout(r, 5000));
        } else {
          // Immediate market BUY on the first valid live price.
          await sendImmediateBuy(price);
        }
      }

      if (armed) await closeForStop(price);

      // After cutoff, this test bot never opens another position. Once the
      // existing test position is closed, exit cleanly so Railway does not
      // restart it under the ON_FAILURE policy.
      if (entriesDisabledByCutoff() && !armed) {
        console.log(`[SAMPLE CUTOFF] no open position; exiting cleanly after ${cutoffIst} IST`);
        process.exit(0);
      }
    }
    console.log(`[SAMPLE STREAM] iterator ended; receivedTicks=${receivedTicks}; reconnecting`);
  } catch (err) {
    console.error("[SAMPLE STREAM ERROR] " + (err?.message || err) + "; reconnecting in 3000ms");
    await new Promise(r => setTimeout(r, 3000));
  }
}
