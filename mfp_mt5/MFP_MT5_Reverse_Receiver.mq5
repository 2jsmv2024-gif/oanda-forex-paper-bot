#property strict
#property version "1.00"
#property description "MFP to MT5 reverse copier receiver. Dry-run by default; live execution restricted to demo accounts."
#include <Trade/Trade.mqh>
CTrade trade;

input string InpBaseUrl = "https://REPLACE-WITH-RAILWAY-DOMAIN";
input string InpBearerToken = "SET_SAME_MT5_BRIDGE_TOKEN";
input string InpSymbol = "XAUUSD";
input double InpFixedLots = 0.59;
input bool InpDryRun = true;
input long InpMagic = 29102026;
input int InpPollSeconds = 5;
input int InpTimeoutMs = 7000;

string TrimSlash(string s) { while(StringLen(s)>0 && StringSubstr(s,StringLen(s)-1,1)=="/") s=StringSubstr(s,0,StringLen(s)-1); return s; }
string JsonString(string json,string key) {
  string needle="\"" + key + "\":\""; int p=StringFind(json,needle); if(p<0) return "";
  p+=StringLen(needle); int e=p; bool esc=false;
  while(e<StringLen(json)){string c=StringSubstr(json,e,1);if(c=="\""&&!esc)break;if(c=="\\"&&!esc)esc=true;else esc=false;e++;}
  if(e>=StringLen(json))return ""; return StringSubstr(json,p,e-p);
}
double JsonNumber(string json,string key,double fallback=0.0) {
  string needle="\"" + key + "\":"; int p=StringFind(json,needle);if(p<0)return fallback;p+=StringLen(needle);
  while(p<StringLen(json)&&(StringSubstr(json,p,1)==" "||StringSubstr(json,p,1)=="\t"))p++;
  int e=p;while(e<StringLen(json)){string c=StringSubstr(json,e,1);if((c>="0"&&c<="9")||c=="."||c=="-"||c=="+")e++;else break;}
  if(e==p)return fallback;return StringToDouble(StringSubstr(json,p,e-p));
}
bool Http(string method,string endpoint,string body,string &response) {
  string url=TrimSlash(InpBaseUrl)+endpoint;
  string headers="Authorization: Bearer "+InpBearerToken+"\r\nAccept: application/json\r\nContent-Type: application/json\r\n";
  char data[],result[]; if(StringLen(body)>0){int n=StringToCharArray(body,data,0,WHOLE_ARRAY,CP_UTF8);if(n>0)ArrayResize(data,n-1);}
  string rh; ResetLastError(); int code=WebRequest(method,url,headers,InpTimeoutMs,data,result,rh);
  if(code==-1){Print("MFP-MT5 bridge WebRequest failed err=",GetLastError(),". Add Railway HTTPS domain under Tools > Options > Expert Advisors > Allow WebRequest.");return false;}
  response=CharArrayToString(result,0,-1,CP_UTF8);
  if(code<200||code>=300){Print("Bridge HTTP ",code,": ",response);return false;}return true;
}
bool Ack(string id,string status,string message="") {
  string escaped=message;StringReplace(escaped,"\\","\\\\");StringReplace(escaped,"\"","\\\"");
  string body="{\"id\":\""+id+"\",\"status\":\""+status+"\",\"message\":\""+escaped+"\"}";
  string response;return Http("POST","/ack",body,response);
}
bool IsDone(string id){return GlobalVariableCheck("MFPREV_DONE_"+id);}
void MarkDone(string id){GlobalVariableSet("MFPREV_DONE_"+id,(double)TimeCurrent());}
bool ValidVolume(string symbol,double lots) {
  double minv=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN),maxv=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX),step=SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP);
  if(minv<=0||step<=0||lots<minv-1e-8||lots>maxv+1e-8){Print("Volume blocked: lots=",lots," min=",minv," max=",maxv," step=",step);return false;}
  double steps=(lots-minv)/step;if(MathAbs(steps-MathRound(steps))>1e-6){Print("Volume blocked: ",lots," is not aligned to volume step ",step);return false;}return true;
}
bool CloseCopierPositions(string symbol) {
  bool ok=true;
  for(int i=PositionsTotal()-1;i>=0;i--){
    ulong ticket=PositionGetTicket(i);if(ticket==0||!PositionSelectByTicket(ticket))continue;
    if(PositionGetString(POSITION_SYMBOL)!=symbol)continue;
    if((long)PositionGetInteger(POSITION_MAGIC)!=InpMagic)continue;
    if(!trade.PositionClose(ticket)){Print("Close failed ticket=",ticket," ret=",trade.ResultRetcode()," ",trade.ResultRetcodeDescription());ok=false;}
  }
  return ok;
}
void ProcessCommand(string json) {
  if(StringFind(json,"\"command\":null")>=0||StringFind(json,"\"command\" : null")>=0)return;
  string id=JsonString(json,"id");if(id=="")return;
  string action=JsonString(json,"action"),side=JsonString(json,"side"),symbol=JsonString(json,"symbol");
  double lots=JsonNumber(json,"lots",InpFixedLots);if(symbol=="")symbol=InpSymbol;
  if(IsDone(id)){Ack(id,InpDryRun?"DRY_RUN_OK":(action=="CLOSE"?"CLOSED":"EXECUTED"),"duplicate command already processed locally");return;}
  if(InpDryRun){Print("[DRY RUN] id=",id," action=",action," side=",side," symbol=",symbol," lots=",DoubleToString(lots,2),"; no order sent");MarkDone(id);Ack(id,"DRY_RUN_OK","MT5 receiver connection verified; execution disabled");return;}
  if((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO){Print("BLOCKED: live execution only permitted by this receiver on an OANDA DEMO account.");Ack(id,"BLOCKED","account is not demo");return;}
  if(!TerminalInfoInteger(TERMINAL_CONNECTED)||!MQLInfoInteger(MQL_TRADE_ALLOWED)||!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)){Print("Trading permissions/connection not ready.");Ack(id,"ERROR","terminal disconnected or trading not allowed");return;}
  if(!SymbolSelect(symbol,true)){Print("SymbolSelect failed: ",symbol);Ack(id,"ERROR","symbol unavailable: "+symbol);return;}
  trade.SetExpertMagicNumber(InpMagic);trade.SetAsyncMode(false);trade.SetTypeFillingBySymbol(symbol);
  if(action=="CLOSE"){
    if(!CloseCopierPositions(symbol)){Ack(id,"ERROR","one or more copier positions failed to close");return;}
    MarkDone(id);Ack(id,"CLOSED","copier positions closed");return;
  }
  if(action!="OPEN"||(side!="BUY"&&side!="SELL")){Ack(id,"ERROR","invalid command action/side");return;}
  if(MathAbs(lots-InpFixedLots)>1e-8){Ack(id,"BLOCKED","command lots differ from configured fixed lots");return;}
  if(!ValidVolume(symbol,lots)){Ack(id,"BLOCKED","volume invalid for destination symbol");return;}
  string comment="MFPREV-"+StringSubstr(id,0,20);
  bool sent=(side=="BUY")?trade.Buy(lots,symbol,0,0,0,comment):trade.Sell(lots,symbol,0,0,0,comment);
  if(!sent){Print("Order rejected ret=",trade.ResultRetcode()," ",trade.ResultRetcodeDescription());Ack(id,"ERROR",trade.ResultRetcodeDescription());return;}
  Print("[DEMO ORDER ACCEPTED] id=",id," side=",side," symbol=",symbol," lots=",lots," order=",trade.ResultOrder());
  MarkDone(id);Ack(id,"EXECUTED","demo order accepted by MT5");
}
int OnInit() {
  if(InpBearerToken=="SET_SAME_MT5_BRIDGE_TOKEN"||StringLen(InpBearerToken)<20){Print("Set InpBearerToken to the MT5_BRIDGE_TOKEN configured on Railway.");return INIT_PARAMETERS_INCORRECT;}
  if(StringFind(InpBaseUrl,"REPLACE-WITH-RAILWAY-DOMAIN")>=0){Print("Set InpBaseUrl to the deployed Railway HTTPS domain.");return INIT_PARAMETERS_INCORRECT;}
  if(!InpDryRun&&(ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE)!=ACCOUNT_TRADE_MODE_DEMO){Print("This receiver refuses live-account execution. Attach it only to the OANDA demo account.");return INIT_FAILED;}
  trade.SetExpertMagicNumber(InpMagic);EventSetTimer(MathMax(2,InpPollSeconds));
  Print("MFP reverse receiver started. mode=",InpDryRun?"DRY_RUN":"DEMO_EXECUTION"," symbol=",InpSymbol," fixedLots=",InpFixedLots,"; MFP BUY -> MT5 SELL, MFP SELL -> MT5 BUY.");
  return INIT_SUCCEEDED;
}
void OnDeinit(const int reason){EventKillTimer();}
void OnTimer(){string response;if(!Http("GET","/next","",response))return;ProcessCommand(response);}
