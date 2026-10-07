import {MyFundedPerps, PriceStream} from "@myfundedperps/sdk";

const key=(process.env.MFP_API_KEY||"").trim();
const marketHint=process.env.MFP_MARKET_ID||"BTC-USD";\nlet market=marketHint;
const lev=+(process.env.MFP_LEVERAGE||"5");
const live=(process.env.LIVE_TRADING||"false").toLowerCase()==="true";
const dry=(process.env.DRY_RUN_ONLY||"true").toLowerCase()==="true";
const confirm=(process.env.MFP_USER_LIVE_CONFIRMATION||"").trim()==="YES";
const executionEnabled=live&&!dry&&confirm;

const BASE=+(process.env.MFP_BASE_QTY||"0.80");
const SCALE=+(process.env.MFP_SCALED_QTY||"0.15");
const MAX_QTY=+(process.env.MFP_MAX_QTY||"0.95");
const MAX_POSITIONS=2;
const FILL_CONFIRM_MS=15000;
const FILL_POLL_MS=500;
const COOLDOWN_MS=5000;
const COOLDOWN_RETRIES=3;

const client=new MyFundedPerps({apiKey:key});
let accountId=(process.env.MFP_ACCOUNT_ID||"").trim();
let raw=[],cur=null,lastMinute=null,lastSignals=new Map();
const tracked=new Map();

const SES={S1:["03:15","08:15"],S2:["10:15","14:15"],S3:["16:15","21:15"]};

const SYSTEMS=[
  {id:"S1-57-59-11M",engine:"57-59",session:"S1",tf:11},
  {id:"S1-V2-13M",engine:"V2",session:"S1",tf:13},
  {id:"S1-V2-8M",engine:"V2",session:"S1",tf:8},
  {id:"S2-OPP-11M",engine:"OPPOSITE",session:"S2",tf:11},
  {id:"S2-OPP-10M",engine:"OPPOSITE",session:"S2",tf:10},
  {id:"S2-OPP-7M",engine:"OPPOSITE",session:"S2",tf:7},
  {id:"S3-OPP-15M",engine:"OPPOSITE",session:"S3",tf:15},
  {id:"S3-V2-10M",engine:"V2",session:"S3",tf:10},
  {id:"S3-V2-9M",engine:"V2",session:"S3",tf:9},
];

const v=(x,keys)=>{for(const k of keys)if(x?.[k]!=null)return x[k];return null};
const u=x=>x?.data??x;

function ist(ts){
  const d=new Date(ts+19800000);
  return {date:d.toISOString().slice(0,10),min:d.getUTCHours()*60+d.getUTCMinutes()};
}
function session(ts){
  const p=ist(ts);
  for(const [s,[a,b]] of Object.entries(SES)){
    const [ah,am]=a.split(":").map(Number),[bh,bm]=b.split(":").map(Number);
    if(p.min>=ah*60+am&&p.min<bh*60+bm)return {name:s,key:p.date+"|"+s};
  }
  return null;
}

function aggregate(tf,filterSession=null){
  const g=new Map();
  for(const r of raw){
    if(filterSession && session(r.ts)?.name!==filterSession)continue;
    const k=Math.floor(r.ts/60000/tf)*tf;
    if(!g.has(k))g.set(k,[]);
    g.get(k).push(r);
  }
  return [...g].sort((a,b)=>a[0]-b[0]).map(([,x])=>({
    ts:x[0].ts,open:x[0].open,high:Math.max(...x.map(z=>z.high)),
    low:Math.min(...x.map(z=>z.low)),close:x.at(-1).close
  }));
}

function heiken(a){
  const out=[]; let ho=null,hc=null;
  for(const r of a){
    const close=(r.open+r.high+r.low+r.close)/4;
    const open=ho==null?(r.open+r.close)/2:(ho+hc)/2;
    out.push({...r,ha_open:open,ha_close:close,
      ha_high:Math.max(r.high,open,close),
      ha_low:Math.min(r.low,open,close)});
    ho=open;hc=close;
  }
  return out;
}

function candles(sys){
  if(sys.engine==="V2"){
    // Frozen V2: M1 -> session filter -> timeframe aggregation -> HA.
    return heiken(aggregate(sys.tf,sys.session));
  }
  if(sys.engine==="57-59"){
    // Frozen 57-59 source path: session-filtered timeframe candles -> HA.
    return heiken(aggregate(sys.tf,sys.session));
  }
  // Frozen V3: M1 -> timeframe aggregation -> continuous HA -> session filter.
  return heiken(aggregate(sys.tf)).filter(x=>session(x.ts)?.name===sys.session);
}

