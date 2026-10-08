import {MyFundedPerps,PriceStream} from "@myfundedperps/sdk";

const key=(process.env.MFP_API_KEY||"").trim();
let market=process.env.MFP_MARKET_ID||"hyperliquid|xyz:GOLD";
let orderMarket=market;
const qty=+(process.env.MFP_BASE_QTY||"0.150");
const lev=+(process.env.MFP_LEVERAGE||"5");
const live=(process.env.LIVE_TRADING||"false").toLowerCase()==="true";
const dry=(process.env.DRY_RUN_ONLY||"true").toLowerCase()==="true";
const liveConfirm=(process.env.MFP_USER_LIVE_CONFIRMATION||"").trim()==="YES";
const executionEnabled=live&&!dry&&liveConfirm;

const client=new MyFundedPerps({apiKey:key});
let aid=(process.env.MFP_ACCOUNT_ID||"").trim();
let raw=[],cur=null,sk=null;
const positionsBySystem=new Map();
const lastSignal=new Map();
const FILL_CONFIRM_MS=15000;
const FILL_POLL_MS=500;
const ORDER_COOLDOWN_RETRY_MS=5000;
const ORDER_COOLDOWN_RETRIES=3;
const PORTFOLIO_TOTAL_MARGIN=+(process.env.MFP_BASE_BALANCE||"500000");
const PORTFOLIO_MARGIN_PER_TF=+(process.env.MFP_BASE_SIZE_ANCHOR||String(PORTFOLIO_TOTAL_MARGIN/2));
const PORTFOLIO_MAX_POSITIONS=2;

const SYSTEMS=[
  {id:"V3-S1-2M",version:"V3",session:"S1",tf:2},
  {id:"V2-S1-3M",version:"V2",session:"S1",tf:3},
  {id:"V3-S3-4M",version:"V3",session:"S3",tf:4},
  {id:"V2-S1-5M",version:"V2",session:"S1",tf:5},
];

const SES={S1:["03:15","08:15"],S2:["10:15","14:15"],S3:["16:15","21:15"]};
const v=(x,a)=>{for(const k of a)if(x?.[k]!=null)return x[k];return null};
const u=x=>x?.data??x;

function ist(t){const d=new Date(t+19800000);return {d:d.toISOString().slice(0,10),m:d.getUTCHours()*60+d.getUTCMinutes()}}
function sess(t){
  const p=ist(t);
  for(const [s,[a,b]] of Object.entries(SES)){
    const A=a.split(":").map(Number),B=b.split(":").map(Number);
    if(p.m>=A[0]*60+A[1]&&p.m<B[0]*60+B[1])return{s,k:p.d+"|"+s};
  }
}

function agg(tf,sessionName){
  const by=new Map();
  for(const r of raw){
    const ss=sess(r.ts);
    if(!ss||ss.s!==sessionName)continue;
    const k=Math.floor(r.ts/60000/tf)*tf;
    if(!by.has(k))by.set(k,[]);
    by.get(k).push(r);
  }
  return [...by].sort((a,b)=>a[0]-b[0]).map(([,x])=>({
    ts:x[0].ts,open:x[0].open,high:Math.max(...x.map(r=>r.high)),
    low:Math.min(...x.map(r=>r.low)),close:x.at(-1).close
  }));
}

function aggFull(tf){
  const by=new Map();
  for(const r of raw){
    const k=Math.floor(r.ts/60000/tf)*tf;
    if(!by.has(k))by.set(k,[]);
    by.get(k).push(r);
  }
  return [...by].sort((a,b)=>a[0]-b[0]).map(([,x])=>({
    ts:x[0].ts,open:x[0].open,high:Math.max(...x.map(r=>r.high)),
    low:Math.min(...x.map(r=>r.low)),close:x.at(-1).close
  }));
}

function ha(a){
  const z=[];let o,c;
  for(const r of a){
    const hc=(r.open+r.high+r.low+r.close)/4;
    const ho=o==null?(r.open+r.close)/2:(o+c)/2;
    z.push({...r,ha_open:ho,ha_close:hc,
      ha_high:Math.max(r.high,ho,hc),ha_low:Math.min(r.low,ho,hc)});
    o=ho;c=hc;
  }
  return z;
}

function dataFor(sys){
  if(sys.version==="V2"){
    return ha(agg(sys.tf,sys.session));
  }
  return ha(aggFull(sys.tf)).filter(x=>sess(x.ts)?.s===sys.session);
}

