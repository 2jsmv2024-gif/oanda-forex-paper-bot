import { MyFundedPerps, PriceStream } from "@myfundedperps/sdk";

const key = (process.env.MFP_API_KEY || "").trim();
const market = process.env.MFP_MARKET_ID || "hyperliquid|xyz:GOLD";
const qty = 0.001;
const leverage = +(process.env.MFP_LEVERAGE || "5");

const live = (process.env.LIVE_TRADING || "false").toLowerCase() === "true";
const dry = (process.env.DRY_RUN_ONLY || "true").toLowerCase() === "true";
const liveConfirm = (process.env.MFP_USER_LIVE_CONFIRMATION || "").trim() === "YES";
const executionEnabled = live && !dry && liveConfirm;

const buyAbove = +(process.env.BUY_ABOVE_PRICE || "0");
const sellBelow = +(process.env.SELL_BELOW_PRICE || "0");

const client = new MyFundedPerps({ apiKey: key });
let accountId = (process.env.MFP_ACCOUNT_ID || "").trim();

const unwrap = x => x?.data ?? x;
const pick = (x, keys) => {
  for (const k of keys) if (x?.[k] != null) return x[k];
  return null;
};

async function account() {
  if (accountId) return accountId;
  const x = unwrap(await client.listAccounts());
  const a = Array.isArray(x) ? x : x?.data || [];
  accountId = pick(a[0], ["account_id", "id", "accountId"]);
  if (!accountId) throw new Error("MFP account identifier unavailable");
  return accountId;
}

async function sendOrder(side, price) {
  console.log(`[SAMPLE SIGNAL] ${side} qty=0.001 price=${price}`);

  if (!executionEnabled) {
    console.log(`[SAMPLE DRY] order NOT submitted executionEnabled=${executionEnabled}`);
    return;
  }

  const result = await client.createOrder({
    account_id: await account(),
    market_id: market,
    side: side.toLowerCase(),
    type: "market",
    size: qty,
    leverage,
    margin_mode: "cross"
  });

  console.log(`[SAMPLE ORDER ACCEPTED] ${side} qty=0.001 price=${price}`);
  console.log(JSON.stringify(unwrap(result)));
}

if (!key) throw new Error("MFP_API_KEY missing");
if (!Number.isFinite(buyAbove) || !Number.isFinite(sellBelow)) {
  throw new Error("BUY_ABOVE_PRICE and SELL_BELOW_PRICE must be valid numbers");
}
if (buyAbove <= sellBelow) {
  throw new Error("BUY_ABOVE_PRICE must be greater than SELL_BELOW_PRICE");
}

console.log(`[SAMPLE BOT] market=${market} qty=0.001 buyAbove=${buyAbove} sellBelow=${sellBelow}`);
console.log(`[SAMPLE BOT] LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${liveConfirm} EXECUTION_ENABLED=${executionEnabled}`);

await account();
console.log("[SAMPLE AUTH] authenticated; account ready");

const mi = unwrap(await client.getMarket({ market_id: market }));
const streamSymbol = pick(mi, ["symbol", "stream_symbol", "market_symbol", "ticker", "name"]);
if (!streamSymbol) throw new Error("MFP stream symbol unavailable");
console.log("[SAMPLE MARKET] stream symbol=" + streamSymbol);

let buyDone = false;
let sellDone = false;
let previous = null;

while (true) {
  try {
    const stream = new PriceStream({ symbols: [streamSymbol] });
    console.log("[SAMPLE STREAM] GOLD connected");

    for await (const tick of stream) {
      const price = +pick(tick, ["price", "mid", "mark"]);
      if (!Number.isFinite(price)) continue;

      if (previous != null) {
        if (!buyDone && previous < buyAbove && price >= buyAbove) {
          buyDone = true;
          await sendOrder("BUY", price);
        }

        if (!sellDone && previous > sellBelow && price <= sellBelow) {
          sellDone = true;
          await sendOrder("SELL", price);
        }
      }

      previous = price;
      console.log(`[SAMPLE PRICE] ${price}`);
    }
  } catch (err) {
    console.error("[SAMPLE STREAM ERROR] " + (err?.message || err) + "; reconnecting in 3000ms");
    await new Promise(r => setTimeout(r, 3000));
  }
}
