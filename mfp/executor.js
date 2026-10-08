import {MyFundedPerps,PriceStream} from "@myfundedperps/sdk";
const key=(process.env.MFP_API_KEY||"").trim(), market=process.env.MFP_MARKET_ID||"hyperliquid|xyz:GOLD";
const qty=+(process.env.MFP_BASE_QTY||"0.150"), lev=+(process.env.MFP_LEVERAGE||"5");
const live=(process.env.LIVE_TRADING||"false").toLowerCase()=="true", dry=(process.env.DRY_RUN_ONLY||"true").toLowerCase()=="true";
const liveConfirm=(process.env.MFP_USER_LIVE_CONFIRMATION||"").trim()=="YES";
const executionEnabled=live&&!dry&&liveConfirm;
const client=new MyFundedPerps({apiKey:key}); let aid=(process.env.MFP_ACCOUNT_ID||"").trim(), raw=[],cur=null,last=new Map(),slots={S1:[],S2:[],S3:[]},sk=null;
const SES={S1:["03:15","08:15"],S2:["10:15","14:15"],S3:["16:15","21:15"]};
const TF={S1:{10:"T1",13:"T2",15:"T3"},S2:{6:"T1",14:"T2",15:"T3"},S3:{14:"T1",10:"T2",15:"T3"}};
const v=(x,a)=>{for(const k of a)if(x?.[k]!=null)return x[k];return null},u=x=>x?.data??x;
function ist(t){let d=new Date(t+19800000);return {d:d.toISOString().slice(0,10),m:d.getUTCHours()*60+d.getUTCMinutes()}}
function sess(t){let p=ist(t);for(const[s,[a,b]]of Object.entries(SES)){let A=a.split(":").map(Number),B=b.split(":").map(Number);if(p.m>=A[0]*60+A[1]&&p.m<B[0]*60+B[1])return{s,k:p.d+"|"+s}}}
function agg(tf){let g=new Map;for(const r of raw){let k=Math.floor(r.ts/60000/tf)*tf;if(!g.has(k))g.set(k,[]);g.get(k).push(r)}return [...g].sort((a,b)=>a[0]-b[0]).map(([,x])=>({ts:x[0].ts,open:x[0].open,high:Math.max(...x.map(r=>r.high)),low:Math.min(...x.map(r=>r.low)),close:x.at(-1).close}))}
function ha(a){let z=[],o,c;for(const r of a){let hc=(r.open+r.high+r.low+r.close)/4,ho=o==null?(r.open+r.close)/2:(o+c)/2;z.push({...r,ha_open:ho,ha_close:hc,ha_high:Math.max(r.high,ho,hc),ha_low:Math.min(r.low,ho,hc)});o=ho;c=hc}return z}
function rows(tf,s){return ha(agg(tf)).filter(x=>sess(x.ts)?.s==s)}
function events(a){let e=[],p=null,r=null;for(let i=2;i<a.length;i++){let c2=a[i-2],c1=a[i-1],c0=a[i],b=c2.ha_close>c2.ha_open&&c1.ha_open<c1.ha_high,q=c2.ha_close<c2.ha_open&&c1.ha_open>c1.ha_low;
if(b){if(p!="BUY"){p="BUY";e.push({t:c0.ts,s:"BUY",r:"NEW_BUY_SIGNAL"})}r=c1.ha_low}else if(q){if(p!="SELL"){p="SELL";e.push({t:c0.ts,s:"SELL",r:"NEW_SELL_SIGNAL"})}r=c1.ha_high}
if(p=="BUY"){if(c0.ha_low<r){p="SELL";e.push({t:c0.ts,s:"SELL",r:"REFERENCE_LOW_BREAK"});r=c0.ha_high}else r=c0.ha_low}
else if(p=="SELL"){if(c0.ha_high>r){p="BUY";e.push({t:c0.ts,s:"BUY",r:"REFERENCE_HIGH_BREAK"});r=c0.ha_low}else r=c0.ha_high}}return e}
async function account(){let x=u(await client.listAccounts()),a=Array.isArray(x)?x:x?.data||[];if(!a.length)throw Error("MFP authenticated account unavailable");let configured=(process.env.MFP_ACCOUNT_ID||"").trim();let match=configured?a.find(z=>String(v(z,["account_id","id","accountId"]))===configured):null;let chosen=match||a[0];aid=v(chosen,["account_id","id","accountId"]);if(!aid)throw Error("MFP account identifier unavailable");if(configured&&!match)console.log("[MFP ACCOUNT] configured account not returned by API; using authenticated account");return aid}
async function open(s,pr,side,t){if(!executionEnabled){console.log(`[MFP DRY SIGNAL] ${s} ${pr} ${side} executionEnabled=${executionEnabled}`);return null}if(Date.now()-lastMarketCheck>15000||!marketReady){const ok=await refreshMarket();if(!ok){console.log(`[MFP ORDER BLOCKED] ${s} ${pr} ${side} market unavailable/reduce-only; no order submitted`);return null}}let r=await client.createOrder({body:{account_id:await account(),market_id:market,side:side.toLowerCase(),type:"market",size:qty,leverage:lev,margin_mode:"cross",expected_price:cur?.close||undefined}});console.log(`[MFP ORDER ACCEPTED] ${s} ${pr} ${side} ${new Date(t).toISOString()}`);return u(r)}
async function positions(){let x=u(await client.listOpenPositions({account_id:await account()}));return Array.isArray(x)?x:x?.data||[]}
async function closeSession(s,why){let ps=await positions();for(const z of slots[s]){let p=ps.find(x=>String(v(x,["position_id","id","positionId"]))==String(z.id));if(p&&executionEnabled){let pid=v(p,["position_id","id","positionId"]),rawSize=v(p,["size","quantity","qty","position_size","positionSize"]),closeSize=Math.abs(+(rawSize||qty)),posSide=String(v(p,["side","position_side","positionSide"])||"long").toLowerCase(),closeSide=posSide.includes("short")?"buy":"sell";await client.createOrder({body:{account_id:await account(),market_id:market,side:closeSide,type:"market",size:closeSize,leverage:lev,margin_mode:"cross",reduce_only:true}});}console.log(`[MFP CLOSE] ${s} ${why}`)}slots[s]=[]}
function ok2(a,b){return a=="T1"?(b=="T2"||b=="T3"):a=="T2"?b=="T1":a=="T3"?b=="T1":false}
async function candidate(s,pr,side,t,r,tf){console.log(`[MFP SIGNAL] ${s} ${pr} ${side} ${r} tf=${tf}`);let a=slots[s],opp=a.some(x=>x.side!=side);
if(opp){if(pr=="T3"){console.log(`[MFP REJECTED] ${s} opposite-T3`);return}await closeSession(s,"OPPOSITE_T1_T2");let x=await open(s,pr,side,t);if(x)a.push({side,pr,id:v(x,["position_id","id","positionId"])});return}
if(a.length>=2||a.some(x=>x.pr==pr)||(a.length==1&&!ok2(a[0].pr,pr))){console.log(`[MFP REJECTED] ${s} priority=${pr}`);return}
let x=await open(s,pr,side,t);if(x)a.push({side,pr,id:v(x,["position_id","id","positionId"])})}
function minute(c){let ss=sess(c.ts),k=ss?.k||null;if(k!=sk){if(sk)slots[sk.split("|")[1]]=[];sk=k;if(k)console.log("[MFP SESSION] "+k)}
if(!cur||Math.floor(c.ts/60000)!=Math.floor(cur.ts/60000)){if(cur)raw.push(cur);raw=raw.slice(-2000);cur={...c};if(!ss)return;for(const[tf,pr]of Object.entries(TF[ss.s])){let a=rows(+tf,ss.s);if(a.length<4)continue;let e=events(a).at(-1);if(!e)continue;let kk=ss.s+"|"+tf,sg=e.t+"|"+e.s+"|"+e.r;if(last.get(kk)==sg)continue;last.set(kk,sg);candidate(ss.s,pr,e.s,e.t,e.r,+tf).catch(x=>console.error("[MFP EXEC ERROR]",x.message))}}
cur.high=Math.max(cur.high,c.high);cur.low=Math.min(cur.low,c.low);cur.close=c.close}
if(!key)throw Error("MFP_API_KEY missing");
console.log(`[MFP EXECUTOR] PRIMARY_ENGINE=57-59 MARKET=${market} LIVE_TRADING=${live} DRY_RUN_ONLY=${dry} USER_LIVE_CONFIRMATION=${liveConfirm} EXECUTION_ENABLED=${executionEnabled}`);
await account();console.log("[MFP AUTH] authenticated; account ready");
let marketMeta=null, marketReady=false, lastMarketCheck=0;
async function refreshMarket(){try{const mi=u(await client.getMarket({market_id:market})); marketMeta=mi; const enabled=mi?.trading_enabled===true; const reduceOnly=mi?.reduce_only===true; marketReady=enabled&&!reduceOnly; if(marketReady) console.log(`[MFP MARKET READY] ${market} trading_enabled=true reduce_only=false`); else console.log(`[MFP MARKET WAIT] ${market} trading_enabled=${mi?.trading_enabled} reduce_only=${mi?.reduce_only}`); lastMarketCheck=Date.now(); return marketReady;}catch(e){marketReady=false; console.error("[MFP MARKET CHECK ERROR] "+(e?.message||e)); lastMarketCheck=Date.now(); return false;}}
await refreshMarket();
const streamSymbol=v(marketMeta,["coin","stream_symbol","symbol","market_symbol","ticker","name"]); if(!streamSymbol) throw Error("MFP stream symbol unavailable"); console.log("[MFP MARKET] stream symbol="+streamSymbol);
async function streamLoop(streamSymbol){
while(true){
try{
const stream=new PriceStream({symbols:[streamSymbol]});
console.log("[MFP STREAM] GOLD connected");
for await(const t of stream){let p=+v(t,["price","mid","mark"]);if(!Number.isFinite(p))continue;let ts=+v(t,["timestamp","time","ts"])||Date.now(),m=Math.floor(ts/60000)*60000;
if(!cur||cur.ts!=m)minute({ts:m,open:p,high:p,low:p,close:p});else minute({ts:m,open:cur.open,high:Math.max(cur.high,p),low:Math.min(cur.low,p),close:p})}
console.error("[MFP STREAM END] price subscription ended; reconnecting in 3000ms");await new Promise(r=>setTimeout(r,3000));
}catch(x){console.error("[MFP STREAM ERROR] "+(x?.message||x)+"; reconnecting in 3000ms");await new Promise(r=>setTimeout(r,3000));}
}}
await streamLoop(streamSymbol);
