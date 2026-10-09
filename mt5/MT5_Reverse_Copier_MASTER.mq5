#property strict
#property version "1.00"
#property description "Reverse Copier MASTER: publishes open/close trade events to Railway"

input string CopierBaseUrl = "https://mfp-competition-production.up.railway.app";
input string CopierToken = "6fpyLJ5Jxnn2Uty4pQn289QevrD9yQbcoyn3dVwbJYF5oD9d";
input int RequestTimeoutMs = 5000;
input bool PublishExistingPositionsOnStart = true;

string Esc(string s){ StringReplace(s,"\\","\\\\"); StringReplace(s,"\"","\\\""); StringReplace(s,"\r","\\r"); StringReplace(s,"\n","\\n"); return s; }

bool SendEvent(string eventId,string masterTicket,string symbol,string action,double volume,double price,double sl,double tp)
{
   string body="{\"event_id\":\""+Esc(eventId)+"\",\"master_ticket\":\""+Esc(masterTicket)+"\",\"symbol\":\""+Esc(symbol)+"\",\"action\":\""+action+"\",\"volume\":"+DoubleToString(volume,8)+",\"price\":"+DoubleToString(price,10)+",\"sl\":"+DoubleToString(sl,10)+",\"tp\":"+DoubleToString(tp,10)+"}";
   char data[]; StringToCharArray(body,data,0,WHOLE_ARRAY,CP_UTF8); if(ArraySize(data)>0) ArrayResize(data,ArraySize(data)-1);
   char result[]; string resultHeaders;
   string headers="Content-Type: application/json\r\nAuthorization: Bearer "+CopierToken+"\r\n";
   ResetLastError();
   int code=WebRequest("POST",CopierBaseUrl+"/master/event",headers,RequestTimeoutMs,data,result,resultHeaders);
   if(code<200 || code>=300){ Print("COPIER MASTER POST failed HTTP=",code," err=",GetLastError()," response=",CharArrayToString(result)); return false; }
   Print("COPIER MASTER sent ",action," ",symbol," ticket=",masterTicket," response=",CharArrayToString(result));
   return true;
}

int OnInit()
{
   if(PublishExistingPositionsOnStart)
   {
      long login=AccountInfoInteger(ACCOUNT_LOGIN);
      for(int i=PositionsTotal()-1;i>=0;i--)
      {
         ulong ticket=PositionGetTicket(i);
         if(ticket==0 || !PositionSelectByTicket(ticket)) continue;
         string symbol=PositionGetString(POSITION_SYMBOL);
         long ident=PositionGetInteger(POSITION_IDENTIFIER);
         string action=((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY ? "BUY":"SELL");
         SendEvent("SNAP-"+IntegerToString(login)+"-"+IntegerToString(ident),
                   IntegerToString(ident),symbol,action,PositionGetDouble(POSITION_VOLUME),
                   PositionGetDouble(POSITION_PRICE_OPEN),PositionGetDouble(POSITION_SL),PositionGetDouble(POSITION_TP));
      }
   }
   Print("Reverse Copier MASTER ready. Add https://mfp-competition-production.up.railway.app to MT5 WebRequest allow-list.");
   return INIT_SUCCEEDED;
}

void OnTradeTransaction(const MqlTradeTransaction &trans,const MqlTradeRequest &request,const MqlTradeResult &result)
{
   if(trans.type!=TRADE_TRANSACTION_DEAL_ADD || trans.deal==0 || !HistoryDealSelect(trans.deal)) return;
   long entry=HistoryDealGetInteger(trans.deal,DEAL_ENTRY);
   long type=HistoryDealGetInteger(trans.deal,DEAL_TYPE);
   long login=AccountInfoInteger(ACCOUNT_LOGIN);
   long posId=HistoryDealGetInteger(trans.deal,DEAL_POSITION_ID);
   string symbol=HistoryDealGetString(trans.deal,DEAL_SYMBOL);
   double vol=HistoryDealGetDouble(trans.deal,DEAL_VOLUME);
   double price=HistoryDealGetDouble(trans.deal,DEAL_PRICE);
   string dealId=IntegerToString((long)trans.deal);
   string masterTicket=IntegerToString(posId);

   if(entry==DEAL_ENTRY_INOUT)
      SendEvent("DEAL-"+IntegerToString(login)+"-"+dealId+"-CLOSE",masterTicket,symbol,"CLOSE",vol,price,0,0);

   if(entry==DEAL_ENTRY_IN || entry==DEAL_ENTRY_INOUT)
   {
      string action=(type==DEAL_TYPE_BUY ? "BUY" : type==DEAL_TYPE_SELL ? "SELL" : "");
      if(action=="") return;
      double sl=0,tp=0;
      if(PositionSelect(symbol)){ sl=PositionGetDouble(POSITION_SL); tp=PositionGetDouble(POSITION_TP); }
      SendEvent("DEAL-"+IntegerToString(login)+"-"+dealId+"-OPEN",masterTicket,symbol,action,vol,price,sl,tp);
   }
   else if(entry==DEAL_ENTRY_OUT || entry==DEAL_ENTRY_OUT_BY)
      SendEvent("DEAL-"+IntegerToString(login)+"-"+dealId+"-CLOSE",masterTicket,symbol,"CLOSE",vol,price,0,0);
}
