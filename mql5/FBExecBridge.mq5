//+------------------------------------------------------------------+
//|                                              FBExecBridge.mq5     |
//|     Thin execution bridge — Python is the brain, this EA drains. |
//|                                                                  |
//|  MQL5 EAs are outbound-only, so Python cannot push. Instead this |
//|  EA POLLs a Python command queue over HTTP on a timer, executes  |
//|  each command via CTrade, and ACKs results back. No strategy     |
//|  logic lives here — every order is decided Python-side.          |
//|                                                                  |
//|  v1 scope (place-only minimal):                                  |
//|    PLACE_PENDING  buy_stop / sell_stop @ price, lot, sl, tp      |
//|    CLOSE_ALL      close positions + cancel pendings (this magic) |
//|                                                                  |
//|  Endpoints (whitelist InpBridgeURL host under                    |
//|    Tools -> Options -> Expert Advisors -> Allow WebRequest):     |
//|    POST {InpBridgeURL}/exec/poll  {account}  -> {commands:[...]} |
//|    POST {InpBridgeURL}/exec/ack   {account, results:[...]}        |
//+------------------------------------------------------------------+
#property copyright "Aniket"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>

input string InpBridgeURL   = "http://127.0.0.1:5000"; // Python bridge base URL (whitelist host!)
input int    InpPollMs      = 1000;                    // Poll interval (ms)
input int    InpTimeoutMs   = 4000;                    // WebRequest timeout (ms)
input string InpToken       = "";                      // X-FB-Token (must match server FB_EXEC_TOKEN; blank = none)
input int    InpMagic       = 770001;                  // Magic number for bridge orders
input int    InpSlippage    = 20;                      // Deviation (points)
input bool   InpVerbose     = true;                    // Log every command

CTrade        trade;
COrderInfo    orderInfo;
CPositionInfo posInfo;

string gAccount = "";

//+------------------------------------------------------------------+
//| Minimal flat-JSON scalar extractors (contract is flat per object)|
//+------------------------------------------------------------------+
string JsonGetString(const string js, const string key)
{
   string pat = "\"" + key + "\"";
   int k = StringFind(js, pat);
   if(k < 0) return "";
   int colon = StringFind(js, ":", k + StringLen(pat));
   if(colon < 0) return "";
   int q1 = StringFind(js, "\"", colon + 1);
   if(q1 < 0) return "";
   int q2 = StringFind(js, "\"", q1 + 1);
   if(q2 < 0) return "";
   return StringSubstr(js, q1 + 1, q2 - q1 - 1);
}

double JsonGetNumber(const string js, const string key)
{
   string pat = "\"" + key + "\"";
   int k = StringFind(js, pat);
   if(k < 0) return 0.0;
   int colon = StringFind(js, ":", k + StringLen(pat));
   if(colon < 0) return 0.0;
   int i = colon + 1;
   while(i < StringLen(js) && StringGetCharacter(js, i) == ' ') i++;
   int start = i;
   while(i < StringLen(js))
   {
      ushort ch = StringGetCharacter(js, i);
      if(ch == ',' || ch == '}' || ch == ']') break;
      i++;
   }
   return StringToDouble(StringSubstr(js, start, i - start));
}

//+------------------------------------------------------------------+
//| Split the "commands":[ {..},{..} ] array into object substrings  |
//| Returns count; fills out[] with each {...} object string.        |
//+------------------------------------------------------------------+
int JsonSplitCommands(const string js, string &out[])
{
   ArrayResize(out, 0);
   int k = StringFind(js, "\"commands\"");
   if(k < 0) return 0;
   int open = StringFind(js, "[", k);
   if(open < 0) return 0;
   //--- find matching close bracket for the array (objects have no nested [])
   int close = StringFind(js, "]", open);
   if(close < 0) return 0;
   string body = StringSubstr(js, open + 1, close - open - 1);

   int pos = 0;
   while(true)
   {
      int objOpen = StringFind(body, "{", pos);
      if(objOpen < 0) break;
      int objClose = StringFind(body, "}", objOpen);
      if(objClose < 0) break;
      int n = ArraySize(out);
      ArrayResize(out, n + 1);
      out[n] = StringSubstr(body, objOpen, objClose - objOpen + 1);
      pos = objClose + 1;
   }
   return ArraySize(out);
}