function baseEvents(a){
  const e=[]; let side=null,ref=null;
  for(let i=2;i<a.length;i++){
    const c2=a[i-2],c1=a[i-1],c0=a[i];
    const buy=c2.ha_close>c2.ha_open && c1.ha_open<c1.ha_high;
    const sell=c2.ha_close<c2.ha_open && c1.ha_open>c1.ha_low;

    if(side===null){
      if(buy){side="BUY";ref=c1.ha_low;e.push({t:c0.ts,side:"BUY",reason:"NEW_BUY_SIGNAL"});continue;}
      if(sell){side="SELL";ref=c1.ha_high;e.push({t:c0.ts,side:"SELL",reason:"NEW_SELL_SIGNAL"});continue;}
    }

    if(side==="BUY"){
      if(c0.ha_low<ref){
        side="SELL";ref=c0.ha_high;
        e.push({t:c0.ts,side:"SELL",reason:"REFERENCE_LOW_BREAK"});
      }else ref=c0.ha_low;
    }else if(side==="SELL"){
      if(c0.ha_high>ref){
        side="BUY";ref=c0.ha_low;
        e.push({t:c0.ts,side:"BUY",reason:"REFERENCE_HIGH_BREAK"});
      }else ref=c0.ha_high;
    }
  }
  return e;
}

function eventFor(sys,a){
  const e=baseEvents(a).at(-1);
  if(!e)return null;
  if(sys.engine==="OPPOSITE"){
    return {...e,side:e.side==="BUY"?"SELL":"BUY",reason:"OPPOSITE_"+e.reason};
  }
  return e;
}

async function account(){
  const x=u(await client.listAccounts());
  const arr=Array.isArray(x)?x:x?.data||[];
  if(!arr.length)throw Error("MFP authenticated account unavailable");
  const configured=(process.env.MFP_ACCOUNT_ID||"").trim();
  const match=configured?arr.find(z=>String(v(z,["account_id","id","accountId"]))===configured):null;
  const chosen=match||arr[0];
  accountId=v(chosen,["account_id","id","accountId"]);
  if(!accountId)throw Error("MFP account identifier unavailable");
  if(configured&&!match)console.log("[MFP ACCOUNT] configured account not returned by API; using authenticated account");
  return accountId;
}

async function positions(){
  const x=u(await client.listOpenPositions({account_id:await account()}));
  return Array.isArray(x)?x:x?.data||[];
}

function isBTC(p){
  const s=String(v(p,["market_id","marketId","market","symbol","ticker"])||"").toUpperCase();
  return s==="BTC-USD"||s.includes("BTC");
}
function psize(p){return Math.abs(+(v(p,["size","quantity","qty","position_size","positionSize"])||0));}
function pside(p){
  const s=String(v(p,["side","position_side","positionSide"])||"").toLowerCase();
  return s.includes("short")||s.includes("sell")?"SELL":"BUY";
}

async function portfolioSize(){
  const ps=(await positions()).filter(isBTC);
  const exposure=ps.reduce((n,p)=>n+psize(p),0);
  if(ps.length>=MAX_POSITIONS){
    console.warn(`[BTC PORTFOLIO BLOCK] open_positions=${ps.length} max=${MAX_POSITIONS}`);
    return null;
  }
  const remaining=Math.max(0,MAX_QTY-exposure);
  const size=Math.min(ps.length===0?BASE:SCALE,remaining);
  if(size<=0){
    console.warn(`[BTC PORTFOLIO BLOCK] exposure=${exposure} max=${MAX_QTY}`);
    return null;
  }
  console.log(`[BTC PORTFOLIO] open_positions=${ps.length} exposure_btc=${exposure} size=${size} max_btc=${MAX_QTY}`);
  return size;
}

async function open(sys,side){
  if(!executionEnabled){
    console.log(`[BTC DRY SIGNAL] ${sys.id} ${side} executionEnabled=${executionEnabled}`);
    return null;
  }
  const aid=await account();
  const size=await portfolioSize();
  if(size==null)return null;

  let response,attempt=0;
  while(true){
    try{
      response=await client.createOrder({body:{
        account_id:aid,market_id:market,side:side.toLowerCase(),type:"market",
        size,leverage:lev,margin_mode:"cross",expected_price:cur?.close||undefined
      }});
      break;
    }catch(e){
      if(String(e?.code||"").toLowerCase()==="cooldown"&&attempt<COOLDOWN_RETRIES){
        attempt++;
        console.warn(`[BTC ORDER COOLDOWN] ${sys.id} attempt=${attempt}/${COOLDOWN_RETRIES}`);
        await new Promise(r=>setTimeout(r,COOLDOWN_MS));
        continue;
      }
      console.error("[BTC ORDER ERROR]",e?.message||e);
      return null;
    }
  }

  const o=u(response),oid=v(o,["order_id","orderId","id"]);
  console.log(`[BTC ORDER REQUESTED] ${sys.id} ${side} qty=${size} orderId=${oid||""} status=${v(o,["status","order_status"])||""} filled=${v(o,["filled_size","filledSize"])||0}`);
  console.log("[BTC ORDER RESPONSE] "+JSON.stringify(o).slice(0,5000));

  const deadline=Date.now()+FILL_CONFIRM_MS;
  while(Date.now()<deadline){
    try{
      const p=(await positions()).find(x=>isBTC(x)&&pside(x)===side);
      if(p){
        const pid=v(p,["position_id","positionId","id"]);
        const actual=psize(p);
        console.log(`[BTC FILL CONFIRMED] ${sys.id} ${side} position=${pid||""} size=${actual} orderId=${oid||""}`);
        return {position_id:pid,filled_size:actual};
      }
    }catch(e){console.error("[BTC FILL CHECK ERROR]",e?.message||e);}
    await new Promise(r=>setTimeout(r,FILL_POLL_MS));
  }
  console.warn(`[BTC NOT FILLED] ${sys.id} ${side} orderId=${oid||""}; no position armed`);
  return null;
}