function events(a){
  const e=[];let side=null,reference=null;
  for(let i=2;i<a.length;i++){
    const c2=a[i-2],c1=a[i-1],c0=a[i];
    const buy=c2.ha_close>c2.ha_open&&c1.ha_open<c1.ha_high;
    const sell=c2.ha_close<c2.ha_open&&c1.ha_open>c1.ha_low;

    if(side==="BUY"&&sell){
      e.push({t:c0.ts,s:"SELL",r:"OPPOSITE_SIGNAL"});
      side="SELL";reference=c1.ha_high;continue;
    }
    if(side==="SELL"&&buy){
      e.push({t:c0.ts,s:"BUY",r:"OPPOSITE_SIGNAL"});
      side="BUY";reference=c1.ha_low;continue;
    }
    if(side===null){
      if(buy){e.push({t:c0.ts,s:"BUY",r:"NEW_BUY_SIGNAL"});side="BUY";reference=c1.ha_low;continue}
      if(sell){e.push({t:c0.ts,s:"SELL",r:"NEW_SELL_SIGNAL"});side="SELL";reference=c1.ha_high;continue}
    }
    if(side==="BUY"){
      if(c0.ha_low<reference){
        e.push({t:c0.ts,s:"SELL",r:"REFERENCE_LOW_BREAK"});
        side="SELL";reference=c0.ha_high;
      }else reference=c0.ha_low;
    }else if(side==="SELL"){
      if(c0.ha_high>reference){
        e.push({t:c0.ts,s:"BUY",r:"REFERENCE_HIGH_BREAK"});
        side="BUY";reference=c0.ha_low;
      }else reference=c0.ha_high;
    }
  }
  return e;
}

async function account(){
  const x=u(await client.listAccounts()),a=Array.isArray(x)?x:x?.data||[];
  if(!a.length)throw Error("MFP authenticated account unavailable");
  const configured=(process.env.MFP_ACCOUNT_ID||"").trim();
  const match=configured?a.find(z=>String(v(z,["account_id","id","accountId"]))===configured):null;
  const chosen=match||a[0];
  aid=v(chosen,["account_id","id","accountId"]);
  if(!aid)throw Error("MFP account identifier unavailable");
  if(configured&&!match)console.log("[MFP ACCOUNT] configured account not returned by API; using authenticated account");
  return aid;
}

