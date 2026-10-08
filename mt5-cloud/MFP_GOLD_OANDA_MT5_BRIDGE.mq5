#property strict
#property version "1.00"
#property description "MFP GOLD 4TF + MFP REST -> OANDA MT5 demo cross-check executor"

#include <Trade/Trade.mqh>
CTrade trade;

input string SignalUrl1 = "";
input string SignalUrl2 = "";
input int    PollSeconds = 2;
input double VolumeLots = 0.15;
input long   MagicNumber = 58247654;
input int    DeviationPoints = 50;
input bool   EnableRealDemoExecution = true;
input bool   CloseOnOppositeSignal = true;
input bool   UseChartSymbol = true;
input string FixedSymbol = "";
input string LogFileName = "MFP_OANDA_GOLD_CROSSCHECK.csv";

string lastId1="", lastId2="";
string symbolName="";
datetime lastPoll=0;

string Trim(string s){ StringTrimLeft(s); StringTrimRight(s); return s; }

string JsonString(string body,string key){
   string pat="\"" + key + "\":\"";
   int p=StringFind(body,pat);
   if(p<0) return "";
   p+=StringLen(pat);
   int e=StringFind(body,"\"",p);
   if(e<0) return "";
   return StringSubstr(body,p,e-p);
}

double JsonNumber(string body,string key,double def=0){
   string pat="\"" + key + "\":";
   int p=StringFind(body,pat);
   if(p<0) return def;
   p+=StringLen(pat);
   while(p<StringLen(body) && (StringGetCharacter(body,p)==' ' || StringGetCharacter(body,p)=='\t')) p++;
   int e=p;
   while(e<StringLen(body)){
      ushort c=StringGetCharacter(body,e);
      if((c>='0'&&c<='9')||c=='.'||c=='-'||c=='+'||c=='e'||c=='E') e++;
      else break;
   }
   return StringToDouble(StringSubstr(body,p,e-p));
}

bool HttpGet(string url,string &body){
   body="";
   if(url=="") return false;
   uchar data[],result[];
   string result_headers="";
   ResetLastError();
   int code=WebRequest("GET",url,"","",5000,data,0,result,result_headers);
   if(code<0){
      PrintFormat("[OANDA BRIDGE] WebRequest failed url=%s err=%d. Add URL to MT5 WebRequest allowed list.",url,GetLastError());
      return false;
   }
   body=CharArrayToString(result,0,-1,CP_UTF8);
   if(code<200 || code>=300){
      PrintFormat("[OANDA BRIDGE] HTTP %d url=%s body=%s",code,url,body);
      return false;
   }
   return true;
}

void EnsureLog(){
   int h=FileOpen(LogFileName,FILE_READ|FILE_WRITE|FILE_CSV|FILE_COMMON,',');
   if(h==INVALID_HANDLE) return;
   if(FileSize(h)==0)
      FileWrite(h,"local_time","source","signal_id","signal_time","symbol","side","signal_price","requested_volume","mt5_action","ticket","deal","fill_price","retcode","retcode_desc","status");
   FileClose(h);
}

void LogTrade(string source,string id,string signalTime,string side,double signalPrice,double vol,string action,
              ulong ticket,ulong deal,double fillPrice,long retcode,string desc,string status){
   int h=FileOpen(LogFileName,FILE_READ|FILE_WRITE|FILE_CSV|FILE_COMMON,',');
   if(h==INVALID_HANDLE) return;
   FileSeek(h,0,SEEK_END);
   FileWrite(h,TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS),source,id,signalTime,symbolName,
             side,DoubleToString(signalPrice,_Digits),DoubleToString(vol,2),action,
             (string)ticket,(string)deal,DoubleToString(fillPrice,_Digits),(string)retcode,desc,status);
   FileClose(h);
}

double NormalizeVolume(double v){
   double minv=SymbolInfoDouble(symbolName,SYMBOL_VOLUME_MIN);
   double maxv=SymbolInfoDouble(symbolName,SYMBOL_VOLUME_MAX);
   double step=SymbolInfoDouble(symbolName,SYMBOL_VOLUME_STEP);
   if(step<=0) step=minv;
   if(v<minv || v>maxv) return 0;
   double n=MathFloor(v/step+1e-9)*step;
   int vd=(int)MathMax(0,MathRound(-MathLog10(step)));
   return NormalizeDouble(n,vd);
}

bool CloseOpposite(string side){
   if(!CloseOnOppositeSignal) return true;
   bool wantBuy=(side=="BUY");
   for(int i=PositionsTotal()-1;i>=0;i--){
      ulong ticket=PositionGetTicket(i);
      if(ticket==0) continue;
      if(PositionGetString(POSITION_SYMBOL)!=symbolName) continue;
      long type=PositionGetInteger(POSITION_TYPE);
      bool isBuy=(type==POSITION_TYPE_BUY);
      if(isBuy==wantBuy) continue;
      if(!trade.PositionClose(ticket)){
         PrintFormat("[OANDA BRIDGE] opposite close failed ticket=%I64u retcode=%u %s",
                     ticket,trade.ResultRetcode(),trade.ResultRetcodeDescription());
         return false;
      }
      PrintFormat("[OANDA BRIDGE] opposite position closed ticket=%I64u deal=%I64u price=%s",
                  ticket,trade.ResultDeal(),DoubleToString(trade.ResultPrice(),_Digits));
   }
   return true;
}