async function closeTracked(sys){
  const tr=tracked.get(sys.id);
  if(!tr?.id)return;
  const ps=await positions();
  const p=ps.find(x=>String(v(x,["position_id","positionId","id"]))===String(tr.id));
  if(p&&executionEnabled){
    const closeSide=pside(p)==="SELL"?"buy":"sell";
    const size=psize(p);
    if(size>0){
      await client.createOrder({body:{
        account_id:await account(),market_id:market,side:closeSide,type:"market",
        size,leverage:lev,margin_mode:"cross",reduce_only:true
      }});
      console.log(`[BTC CLOSE] ${sys.id} size=${size}`);
    }
  }
  tracked.delete(sys.id);
}

async function signal(sys,e){
  const key=sys.id+"|"+e.t+"|"+e.side+"|"+e.reason;
  if(lastSignals.get(sys.id)===key)return;
  lastSignals.set(sys.id,key);

  console.log(`[BTC SIGNAL] ${sys.id} ${sys.engine} ${sys.session} ${sys.tf}M ${e.side} ${e.reason}`);
  const tr=tracked.get(sys.id);
  if(tr){
    if(tr.side===e.side)return;
    await closeTracked(sys);
  }
  const r=await open(sys,e.side);
  if(r)tracked.set(sys.id,{side:e.side,id:r.position_id});
}

async function processMinute(c){
  const ss=session(c.ts);
  if(!cur||cur.ts!==c.ts){
    if(cur)raw.push(cur);
    raw=raw.slice(-12000);
    cur={...c};
    if(!ss)return;

    for(const sys of SYSTEMS){
      if(sys.session!==ss.name)continue;
      const a=candles(sys);
      if(a.length<4)continue;
      const e=eventFor(sys,a);
      if(!e||e.t!==a.at(-1).ts)continue;
      signal(sys,e).catch(err=>console.error("[BTC EXEC ERROR]",sys.id,err?.message||err));
    }
  }else{
    cur.high=Math.max(cur.high,c.high);
    cur.low=Math.min(cur.low,c.low);
    cur.close=c.close;
  }
}

if(!key)throw Error("MFP_API_KEY missing");
console.log("[BTC 9-SYSTEM STRATEGY] S1 57-59 11M | S1 V2 13M | S1 V2 8M | S2 OPP 11M | S2 OPP 10M | S2 OPP 7M | S3 OPP 15M | S3 V2 10M | S3 V2 9M");
console.log(`[MFP EXECUTOR] MARKET=${market} LEVERAGE=${lev} BASE=${BASE} SCALE=${SCALE} MAX=${MAX_QTY} LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${confirm} EXECUTION_ENABLED=${executionEnabled}`);

await account();
console.log("[MFP AUTH] authenticated; account ready");

let mi=null;
const candidates=[marketHint,"BTC-USD","hyperliquid|BTC","hyperliquid|xyz:BTC","BTC"];
for(const candidate of [...new Set(candidates)]){
  try{
    const test=u(await client.getMarket({market_id:candidate}));
    if(test){
      market=candidate;
      mi=test;
      console.log("[MFP MARKET] resolved BTC market_id="+market);
      break;
    }
  }catch(e){
    console.warn("[MFP MARKET PROBE] "+candidate+" -> "+(e?.code||e?.message||e));
  }
}
if(!mi)throw Error("BTC market could not be resolved");
const streamSymbol=v(mi,["coin","stream_symbol","symbol","market_symbol","ticker","name"]);
if(!streamSymbol)throw Error("BTC stream symbol unavailable");
console.log("[MFP MARKET] market_id="+market+" stream symbol="+streamSymbol);

while(true){
  try{
    const stream=new PriceStream({symbols:[streamSymbol]});
    console.log("[MFP STREAM] BTC connected");
    for await(const t of stream){
      const price=+v(t,["price","mid","mark"]);
      if(!Number.isFinite(price))continue;
      const ts=+v(t,["timestamp","time","ts"])||Date.now();
      const m=Math.floor(ts/60000)*60000;
      if(!cur||cur.ts!==m)await processMinute({ts:m,open:price,high:price,low:price,close:price});
      else await processMinute({ts:m,open:cur.open,high:Math.max(cur.high,price),low:Math.min(cur.low,price),close:price});
    }
    console.warn("[BTC STREAM END] reconnecting in 3000ms");
  }catch(e){
    console.error("[BTC STREAM ERROR]",e?.message||e);
  }
  await new Promise(r=>setTimeout(r,3000));
}