async function portfolioOrderSize(accountId,sys,side){
  // FIX: use the configured GOLD quantity instead of deriving a huge
  // margin-based size from the competition balance. The previous calculation
  // produced 121-607+ GOLD orders and those were rejected by MFP trading rules.
  // Keep the strategy/signals unchanged; only execution sizing is corrected.
  const orderQty=qty;
  if(!Number.isFinite(orderQty)||orderQty<=0){
    console.warn(`[MFP QTY BLOCK] ${sys.id} ${side} invalid MFP_BASE_QTY=${orderQty}`);
    return null;
  }
  console.log(`[MFP FIXED QTY] ${sys.id} ${side} qty=${orderQty} leverage=${lev}`);
  return orderQty;
}
async function open(sys,side,t){
  // FIRE IMMEDIATELY: createOrder() is the first broker call after a valid
  // signal. Do not wait for market metadata probes here; those probes can be
  // stale and were adding avoidable latency before the actual order request.
  if(!executionEnabled){
    console.log(`[MFP DRY SIGNAL] ${sys.id} ${side} executionEnabled=${executionEnabled}`);
    return null;
  }

  const accountId=await account();
  // GOLD execution uses the configured fixed quantity. Do not replace MFP_BASE_QTY with portfolio-margin sizing.
  // Current MFP GOLD exposure is small enough that the old 120+ GOLD sizing was rejected by market limits.
  const orderQty=qty;
  console.log(`[MFP FIXED QTY] ${sys.id} ${side} qty=${orderQty} leverage=${lev}`);
  const expected=cur?.close||undefined;
  let r;
  let attempt=0;
  while(true){
    try{
      r=await client.createOrder({body:{
        account_id:accountId,market_id:orderMarket,side:side.toLowerCase(),
        type:"market",size:orderQty,leverage:lev,margin_mode:"isolated",
        expected_price:expected
      }});
      break;
    }catch(e){
      const code=String(e?.code??"").toLowerCase();
      const msg=String(e?.message||e);
      if(code==="cooldown" && attempt<ORDER_COOLDOWN_RETRIES){
        attempt++;
        console.warn(`[MFP ORDER COOLDOWN] ${sys.id} ${side} attempt=${attempt}/${ORDER_COOLDOWN_RETRIES}; waiting ${ORDER_COOLDOWN_RETRY_MS}ms before retry`);
        await new Promise(r=>setTimeout(r,ORDER_COOLDOWN_RETRY_MS));
        continue;
      }
      console.error(`[MFP ORDER ERROR] ${sys.id} ${side} message=${msg} status=${e?.status??""} code=${e?.code??""}`);
      throw e;
    }
  }

  const order=u(r);
  const orderId=v(order,["order_id","orderId","id"]);
  const orderStatus=v(order,["status","order_status","orderStatus"]);
  const filled=v(order,["filled_size","filledSize","filled_quantity","filledQuantity","executed_size","executedSize"]);
  console.log(`[MFP ORDER REQUESTED] ${sys.id} ${side} qty=${orderQty} expected=${expected??""} orderId=${orderId??""} status=${orderStatus??""} filled=${filled??""}`);
  console.log("[MFP ORDER RESPONSE] "+JSON.stringify(order).slice(0,5000));

  // createOrder() is only a request acknowledgement. Do not treat it as a
  // filled position. Confirm the actual account position before arming the
  // strategy; otherwise one unfilled request can permanently block later
  // signals for this system.
  const deadline=Date.now()+FILL_CONFIRM_MS;
  while(Date.now()<deadline){
    try{
      const ps=u(await client.listOpenPositions({account_id:accountId}));
      const arr=Array.isArray(ps)?ps:ps?.data||[];
      const p=arr.find(x=>{
        const m=String(v(x,["market_id","marketId","market","symbol","ticker"])||"").toUpperCase();
        const pside=String(v(x,["side","position_side","positionSide"])||"").toLowerCase();
        const isBuyPosition=pside.includes("long")||pside.includes("buy");
        const isSellPosition=pside.includes("short")||pside.includes("sell");
        return (m===String(market).toUpperCase()||m.includes("GOLD")) &&
          ((side==="BUY"&&isBuyPosition)||(side==="SELL"&&isSellPosition));
      });
      if(p){
        const pid=v(p,["position_id","positionId","id"]);
        const psize=Math.abs(+(v(p,["size","quantity","qty","position_size","positionSize"])||0));
        console.log(`[MFP FILL CONFIRMED] ${sys.id} ${side} position=${pid??""} size=${psize} orderId=${orderId??""}`);
        return {...order,position_id:pid,filled_size:psize};
      }
    }catch(e){
      console.error(`[MFP FILL CHECK ERROR] ${sys.id} ${e?.message||e}`);
    }
    await new Promise(r=>setTimeout(r,FILL_POLL_MS));
  }

  console.warn(`[MFP NOT FILLED] ${sys.id} ${side} qty=${orderQty} orderId=${orderId??""} afterMs=${FILL_CONFIRM_MS}; no position armed`);
  return null;
}

async function closeSystem(sys,side){
  if(!executionEnabled)return;
  const tracked=positionsBySystem.get(sys.id);
  if(!tracked?.id)return;
  const ps=u(await client.listOpenPositions({account_id:await account()}));
  const arr=Array.isArray(ps)?ps:ps?.data||[];
  const p=arr.find(x=>String(v(x,["position_id","id","positionId"]))===String(tracked.id));
  if(p){
    const pside=String(v(p,["side","position_side","positionSide"])||"").toLowerCase();
    const closeSide=pside.includes("short")?"buy":"sell";
    const size=Math.abs(+(v(p,["size","quantity","qty","position_size","positionSize"])||0));
    if(size){
      await client.createOrder({body:{
        account_id:await account(),market_id:orderMarket,side:closeSide,
        type:"market",size,leverage:lev,margin_mode:"isolated",reduce_only:true
      }});
    }
  }
  positionsBySystem.delete(sys.id);
  console.log(`[MFP CLOSE] ${sys.id} ${side}`);
}

async function handle(sys,event){
  const key=sys.id+"|"+event.t+"|"+event.s+"|"+event.r;
  if(lastSignal.get(sys.id)===key)return;
  lastSignal.set(sys.id,key);
  console.log(`[MFP SIGNAL] ${sys.id} ${sys.version} ${sys.session} ${sys.tf}M ${event.s} ${event.r}`);

  const current=positionsBySystem.get(sys.id);
  if(current&&current.side!==event.s){
    await closeSystem(sys,event.s);
    const x=await open(sys,event.s,event.t);
    if(x)positionsBySystem.set(sys.id,{side:event.s,id:v(x,["position_id","id","positionId"])});
    return;
  }
  if(current)return;

  const x=await open(sys,event.s,event.t);
  if(x)positionsBySystem.set(sys.id,{side:event.s,id:v(x,["position_id","id","positionId"])});
}