void ExecuteSignal(string source,string body,string &lastId){
   string id=JsonString(body,"id");
   if(id=="") id=JsonString(body,"signal_id");
   if(id=="") return;
   if(id==lastId) return;

   string side=JsonString(body,"side");
   StringToUpper(side);
   if(side!="BUY" && side!="SELL") return;

   double price=JsonNumber(body,"price",JsonNumber(body,"signal_price",0));
   double vol=JsonNumber(body,"size",JsonNumber(body,"qty",VolumeLots));
   if(vol<=0) vol=VolumeLots;
   vol=NormalizeVolume(vol);

   string signalTime=JsonString(body,"timestamp");
   if(signalTime=="") signalTime=JsonString(body,"signal_time");

   if(vol<=0){
      LogTrade(source,id,signalTime,side,price,0,"REJECT_INVALID_VOLUME",0,0,0,0,"volume outside broker limits","REJECTED");
      lastId=id;
      return;
   }

   if(!EnableRealDemoExecution){
      LogTrade(source,id,signalTime,side,price,vol,"DRY_RUN",0,0,0,0,"execution disabled","NOT_SENT");
      lastId=id;
      return;
   }

   if(!CloseOpposite(side)){
      LogTrade(source,id,signalTime,side,price,vol,"OPPOSITE_CLOSE_FAILED",0,0,0,trade.ResultRetcode(),trade.ResultRetcodeDescription(),"REJECTED");
      return;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(DeviationPoints);
   bool ok=false;
   if(side=="BUY") ok=trade.Buy(vol,symbolName,0,0,0,"MFP_GOLD_4TF");
   else            ok=trade.Sell(vol,symbolName,0,0,0,"MFP_GOLD_4TF");

   ulong ticket=trade.ResultOrder();
   ulong deal=trade.ResultDeal();
   double fill=trade.ResultPrice();
   long rc=(long)trade.ResultRetcode();
   string desc=trade.ResultRetcodeDescription();
   string status=ok ? "ORDER_ACCEPTED" : "ORDER_REJECTED";

   LogTrade(source,id,signalTime,side,price,vol,side,ticket,deal,fill,rc,desc,status);
   PrintFormat("[OANDA BRIDGE] %s id=%s side=%s signal=%s vol=%s ok=%s ticket=%I64u deal=%I64u fill=%s ret=%d %s",
               source,id,side,DoubleToString(price,_Digits),DoubleToString(vol,2),ok?"true":"false",
               ticket,deal,DoubleToString(fill,_Digits),rc,desc);

   if(ok && deal>0) lastId=id;
}

void PollOne(string source,string url,string &lastId){
   string body;
   if(HttpGet(url,body)) ExecuteSignal(source,body,lastId);
}

int OnInit(){
   symbolName=UseChartSymbol ? _Symbol : FixedSymbol;
   if(symbolName==""){
      Print("[OANDA BRIDGE] No symbol selected.");
      return INIT_FAILED;
   }
   if(!SymbolSelect(symbolName,true)){
      PrintFormat("[OANDA BRIDGE] Cannot select symbol %s",symbolName);
      return INIT_FAILED;
   }
   EnsureLog();
   EventSetTimer(MathMax(1,PollSeconds));

   double minv=SymbolInfoDouble(symbolName,SYMBOL_VOLUME_MIN);
   double maxv=SymbolInfoDouble(symbolName,SYMBOL_VOLUME_MAX);
   double step=SymbolInfoDouble(symbolName,SYMBOL_VOLUME_STEP);
   PrintFormat("[OANDA BRIDGE] START server=%s account=%s symbol=%s min=%s max=%s step=%s volume=%s",
               AccountInfoString(ACCOUNT_SERVER),(string)AccountInfoInteger(ACCOUNT_LOGIN),symbolName,
               DoubleToString(minv,4),DoubleToString(maxv,4),DoubleToString(step,4),DoubleToString(VolumeLots,2));
   Print("[OANDA BRIDGE] REAL DEMO EXECUTION=",EnableRealDemoExecution?"ON":"OFF");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason){
   EventKillTimer();
}

void OnTimer(){
   if(TimeCurrent()-lastPoll<MathMax(1,PollSeconds)) return;
   lastPoll=TimeCurrent();
   PollOne("GOLD_4TF",SignalUrl1,lastId1);
   PollOne("MFP_REST",SignalUrl2,lastId2);
}
