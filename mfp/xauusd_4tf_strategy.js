import {PriceStream} from "@myfundedperps/sdk";
import {createServer} from "node:http";

// MFP REST executor; same frozen BTC signal conditions, using the XAU-USD market feed.

const key=(process.env.MFP_API_KEY||"").trim();
let market=process.env.MFP_MARKET_ID||"hyperliquid|xyz:GOLD";
let orderMarket=market;
const qty=+(process.env.MFP_BASE_QTY||"0.150");
const lev=+(process.env.MFP_LEVERAGE||"5");
const live=(process.env.LIVE_TRADING||"false").toLowerCase()==="true";
const dry=(process.env.DRY_RUN_ONLY||"true").toLowerCase()==="true";
const liveConfirm=(process.env.MFP_USER_LIVE_CONFIRMATION||"").trim()==="YES";
const executionEnabled=live&&!dry&&liveConfirm;

const REST_BASE=(process.env.MFP_REST_BASE_URL||"https://developers.myfundedperpetuals.com/v1").replace(/\/$/,"");
async function rest(method,path,body,idem){
 const headers={Authorization:"Bearer "+key,Accept:"application/json"};
 if(body!==undefined)headers["Content-Type"]="application/json";
 if(idem)headers["Idempotency-Key"]=idem;
 const response=await fetch(REST_BASE+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
 const rawText=await response.text();let payload={};try{payload=rawText?JSON.parse(rawText):{}}catch{payload={raw:rawText.slice(0,1000)}}
 if(!response.ok){const e=new Error("MFP REST "+method+" "+path+" -> HTTP "+response.status+": "+JSON.stringify(payload).slice(0,2000));e.status=response.status;e.payload=payload;throw e}
 return payload;
}
const listAccounts=()=>rest("GET","/accounts");
const getMarket=id=>rest("GET","/markets/"+encodeURIComponent(id));
const listPositions=id=>rest("GET","/positions?account_id="+encodeURIComponent(id)+"&status=open&limit=100");
const placeOrder=(body,idem)=>rest("POST","/orders",body,idem);
const closePosition=(id,body,idem)=>rest("POST","/positions/"+encodeURIComponent(id)+"/close",body,idem);
let aid=(process.env.MFP_ACCOUNT_ID||"").trim();
let raw=[],cur=null,sk=null;
const positionsBySystem=new Map();
const lastSignal=new Map();

const FILL_CONFIRM_MS=15000;
const FILL_POLL_MS=500;
const ORDER_COOLDOWN_RETRY_MS=5000;
const ORDER_COOLDOWN_RETRIES=3;
const MAX_PAIR_EXPOSURE_USD=+(process.env.MFP_MAX_PAIR_EXPOSURE_USD||"500000");
const PORTFOLIO_TOTAL_MARGIN=+(process.env.MFP_BASE_BALANCE||"500000");
const PORTFOLIO_MARGIN_PER_TF=+(process.env.MFP_BASE_SIZE_ANCHOR||String(PORTFOLIO_TOTAL_MARGIN/2));
const PORTFOLIO_MAX_POSITIONS=2;

// Railway health endpoint: report process health separately from trading readiness.
let marketReady=false;
let feedReady=false;
let lastHealthTickAt=0;
const healthServer=createServer((req,res)=>{
  if(req.url==="/health" || req.url==="/health/ready"){
    const body=JSON.stringify({ok:true,service:"mfp-xauusd-4tf",marketReady,feedReady,lastTickAt:lastHealthTickAt||null,executionEnabled});
    res.writeHead(200,{"content-type":"application/json","cache-control":"no-store"});res.end(body);return;
  }
  res.writeHead(404,{"content-type":"application/json"});res.end(JSON.stringify({error:"not_found"}));
});
healthServer.listen(+(process.env.PORT||8080),"0.0.0.0",()=>console.log("[HEALTH] listening port="+(process.env.PORT||8080)));

const SYSTEMS=[
  {id:"V3-S1-11M",version:"V3",session:"S1",tf:11},
  {id:"V3-S1-10M",version:"V3",session:"S1",tf:10},
  {id:"V2-S1-13M",version:"V2",session:"S1",tf:13},
  {id:"5759-OPP-S2-11M",version:"57-59-OPPOSITE",session:"S2",tf:11},
  {id:"5759-OPP-S2-13M",version:"57-59-OPPOSITE",session:"S2",tf:13},
  {id:"5759-OPP-S2-7M",version:"57-59-OPPOSITE",session:"S2",tf:7},
  {id:"5759-OPP-S3-15M",version:"57-59-OPPOSITE",session:"S3",tf:15},
  {id:"V2-S3-10M",version:"V2",session:"S3",tf:10},
  {id:"V2-S3-9M",version:"V2",session:"S3",tf:9},
];

const SES={S1:["23:45","08:15"],S2:["10:15","14:15"],S3:["16:15","21:15"]};
const v=(x,a)=>{for(const k of a)if(x?.[k]!=null)return x[k];return null};
const u=x=>x?.data??x;
function orderErrorDetails(e){const out={message:e?.message??String(e),code:e?.code??null,status:e?.status??e?.statusCode??null,type:e?.type??null,details:e?.details??null,response:e?.response?.data??e?.response??null,body:e?.body??null};try{return JSON.stringify(out).slice(0,8000)}catch{return String(e?.message??e)}}

function ist(t){const d=new Date(t+19800000);return {d:d.toISOString().slice(0,10),m:d.getUTCHours()*60+d.getUTCMinutes()}}
function sess(t){
  const p=ist(t);
  for(const [s,[a,b]] of Object.entries(SES)){
    const A=a.split(":").map(Number),B=b.split(":").map(Number);
    const start=A[0]*60+A[1],end=B[0]*60+B[1];
    const inside=start<=end?(p.m>=start&&p.m<end):(p.m>=start||p.m<end);
    if(inside)return{s,k:p.d+"|"+s};
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
  // Match the saved backtest's engine-specific pipeline.
  if(sys.version==="V2"){
    const sessionRaw=raw.filter(r=>sess(r.ts)?.s===sys.session);
    const by=new Map();
    for(const r of sessionRaw){
      const k=Math.floor(r.ts/60000/sys.tf)*sys.tf;
      if(!by.has(k))by.set(k,[]);
      by.get(k).push(r);
    }
    const bars=[...by].sort((a,b)=>a[0]-b[0]).map(([,x])=>({
      ts:x[0].ts,open:x[0].open,high:Math.max(...x.map(r=>r.high)),
      low:Math.min(...x.map(r=>r.low)),close:x.at(-1).close
    }));
    return ha(bars);
  }
  return ha(aggFull(sys.tf)).filter(x=>sess(x.ts)?.s===sys.session);
}

function events(a,version){
  // V2/V3 use the frozen base signal; OPPOSITE reverses entry signals.
  const e=[];let side=null,reference=null;
  const opposite=version==="57-59-OPPOSITE";
  for(let i=2;i<a.length;i++){
    const c2=a[i-2],c1=a[i-1],c0=a[i];
    const baseBuy=(c2.ha_close>c2.ha_open)&&(c1.ha_open<c1.ha_high);
    const baseSell=(c2.ha_close<c2.ha_open)&&(c1.ha_open>c1.ha_low);
    const buySignal=opposite?baseSell:baseBuy;
    const sellSignal=opposite?baseBuy:baseSell;
    if(side==="BUY"&&sellSignal){
      e.push({t:c0.ts,s:"SELL",r:"OPPOSITE_SIGNAL"});
      side="SELL";reference=c1.ha_high;continue;
    }
    if(side==="SELL"&&buySignal){
      e.push({t:c0.ts,s:"BUY",r:"OPPOSITE_SIGNAL"});
      side="BUY";reference=c1.ha_low;continue;
    }
    if(side===null){
      if(buySignal){
        e.push({t:c0.ts,s:"BUY",r:"NEW_BUY_SIGNAL"});
        side="BUY";reference=c1.ha_low;continue;
      }
      if(sellSignal){
        e.push({t:c0.ts,s:"SELL",r:"NEW_SELL_SIGNAL"});
        side="SELL";reference=c1.ha_high;continue;
      }
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
  const x=u(await listAccounts());
  const a=Array.isArray(x)?x:x?.data||[];
  if(!a.length)throw Error("MFP authenticated account unavailable");
  const configured=(process.env.MFP_ACCOUNT_ID||"").trim();
  const ids=a.map(z=>String(v(z,["account_id","id","accountId"]))).filter(Boolean);
  const match=configured?a.find(z=>String(v(z,["account_id","id","accountId"]))===configured):null;
  if(match){
    aid=v(match,["account_id","id","accountId"]);
    if(!aid)throw Error("MFP account identifier unavailable");
    console.log("[MFP ACCOUNT] configured account matched: "+aid);
    return aid;
  }
  if(a.length===1){
    aid=v(a[0],["account_id","id","accountId"]);
    if(!aid)throw Error("MFP account identifier unavailable");
    console.warn("[MFP ACCOUNT] configured account "+(configured||"<missing>")+" not returned; using the only authenticated account returned by API: "+aid);
    return aid;
  }
  throw Error("MFP account mismatch: configured="+(configured||"<missing>")+" returned=["+ids.join(",")+"] ; refusing ambiguous account selection");
}
async function portfolioOrderSize(accountId,sys,side){
  // FIX: use the configured XAU-USD quantity instead of deriving a huge
  // margin-based size from the competition balance. The previous calculation
  // produced 121-607+ XAU-USD orders and those were rejected by MFP trading rules.
  // Keep the strategy/signals unchanged; only execution sizing is corrected.
  const orderQty=qty;
  if(!Number.isFinite(orderQty)||orderQty<=0){
    console.warn(`[MFP QTY BLOCK] ${sys.id} ${side} invalid MFP_BASE_QTY=${orderQty}`);
    return null;
  }
  console.log(`[MFP FIXED QTY] ${sys.id} ${side} qty=${orderQty} leverage=${lev}`);
  return orderQty;
}
let orderQueue=Promise.resolve();
function open(sys,side,t){
  const run=()=>openUnlocked(sys,side,t);
  const next=orderQueue.then(run,run);
  orderQueue=next.catch(()=>{});
  return next;
}

async function openUnlocked(sys,side,t){
  // FIRE IMMEDIATELY: createOrder() is the first broker execution call after
  // a valid signal. Quantity may be reduced to the market minimum only when
  // the configured quantity is invalid; never block a valid signal merely
  // because the preferred size is unavailable.
  if(!executionEnabled || !marketReady || !mi){
    console.log(`[MFP EXECUTION BLOCK] ${sys.id} ${side} executionEnabled=${executionEnabled} marketReady=${marketReady} marketMetadata=${!!mi}`);
    return null;
  }

  const accountId=await account();
  // Enforce the user's hard per-market notional ceiling before every entry.
  // Serialize entry requests so simultaneous timeframe signals cannot all pass
  // the same stale exposure check.
  if(!Number.isFinite(MAX_PAIR_EXPOSURE_USD)||MAX_PAIR_EXPOSURE_USD<=0){
    console.error(`[MFP EXPOSURE BLOCK] invalid MFP_MAX_PAIR_EXPOSURE_USD=${MAX_PAIR_EXPOSURE_USD}`);
    return null;
  }
  const marketMin=Math.max(0.0001,+(v(mi||{},["min_size","minSize"])||0.0001));
  const orderQty=(Number.isFinite(qty)&&qty>0)?qty:marketMin;
  const price=+(cur?.close||0);
  if(!Number.isFinite(price)||price<=0){
    console.error(`[MFP EXPOSURE BLOCK] ${sys.id} no valid current price; refusing entry`);
    return null;
  }
  const ps=u(await listPositions(accountId));
  const openPositions=Array.isArray(ps)?ps:(ps?.data||[]);
  let currentExposure=0;
  for(const p of openPositions){
    const pm=String(v(p,["market_id","marketId","market","symbol","ticker","coin"])||"").toUpperCase();
    const sameMarket=pm===String(orderMarket).toUpperCase()
      || pm===String(market).toUpperCase()
      || (pm.includes("GOLD") && String(market).toUpperCase().includes("GOLD"));
    if(!sameMarket)continue;
    const directNotional=+(v(p,["notional_usd","notionalUsd","value_usd","valueUsd","position_value","positionValue","market_value","marketValue"])||0);
    const size=Math.abs(+(v(p,["size","quantity","qty","position_size","positionSize"])||0));
    const pPrice=+(v(p,["mark_price","markPrice","price","entry_price","entryPrice"])||price);
    const notional=directNotional>0?directNotional:size*pPrice;
    if(!Number.isFinite(notional)||notional<0 || (size>0 && notional===0)){
      console.error(`[MFP EXPOSURE BLOCK] ${sys.id} cannot reliably value an existing XAU-USD position; refusing new exposure`);
      return null;
    }
    currentExposure+=notional;
  }
  const newNotional=orderQty*price*+(v(mi||{},["contract_size","contractSize"])||1);
  const projectedExposure=currentExposure+newNotional;
  console.log(`[MFP EXPOSURE CHECK] ${sys.id} market=${orderMarket} existingUsd=${currentExposure.toFixed(2)} newOrderUsd=${newNotional.toFixed(2)} projectedUsd=${projectedExposure.toFixed(2)} capUsd=${MAX_PAIR_EXPOSURE_USD.toFixed(2)}`);
  if(projectedExposure>MAX_PAIR_EXPOSURE_USD){
    console.error(`[MFP EXPOSURE BLOCK] ${sys.id} projected exposure ${projectedExposure.toFixed(2)} exceeds cap ${MAX_PAIR_EXPOSURE_USD.toFixed(2)}; no order sent`);
    return null;
  }
  console.log(`[MFP FIXED QTY] ${sys.id} ${side} qty=${orderQty} leverage=${lev}`);
  const expected=price;
  let r;
  let attempt=0;
  while(true){
    try{
      const clientOrderId=("xau4tf:"+sys.id+":"+t+":"+side).slice(0,128);
      r=await placeOrder({client_order_id:clientOrderId,account_id:accountId,market_id:orderMarket,side:side.toLowerCase(),type:"market",size:orderQty,leverage:lev,margin_mode:"isolated",expected_price:expected},clientOrderId);
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
      const ps=u(await listPositions(accountId));
      const arr=Array.isArray(ps)?ps:ps?.data||[];
      const p=arr.find(x=>{
        const m=String(v(x,["market_id","marketId","market","symbol","ticker"])||"").toUpperCase();
        const pside=String(v(x,["side","position_side","positionSide"])||"").toLowerCase();
        const isBuyPosition=pside.includes("long")||pside.includes("buy");
        const isSellPosition=pside.includes("short")||pside.includes("sell");
        return (m===String(market).toUpperCase()||m.includes("BTC")||m.includes("xyz:GOLD")) &&
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
  const ps=u(await listPositions(await account()));
  const arr=Array.isArray(ps)?ps:ps?.data||[];
  const p=arr.find(x=>String(v(x,["position_id","id","positionId"]))===String(tracked.id));
  if(p){
    const pside=String(v(p,["side","position_side","positionSide"])||"").toLowerCase();
    const closeSide=pside.includes("short")?"buy":"sell";
    const size=Math.abs(+(v(p,["size","quantity","qty","position_size","positionSize"])||0));
    if(size){
      const closeKey=("xau4tf-close:"+sys.id+":"+Date.now()).slice(0,128);
      await closePosition(tracked.id,{client_order_id:closeKey,type:"market",size,expected_price:cur?.close||undefined},closeKey);
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
      const latest=a.at(-1);
      if(a.length<3){
        console.log(`[MFP 4TF WAIT] ${sys.id} candles=${a.length} need=3`);
        continue;
      }
      const e=events(a,sys.version).at(-1);
      console.log(`[MFP 4TF EVAL] ${sys.id} candleTs=${latest?.ts??""} eventTs=${e?.t??""} event=${e?.s??"NONE"} reason=${e?.r??""}`);
      if(!e)continue;
      if(e.t!==latest.ts)continue;
      handle(sys,e).catch(x=>console.error("[MFP EXEC ERROR]",sys.id,x?.message||x));
    }
    return;
  }
  cur.high=Math.max(cur.high,c.high);
  cur.low=Math.min(cur.low,c.low);
  cur.close=c.close;
}

if(!key)throw Error("MFP_API_KEY missing");
console.log("[MFP XAU-USD STRATEGY — BTC CONDITIONS] 9 systems: S1 V3-11M/V3-10M/V2-13M | S2 OPP-11M/13M/7M | S3 OPP-15M/V2-10M/V2-9M");
console.log(`[MFP EXECUTOR] MARKET=${market} LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${liveConfirm} EXECUTION_ENABLED=${executionEnabled}`);

await account();
console.log("[MFP AUTH] authenticated; account ready");

// The chart shows XAU-USD perpetual on Binance. Resolve the MFP execution ID
// and the SDK stream symbol from the same market metadata; never silently fall
// back to Hyperliquid or another venue.
let mi=null;
const isTradableXauBinance=(item)=>{
  const text=JSON.stringify(item||{}).toLowerCase();
  const symbol=String(v(item,["coin","stream_symbol","symbol","market_symbol","ticker","name","display_name","market_name"])||"").toUpperCase();
  const id=String(v(item,["market_id","marketId","id"])||"");
  const venue=String(v(item,["venue","exchange","provider","source","market_type"])||"");
  const isXau=symbol.includes("XAU") || /xau[-_/ ]?usd|xauusd/.test(text);
  const isBinance=(venue+" "+id+" "+text).toLowerCase().includes("binance");
  return isXau && isBinance;
};
// First discover canonical MFP market IDs rather than guessing IDs from the
// chart label. Then validate the selected market with GET /markets/{id}.
try{
  const listed=u(await rest("GET","/markets"));
  const marketRows=Array.isArray(listed)?listed:(Array.isArray(listed?.data)?listed.data:(Array.isArray(listed?.markets)?listed.markets:[]));
  console.log("[MFP MARKET LIST] returned="+marketRows.length);
  const matches=marketRows.filter(isTradableXauBinance);
  for(const candidate of matches){
    const id=String(v(candidate,["market_id","marketId","id"])||"");
    if(!id)continue;
    try{
      const test=u(await getMarket(id));
      if(!test)continue;
      console.log("[MFP MARKET PROBE] id="+id+" metadata="+JSON.stringify(test).slice(0,1200));
      if(test?.trading_enabled===true && test?.reduce_only!==true){
        market=id; mi=test; break;
      }
    }catch(e){
      console.warn("[MFP MARKET PROBE] "+id+" -> "+(e?.code||e?.message||e));
    }
  }
  if(!matches.length){
    console.error("[MFP MARKET LIST] No Binance XAU candidates. Sample="+JSON.stringify(marketRows.filter(x=>JSON.stringify(x).toLowerCase().includes("xau")).slice(0,10)).slice(0,4000));
  }
}catch(e){
  console.error("[MFP MARKET LIST ERROR] "+(e?.message||e));
}
if(!mi){
  console.error("[MFP MARKET NOT READY] No confirmed tradeable Binance XAU-USD market found. Executor remains online but ALL new entries are blocked.");
  await new Promise(()=>{});
}
marketReady=true;
orderMarket=String(v(mi,["market_id","marketId","id"])||market);
console.log("[MFP MARKET META] "+JSON.stringify(mi).slice(0,5000));
console.log("[MFP ORDER MARKET] "+orderMarket);
console.log("[MFP EXPOSURE LIMIT] max_pair_exposure_usd="+MAX_PAIR_EXPOSURE_USD);
const streamSymbol=String(v(mi,["coin","stream_symbol","symbol","market_symbol","ticker"])||"");
if(!streamSymbol) throw Error("MFP Binance XAU-USD stream symbol unavailable; orders remain blocked");
let lastTickAt=0;
let lastPrice=0;
function acceptPrice(p,tsRaw){
  p=+p;
  if(!Number.isFinite(p)||p<=0)return false;
  let ts=tsRaw==null?Date.now():(typeof tsRaw==="number"?tsRaw:Date.parse(tsRaw));
  if(!Number.isFinite(ts)||ts<=0)ts=Date.now();
  if(ts<1e12)ts*=1000;
  const m=Math.floor(ts/60000)*60000;
  if(!cur||cur.ts!==m)processMinute({ts:m,open:p,high:p,low:p,close:p});
  else processMinute({ts:m,open:cur.open,high:Math.max(cur.high,p),low:Math.min(cur.low,p),close:p});
  lastTickAt=Date.now();lastHealthTickAt=lastTickAt;feedReady=true;lastPrice=p;
  return true;
}
async function fallbackPoll(){
  console.error("[MFP FEED] No XAU-USD SDK ticks; refusing to substitute a different asset feed.");
  while(true) await new Promise(r=>setTimeout(r,15000));
}
async function streamLoop(){
  while(true){
    try{
      const stream=new PriceStream({symbols:[streamSymbol]});
      console.log("[MFP STREAM] connected symbol="+streamSymbol+"; waiting for ticks");
      const iterator=stream[Symbol.asyncIterator]();
      while(true){
        const next=await Promise.race([
          iterator.next(),
          new Promise(resolve=>setTimeout(()=>resolve({timeout:true}),15000))
        ]);
        if(next?.timeout){
          if(Date.now()-lastTickAt>=15000){await fallbackPoll();return}
          continue;
        }
        if(next.done)throw new Error("SDK PriceStream ended");
        const tick=next.value;
        const p=v(tick,["price","mid","mark","mid_price","mark_price"]);
        if(acceptPrice(p,v(tick,["timestamp","time","ts"]))){
          console.log("[MFP STREAM TICK] "+streamSymbol+" price="+lastPrice);
        }else{
          console.warn("[MFP STREAM UNPARSED TICK] "+JSON.stringify(tick).slice(0,1000));
        }
      }
    }catch(e){
      console.error("[MFP STREAM ERROR] "+(e?.message||e)+"; reconnecting in 3 seconds");
      await new Promise(r=>setTimeout(r,3000));
    }
  }
}
await streamLoop();