function processMinute(c){
  const ss=sess(c.ts);
  if(!cur||Math.floor(c.ts/60000)!==Math.floor(cur.ts/60000)){
    if(cur)raw.push(cur);
    raw=raw.slice(-10000);
    cur={...c};
    if(!ss)return;

    for(const sys of SYSTEMS){
      if(sys.session!==ss.s)continue;
      const a=dataFor(sys);
      if(a.length<3)continue;
      const e=events(a).at(-1);
      if(!e)continue;
      if(e.t!==a.at(-1).ts)continue;
      handle(sys,e).catch(x=>console.error("[MFP EXEC ERROR]",sys.id,x?.message||x));
    }
    return;
  }
  cur.high=Math.max(cur.high,c.high);
  cur.low=Math.min(cur.low,c.low);
  cur.close=c.close;
}

if(!key)throw Error("MFP_API_KEY missing");
console.log("[MFP 4TF STRATEGY] V3-S1-2M | V2-S1-3M | V3-S3-4M | V2-S1-5M");
console.log(`[MFP EXECUTOR] MARKET=${market} LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${liveConfirm} EXECUTION_ENABLED=${executionEnabled}`);

await account();
console.log("[MFP AUTH] authenticated; account ready");

const marketCandidates=[
  market,
  "binance|XAU",
  "binance|XAU-USD",
  "binance|XAUUSD",
  "XAU-USD",
  "hyperliquid|xyz:GOLD"
];
let mi=null;
let firstMarket=null;
for(const candidate of [...new Set(marketCandidates)]){
  try{
    const test=u(await client.getMarket({market_id:candidate}));
    if(!test)continue;
    console.log("[MFP MARKET PROBE] "+candidate+" trading_enabled="+test?.trading_enabled+" reduce_only="+test?.reduce_only+" coin="+(v(test,["coin","stream_symbol","symbol","market_symbol","ticker","name"])||""));
    if(!firstMarket)firstMarket={id:candidate,meta:test};
    if(test?.trading_enabled===true && test?.reduce_only!==true){
      market=candidate;
      mi=test;
      break;
    }
  }catch(e){
    console.warn("[MFP MARKET PROBE] "+candidate+" -> "+(e?.code||e?.message||e));
  }
}
if(!mi){
  if(firstMarket){
    market=firstMarket.id;
    mi=firstMarket.meta;
    console.error("[MFP MARKET NO TRADEABLE CANDIDATE] selected="+market+" trading_enabled="+mi?.trading_enabled+" reduce_only="+mi?.reduce_only);
  }else{
    throw Error("XAU market could not be resolved");
  }
}
orderMarket=v(mi,["market_id","marketId","id"])||market;
console.log("[MFP MARKET META] "+JSON.stringify(mi).slice(0,5000));
console.log("[MFP ORDER MARKET] "+orderMarket);
const streamSymbol=v(mi,["coin","stream_symbol","symbol","market_symbol","ticker","name"]);
if(!streamSymbol)throw Error("MFP stream symbol unavailable");
console.log("[MFP MARKET] market_id="+market+" stream symbol="+streamSymbol);

while(true){
  try{
    const stream=new PriceStream({symbols:[streamSymbol]});
    console.log("[MFP STREAM] GOLD connected");
    for await(const t of stream){
      const p=+v(t,["price","mid","mark"]);
      if(!Number.isFinite(p))continue;
      const ts=+v(t,["timestamp","time","ts"])||Date.now();
      const m=Math.floor(ts/60000)*60000;
      if(!cur||cur.ts!==m)processMinute({ts:m,open:p,high:p,low:p,close:p});
      else processMinute({ts:m,open:cur.open,high:Math.max(cur.high,p),low:Math.min(cur.low,p),close:p});
    }
    console.error("[MFP STREAM END] reconnecting in 3000ms");
  }catch(x){
    console.error("[MFP STREAM ERROR] "+(x?.message||x)+"; reconnecting in 3000ms");
  }
  await new Promise(r=>setTimeout(r,3000));
}