//+------------------------------------------------------------------+
//| HTTP POST helper. Returns HTTP code (-1 on transport error).     |
//+------------------------------------------------------------------+
int HttpPost(const string url, const string body, string &response)
{
   char post[];
   StringToCharArray(body, post, 0, StringLen(body));
   ArrayResize(post, StringLen(body));   // drop trailing \0

   char   result[];
   string resultHeaders;
   string headers = "Content-Type: application/json\r\n";
   if(InpToken != "") headers += "X-FB-Token: " + InpToken + "\r\n";

   ResetLastError();
   int code = WebRequest("POST", url, headers, InpTimeoutMs, post, result, resultHeaders);
   if(code == -1)
   {
      int err = GetLastError();
      Print("❌ WebRequest ", url, " failed (err ", err,
            "). If 4060: whitelist ", InpBridgeURL,
            " under Tools->Options->Expert Advisors->Allow WebRequest.");
      response = "";
      return -1;
   }
   response = CharArrayToString(result, 0, ArraySize(result));
   return code;
}

//+------------------------------------------------------------------+
//| Execute one PLACE_PENDING command. Fills ticket/retcode/err.     |
//+------------------------------------------------------------------+
bool ExecPlacePending(const string cmd, ulong &ticket, int &retcode, string &err)
{
   string sym       = JsonGetString(cmd, "symbol");
   string orderType = JsonGetString(cmd, "order_type");
   string comment   = JsonGetString(cmd, "comment");
   int    digits    = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double price     = NormalizeDouble(JsonGetNumber(cmd, "price"), digits);
   double lot       = NormalizeDouble(JsonGetNumber(cmd, "lot"), 2);
   double sl        = JsonGetNumber(cmd, "sl");
   double tp        = JsonGetNumber(cmd, "tp");
   sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0.0;
   tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0.0;

   if(sym == "" || lot <= 0 || price <= 0)
   {
      err = "bad fields"; retcode = 0; ticket = 0; return false;
   }

   //--- broker freeze/stops level guard (a stop must clear it or it's rejected)
   double point        = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stopsLevelPts = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double minStop      = stopsLevelPts * point;

   bool ok = false;
   if(orderType == "buy_stop")
   {
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      if(price < ask + minStop + point) { err = "buy_stop inside freeze"; return false; }
      ok = trade.BuyStop(lot, price, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
   }
   else if(orderType == "sell_stop")
   {
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      if(price > bid - minStop - point) { err = "sell_stop inside freeze"; return false; }
      ok = trade.SellStop(lot, price, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
   }
   else
   {
      err = "unknown order_type " + orderType; return false;
   }

   retcode = (int)trade.ResultRetcode();
   if(!ok)
   {
      err = trade.ResultRetcodeDescription();
      ticket = 0;
      return false;
   }
   ticket = trade.ResultOrder();
   if(ticket == 0 || !OrderSelect(ticket))
   {
      err = "phantom (not in book)";
      return false;
   }
   err = "";
   return true;
}

//+------------------------------------------------------------------+
//| Execute CLOSE_ALL for a symbol: close positions + cancel pendings|
//| belonging to this magic. Returns true if no failures.            |
//+------------------------------------------------------------------+
bool ExecCloseAll(const string cmd, int &closed, int &cancelled, string &err)
{
   string sym = JsonGetString(cmd, "symbol");
   closed = 0; cancelled = 0; err = "";
   bool allOk = true;

   //--- snapshot pending tickets, then delete (avoid list-shift)
   ulong pend[];
   for(int i = 0; i < OrdersTotal(); i++)
      if(orderInfo.SelectByIndex(i))
         if(orderInfo.Magic() == InpMagic && (sym == "" || orderInfo.Symbol() == sym))
         {
            int n = ArraySize(pend); ArrayResize(pend, n + 1); pend[n] = orderInfo.Ticket();
         }
   for(int i = 0; i < ArraySize(pend); i++)
   {
      if(trade.OrderDelete(pend[i])) cancelled++;
      else { allOk = false; err = "cancel fail #" + IntegerToString((long)pend[i]); }
   }

   //--- snapshot position tickets, then close
   ulong pos[];
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(posInfo.Magic() == InpMagic && (sym == "" || posInfo.Symbol() == sym))
         {
            int n = ArraySize(pos); ArrayResize(pos, n + 1); pos[n] = posInfo.Ticket();
         }
   for(int i = 0; i < ArraySize(pos); i++)
   {
      if(trade.PositionClose(pos[i], InpSlippage)) closed++;
      else { allOk = false; err = "close fail #" + IntegerToString((long)pos[i]); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Poll the queue, execute commands, build + POST the ack array.    |
//+------------------------------------------------------------------+
void PollAndExecute()
{
   //--- report this terminal's live quote so the server can rebase plans onto it
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   string pollBody = StringFormat(
      "{\"account\":\"%s\",\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f}",
      gAccount, _Symbol, bid, ask);
   string resp;
   int code = HttpPost(InpBridgeURL + "/exec/poll", pollBody, resp);
   if(code != 200) return;

   string cmds[];
   int n = JsonSplitCommands(resp, cmds);
   if(n <= 0) return;

   string results = "";   // JSON array body (without brackets)
   for(int i = 0; i < n; i++)
   {
      string id   = JsonGetString(cmds[i], "id");
      string type = JsonGetString(cmds[i], "type");
      bool   ok   = false;
      ulong  ticket = 0;
      int    retcode = 0;
      string err = "";
      string extra = "";

      if(type == "PLACE_PENDING")
      {
         ok = ExecPlacePending(cmds[i], ticket, retcode, err);
         extra = StringFormat(",\"ticket\":%I64u,\"retcode\":%d", ticket, retcode);
         if(InpVerbose)
            Print(ok ? "✅ " : "❌ ", "PLACE_PENDING ", JsonGetString(cmds[i], "order_type"),
                  " ", JsonGetString(cmds[i], "symbol"),
                  " @ ", DoubleToString(JsonGetNumber(cmds[i], "price"), _Digits),
                  " lot ", DoubleToString(JsonGetNumber(cmds[i], "lot"), 2),
                  ok ? (" → #" + IntegerToString((long)ticket)) : (" | " + err));
      }
      else if(type == "CLOSE_ALL")
      {
         int closed = 0, cancelled = 0;
         ok = ExecCloseAll(cmds[i], closed, cancelled, err);
         extra = StringFormat(",\"closed\":%d,\"cancelled\":%d", closed, cancelled);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "CLOSE_ALL ", JsonGetString(cmds[i], "symbol"),
                  " → closed ", closed, " cancelled ", cancelled,
                  (err == "" ? "" : " | " + err));
      }
      else
      {
         err = "unknown type " + type;
         if(InpVerbose) Print("❓ ", err);
      }

      //--- escape any quotes in err (keep it simple — strip them)
      StringReplace(err, "\"", "'");
      string one = StringFormat("{\"id\":\"%s\",\"ok\":%s%s,\"error\":\"%s\"}",
                                id, (ok ? "true" : "false"), extra, err);
      results += (i == 0 ? "" : ",") + one;
   }

   string ackBody = StringFormat("{\"account\":\"%s\",\"results\":[%s]}", gAccount, results);
   string ackResp;
   int ackCode = HttpPost(InpBridgeURL + "/exec/ack", ackBody, ackResp);
   if(ackCode != 200)
      Print("⚠️  ack POST returned ", ackCode, " — commands executed but not confirmed: ",
            StringSubstr(ackResp, 0, 200));
}

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   gAccount = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));

   bool tradeAllowed = TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) &&
                       MQLInfoInteger(MQL_TRADE_ALLOWED);
   Print("─────────────────────────────────────────────");
   Print("✅ FBExecBridge v1.00 — thin Python-driven executor");
   Print("    Account:   ", gAccount);
   Print("    Bridge:    ", InpBridgeURL, "  (poll ", InpPollMs, "ms)");
   Print("    Magic:     ", InpMagic);
   Print("    AutoTrade: ", tradeAllowed ? "✅ ENABLED" : "❌ DISABLED (Ctrl+E)");
   Print("    Token:     ", (InpToken == "" ? "none" : "set"));

   //--- health probe: one poll (also surfaces a missing WebRequest whitelist
   //    and seeds the server's venue-quote cache)
   string resp;
   int code = HttpPost(InpBridgeURL + "/exec/poll",
                       StringFormat("{\"account\":\"%s\",\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f}",
                                    gAccount, _Symbol,
                                    SymbolInfoDouble(_Symbol, SYMBOL_BID),
                                    SymbolInfoDouble(_Symbol, SYMBOL_ASK)), resp);
   if(code == 200) Print("    Bridge health: ✅ reachable");
   else            Print("⚠️  Bridge UNREACHABLE (code ", code,
                         "). Whitelist the URL and start the Python server.");
   Print("─────────────────────────────────────────────");

   EventSetMillisecondTimer(InpPollMs);
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   PollAndExecute();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("🛑 FBExecBridge stopped. Reason: ", reason);
}

//--- OnTick unused (timer-driven), but required for an EA
void OnTick() {}
//+------------------------------------------------------------------+
