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
#property version   "1.03"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>

input string InpBridgeURL   = "http://127.0.0.1:5000"; // Python bridge base URL (whitelist host!)
input int    InpPollMs      = 1000;                    // Poll interval (ms)
input int    InpTimeoutMs   = 4000;                    // WebRequest timeout (ms)
input string InpToken       = "";                      // X-FB-Token (must match server FB_EXEC_TOKEN; blank = none)
input int    InpMagic       = 770000;                  // Magic BASE; server sends base+strat*10+tf (1m/5m/15m/1H × strategy)
input int    InpMagicRange   = 110;                    // EA owns magics [InpMagic, InpMagic+InpMagicRange)
input int    InpSlippage    = 20;                      // Deviation (points)
input bool   InpVerbose     = true;                    // Log every command

input group "=== HVN/LVN zone drawing ==="
input bool   InpDrawZones      = true;          // Draw HVN/LVN zones on the chart
input string InpZoneTF         = "15m";         // TF whose VP zones to draw (5m|15m)
input int    InpZoneRefreshSec = 30;            // Zone redraw interval (s)
input color  InpHVNColor       = clrSteelBlue;  // HVN zone fill
input color  InpLVNColor       = clrSandyBrown; // LVN zone fill
input bool   InpShowZoneLabels = true;          // Label zones/levels with name + value
input bool   InpDrawVP         = true;          // Draw computed volume profile
input int    InpVPMaxBars      = 50;            // VP histogram max width (bars)
input bool   InpVPLeft         = true;          // Anchor VP to left of visible range (VPFR style); false = right margin
input color  InpVPColor        = clrDimGray;    // VP histogram bar colour
input color  InpVPPocColor     = clrGoldenrod;  // VP histogram POC (max) bar colour

input group "=== Bollinger / squeeze pane ==="
input bool   InpShowBB          = true;   // Draw 3σ Bollinger Bands on the price chart
input bool   InpShowSqueezePane = true;   // Attach the FBSqueeze BBW-percentile subwindow
input int    InpBBPeriod        = 20;     // BB period (MATCH server squeeze_bb_period)
input double InpBBMult          = 3.0;    // BB σ multiple (MATCH squeeze_bb_mult)
input double InpBBWPctThr        = 0.15;  // compression pct (MATCH squeeze_bbw_pct)
input int    InpBBWWindow       = 100;    // BBW window (MATCH squeeze_bbw_window)
input int    InpSqMinOn         = 6;      // min coil bars (MATCH squeeze_min_on_bars)

input group "=== Corner dashboard ==="
input bool   InpShowDash        = true;          // Show trigger/HVN/hedged-loss panel (top-right)
input int    InpDashFontSize    = 9;             // Dashboard font size
input color  InpDashColor       = clrWhite;      // Dashboard text colour

input group "=== Equity target (hard local stop) ==="
input double InpEquityTarget    = 0.0;     // Halt EA when account EQUITY ≥ this ($; 0 = off)
input bool   InpHaltRemovesEA   = false;   // true = ExpertRemove() after flatten; false = stop trading, stay loaded

CTrade        trade;
COrderInfo    orderInfo;
CPositionInfo posInfo;

string   gAccount       = "";
datetime gLastZoneFetch = 0;
bool     gHalted        = false;   // equity target reached → flattened, no more polling/placing

//--- last-armed grid metadata (cached from /exec/zones) for the corner dashboard
double   gFulcrum       = 0.0;
double   gNodeLo        = 0.0;
double   gNodeHi        = 0.0;
string   gTriggerKind   = "";
string   gEmitEdge      = "";

//--- per-magic fulcrums (from each poll response) — hedged loss measures each cycle's
//    legs against ITS OWN fulcrum, so parallel cycles aren't conflated.
long     gFulcMagic[];
double   gFulcPrice[];

//--- HVN → cycle map (from /exec/zones hvn_cycle_map): each entry = one HVN zone
//    with zero or more active cycles anchored inside it.
struct HvnCycleEntry { double lo; double hi; long magic; string tf; string edge; };
HvnCycleEntry gHvnCycles[];   // flat: one row per (HVN, cycle) pair

struct GridCycleInfo { long magic; string tf; string kind; double fulcrum;
                       double tp_up; double tp_down;
                       int buy_n; int sell_n;
                       double net_target; double trail_activate; bool squeeze_ok; };
GridCycleInfo gGridCycles[];   // active cycles from /exec/zones grid_cycles array

//--- chart-overlay indicator handles (3σ Bollinger Bands + FBSqueeze BBW% subwindow)
int      gBBHandle      = INVALID_HANDLE;
int      gSqHandle      = INVALID_HANDLE;
int      gSqSubwin      = -1;

#define ZONE_PREFIX "FBZone_"
#define DASH_PREFIX "FBDash_"   // separate prefix → ClearZones() won't sweep the dashboard

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
int JsonSplitArray(const string js, const string arrKey, string &out[])
{
   ArrayResize(out, 0);
   int k = StringFind(js, "\"" + arrKey + "\"");
   if(k < 0) return 0;
   int open = StringFind(js, "[", k);
   if(open < 0) return 0;

   //--- Depth-aware walk: find the matching ']' for THIS array by bracket depth,
   //    split only its DIRECT child objects (depthArr==1), and skip any '[' ']'
   //    '{' '}' that appear inside quoted strings. Robust to nested arrays/objects
   //    and to brackets inside string values (the old first-']' scan was not).
   int len = StringLen(js);
   int depthArr = 0, depthObj = 0, objStart = -1;
   bool inStr = false;
   for(int i = open; i < len; i++)
   {
      ushort ch = StringGetCharacter(js, i);
      if(inStr)
      {
         if(ch == '\\') { i++; continue; }   // skip escaped char inside a string
         if(ch == '"') inStr = false;
         continue;
      }
      if(ch == '"') { inStr = true; continue; }
      if(ch == '[') { depthArr++; continue; }
      if(ch == ']') { depthArr--; if(depthArr == 0) break; continue; }
      if(depthArr == 1)
      {
         if(ch == '{') { if(depthObj == 0) objStart = i; depthObj++; }
         else if(ch == '}')
         {
            depthObj--;
            if(depthObj == 0 && objStart >= 0)
            {
               int n = ArraySize(out); ArrayResize(out, n + 1);
               out[n] = StringSubstr(js, objStart, i - objStart + 1);
               objStart = -1;
            }
         }
      }
   }
   return ArraySize(out);
}

int JsonSplitCommands(const string js, string &out[]) { return JsonSplitArray(js, "commands", out); }

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
   long   magic     = (long)JsonGetNumber(cmd, "magic");   // 0 → use the default InpMagic
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

   //--- stamp the per-order magic (squeeze legs carry their own); restored after place
   trade.SetExpertMagicNumber(magic > 0 ? magic : InpMagic);

   //--- place with one bounded retry on TRANSIENT broker rejects (off-quotes / price-off
   //    / requote / price-changed / dropped trade-link). The live quote is re-read each
   //    attempt so the freeze guard checks fresh price. Geometry rejects (invalid stops)
   //    and market-closed are NOT retried — they won't self-heal in 200ms, and retrying
   //    a whole grid episode on those just wastes the poll timer.
   bool ok = false;
   for(int attempt = 0; attempt < 2; attempt++)
   {
      if(orderType == "buy_stop")
      {
         double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
         if(price < ask + minStop + point) { err = "buy_stop inside freeze"; retcode = 0; ticket = 0; return false; }
         ok = trade.BuyStop(lot, price, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else if(orderType == "sell_stop")
      {
         double bid = SymbolInfoDouble(sym, SYMBOL_BID);
         if(price > bid - minStop - point) { err = "sell_stop inside freeze"; retcode = 0; ticket = 0; return false; }
         ok = trade.SellStop(lot, price, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else if(orderType == "buy_limit")
      {
         // buy_limit fills on a dip — must sit BELOW the ask by at least the stops level
         double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
         if(price > ask - minStop - point) { err = "buy_limit inside freeze"; retcode = 0; ticket = 0; return false; }
         ok = trade.BuyLimit(lot, price, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else if(orderType == "sell_limit")
      {
         // sell_limit fills on a pop — must sit ABOVE the bid by at least the stops level
         double bid = SymbolInfoDouble(sym, SYMBOL_BID);
         if(price < bid + minStop + point) { err = "sell_limit inside freeze"; retcode = 0; ticket = 0; return false; }
         ok = trade.SellLimit(lot, price, sym, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else
      {
         err = "unknown order_type " + orderType; retcode = 0; ticket = 0; return false;
      }

      retcode = (int)trade.ResultRetcode();
      if(ok) break;
      bool transientRej = (retcode == TRADE_RETCODE_PRICE_OFF    ||  // 10021 "Off quotes" — no firm quote
                           retcode == TRADE_RETCODE_REQUOTE      ||  // 10004
                           retcode == TRADE_RETCODE_PRICE_CHANGED||  // 10020
                           retcode == TRADE_RETCODE_TIMEOUT      ||  // 10012 request timed out
                           retcode == TRADE_RETCODE_CONNECTION);     // 10031 trade link dropped
      if(!transientRej || attempt == 1) break;   // permanent reject, or retry already spent
      Sleep(200);   // brief pause; next loop re-reads quote + freeze guard
   }

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
   long   cmdMagic = (long)JsonGetNumber(cmd, "magic");   // >0 → scope to this cycle only
   closed = 0; cancelled = 0; err = "";
   bool allOk = true;

   //--- snapshot pending tickets, then delete (avoid list-shift)
   ulong pend[];
   for(int i = 0; i < OrdersTotal(); i++)
      if(orderInfo.SelectByIndex(i))
         if(MagicMatch(orderInfo.Magic(), cmdMagic) && (sym == "" || orderInfo.Symbol() == sym))
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
         if(MagicMatch(posInfo.Magic(), cmdMagic) && (sym == "" || posInfo.Symbol() == sym))
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
//| Ownership: an order/position is ours if its magic is any we manage |
//| (default grid OR squeeze). Keeps counts/PnL/close blind to nothing.|
//+------------------------------------------------------------------+
bool IsMine(long magic)
{
   return (magic >= InpMagic && magic < InpMagic + InpMagicRange);
}

//--- scope a sweep: an explicit cmdMagic (>0) targets ONE cycle's pool; 0 = any of ours.
bool MagicMatch(long magic, long cmdMagic)
{
   return (cmdMagic > 0) ? (magic == cmdMagic) : IsMine(magic);
}

//+------------------------------------------------------------------+
//| Count this EA's open positions / pending orders (magic + symbol). |
//+------------------------------------------------------------------+
int CountMyPositions()
{
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(IsMine(posInfo.Magic()) && posInfo.Symbol() == _Symbol) n++;
   return n;
}

int CountMyPendings()
{
   int n = 0;
   for(int i = 0; i < OrdersTotal(); i++)
      if(orderInfo.SelectByIndex(i))
         if(IsMine(orderInfo.Magic()) && orderInfo.Symbol() == _Symbol) n++;
   return n;
}

//+------------------------------------------------------------------+
//| Flatten EVERYTHING this EA owns on this chart's symbol: cancel    |
//| all our pendings + close all our positions. Used by the equity    |
//| target hard-stop (local failsafe, independent of the server).     |
//+------------------------------------------------------------------+
void CloseAllMine(int &closed, int &cancelled)
{
   closed = 0; cancelled = 0;
   ulong pend[];
   for(int i = 0; i < OrdersTotal(); i++)
      if(orderInfo.SelectByIndex(i) && IsMine(orderInfo.Magic()) && orderInfo.Symbol() == _Symbol)
      { int n = ArraySize(pend); ArrayResize(pend, n + 1); pend[n] = orderInfo.Ticket(); }
   for(int i = 0; i < ArraySize(pend); i++)
      if(trade.OrderDelete(pend[i])) cancelled++;

   ulong pos[];
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i) && IsMine(posInfo.Magic()) && posInfo.Symbol() == _Symbol)
      { int n = ArraySize(pos); ArrayResize(pos, n + 1); pos[n] = posInfo.Ticket(); }
   for(int i = 0; i < ArraySize(pos); i++)
      if(trade.PositionClose(pos[i], InpSlippage)) closed++;
}

//--- Per-side open counts + basket floating P&L (for the server cycle monitor).
//    NOTE: POSITION_COMMISSION is often 0 on the open position (commission lands on
//    the deal), so pnl may understate true cost — verify on the demo broker.
int CountMyBuys()
{
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(IsMine(posInfo.Magic()) && posInfo.Symbol() == _Symbol
            && posInfo.PositionType() == POSITION_TYPE_BUY) n++;
   return n;
}

int CountMySells()
{
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(IsMine(posInfo.Magic()) && posInfo.Symbol() == _Symbol
            && posInfo.PositionType() == POSITION_TYPE_SELL) n++;
   return n;
}

double SumMyPnL()
{
   double total = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(IsMine(posInfo.Magic()) && posInfo.Symbol() == _Symbol)
            total += posInfo.Profit() + posInfo.Swap() + posInfo.Commission();
   return total;
}

//+------------------------------------------------------------------+
//| Per-magic open-state breakdown → JSON array for the poll body.    |
//| One object per (strategy×TF) magic that has any position/pending  |
//| so the server can monitor each TF cycle independently.            |
//+------------------------------------------------------------------+
int FindMagic(long &mg[], long m)
{
   for(int k = 0; k < ArraySize(mg); k++) if(mg[k] == m) return k;
   return -1;
}

string BuildMagicsJson()
{
   long   mg[];
   int    buys[], sells[], pend[];
   double buyPnl[], sellPnl[];

   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol || !IsMine(posInfo.Magic())) continue;
      long m = posInfo.Magic();
      int k = FindMagic(mg, m);
      if(k < 0)
      {
         k = ArraySize(mg);
         ArrayResize(mg, k + 1); ArrayResize(buys, k + 1); ArrayResize(sells, k + 1);
         ArrayResize(pend, k + 1); ArrayResize(buyPnl, k + 1); ArrayResize(sellPnl, k + 1);
         mg[k] = m; buys[k] = 0; sells[k] = 0; pend[k] = 0; buyPnl[k] = 0.0; sellPnl[k] = 0.0;
      }
      double p = posInfo.Profit() + posInfo.Swap() + posInfo.Commission();
      if(posInfo.PositionType() == POSITION_TYPE_BUY) { buys[k]++;  buyPnl[k]  += p; }
      else                                            { sells[k]++; sellPnl[k] += p; }
   }
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!orderInfo.SelectByIndex(i)) continue;
      if(orderInfo.Symbol() != _Symbol || !IsMine(orderInfo.Magic())) continue;
      long m = orderInfo.Magic();
      int k = FindMagic(mg, m);
      if(k < 0)
      {
         k = ArraySize(mg);
         ArrayResize(mg, k + 1); ArrayResize(buys, k + 1); ArrayResize(sells, k + 1);
         ArrayResize(pend, k + 1); ArrayResize(buyPnl, k + 1); ArrayResize(sellPnl, k + 1);
         mg[k] = m; buys[k] = 0; sells[k] = 0; pend[k] = 0; buyPnl[k] = 0.0; sellPnl[k] = 0.0;
      }
      pend[k]++;
   }

   string js = "";
   for(int k = 0; k < ArraySize(mg); k++)
      js += (k == 0 ? "" : ",") +
            StringFormat("{\"magic\":%I64d,\"buys\":%d,\"sells\":%d,\"pendings\":%d,"
                         "\"pnl\":%.2f,\"buy_pnl\":%.2f,\"sell_pnl\":%.2f}",
                         mg[k], buys[k], sells[k], pend[k],
                         buyPnl[k] + sellPnl[k], buyPnl[k], sellPnl[k]);
   return js;
}

//+------------------------------------------------------------------+
//| Cancel this EA's pending orders ONLY (leave positions). The safe   |
//| re-arm clear — can never flatten a live position.                  |
//+------------------------------------------------------------------+
bool ExecCancelPendings(const string cmd, int &cancelled, string &err)
{
   string sym = JsonGetString(cmd, "symbol");
   long   cmdMagic = (long)JsonGetNumber(cmd, "magic");   // >0 → scope to this cycle only
   cancelled = 0; err = "";
   bool allOk = true;

   ulong pend[];
   for(int i = 0; i < OrdersTotal(); i++)
      if(orderInfo.SelectByIndex(i))
         if(MagicMatch(orderInfo.Magic(), cmdMagic) && (sym == "" || orderInfo.Symbol() == sym))
         {
            int n = ArraySize(pend); ArrayResize(pend, n + 1); pend[n] = orderInfo.Ticket();
         }
   for(int i = 0; i < ArraySize(pend); i++)
   {
      if(trade.OrderDelete(pend[i])) cancelled++;
      else { allOk = false; err = "cancel fail #" + IntegerToString((long)pend[i]); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Close a FRACTION of one side's positions (bias-side profit book). |
//| Scoped to the command's magic + side; closes ceil(count·frac).    |
//+------------------------------------------------------------------+
bool ExecCloseSide(const string cmd, int &closed, string &err)
{
   string sym   = JsonGetString(cmd, "symbol");
   long   magic = (long)JsonGetNumber(cmd, "magic");
   string side  = JsonGetString(cmd, "side");
   double frac  = JsonGetNumber(cmd, "frac"); if(frac <= 0.0) frac = 0.5;
   ENUM_POSITION_TYPE want = (side == "buy") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   closed = 0; err = ""; bool allOk = true;

   ulong tk[];
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(posInfo.Magic() == magic && posInfo.Symbol() == sym
            && posInfo.PositionType() == want)
         {
            int n = ArraySize(tk); ArrayResize(tk, n + 1); tk[n] = posInfo.Ticket();
         }
   int total   = ArraySize(tk);
   int toClose = (int)MathCeil(total * frac);
   for(int i = 0; i < toClose && i < total; i++)
   {
      if(trade.PositionClose(tk[i], InpSlippage)) closed++;
      else { allOk = false; err = "close fail #" + IntegerToString((long)tk[i]); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Modify pending stop orders for a magic: update price + TP.       |
//| Legs are identified by side ("buy"/"sell"/""=both) and comment   |
//| prefix. price_delta shifts each leg by the given amount; tp       |
//| replaces the per-order TP when > 0. Skips orders already inside  |
//| the broker freeze band at the new price.                          |
//+------------------------------------------------------------------+
bool ExecModifyPending(const string cmd, int &modified, string &err)
{
   string sym        = JsonGetString(cmd, "symbol");
   long   cmdMagic   = (long)JsonGetNumber(cmd, "magic");
   double priceDelta = JsonGetNumber(cmd, "price_delta");  // shift all legs by this amount
   double newTp      = JsonGetNumber(cmd, "tp");           // 0 = leave TP unchanged
   string side       = JsonGetString(cmd, "side");         // "buy","sell","" = both
   modified = 0; err = ""; bool allOk = true;

   int    digits    = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point     = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stopsPts  = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double minStop   = stopsPts * point;

   ulong tickets[];
   double prices[], tps[];
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!orderInfo.SelectByIndex(i)) continue;
      if(!MagicMatch(orderInfo.Magic(), cmdMagic)) continue;
      if(sym != "" && orderInfo.Symbol() != sym) continue;
      ENUM_ORDER_TYPE ot = orderInfo.OrderType();
      bool isBuy  = (ot == ORDER_TYPE_BUY_STOP  || ot == ORDER_TYPE_BUY_LIMIT);
      bool isSell = (ot == ORDER_TYPE_SELL_STOP || ot == ORDER_TYPE_SELL_LIMIT);
      if(side == "buy"  && !isBuy)  continue;
      if(side == "sell" && !isSell) continue;
      double newPrice = NormalizeDouble(orderInfo.PriceOpen() + priceDelta, digits);
      double useTp    = (newTp > 0) ? NormalizeDouble(newTp, digits)
                                    : NormalizeDouble(orderInfo.TakeProfit(), digits);
      // freeze guard: buy_stop must be above ask+minStop; sell_stop below bid-minStop
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      if(isBuy  && newPrice < ask + minStop + point) continue;
      if(isSell && newPrice > bid - minStop - point) continue;
      int n = ArraySize(tickets);
      ArrayResize(tickets, n+1); ArrayResize(prices, n+1); ArrayResize(tps, n+1);
      tickets[n] = orderInfo.Ticket(); prices[n] = newPrice; tps[n] = useTp;
   }
   for(int i = 0; i < ArraySize(tickets); i++)
   {
      if(trade.OrderModify(tickets[i], prices[i], 0.0, tps[i], ORDER_TIME_GTC, 0)) modified++;
      else { allOk = false; err = "modify fail #" + IntegerToString((long)tickets[i]); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Move one side's positions' SL to breakeven (risk-free runner).    |
//+------------------------------------------------------------------+
bool ExecMoveBE(const string cmd, int &moved, string &err)
{
   string sym   = JsonGetString(cmd, "symbol");
   long   magic = (long)JsonGetNumber(cmd, "magic");
   string side  = JsonGetString(cmd, "side");
   ENUM_POSITION_TYPE want = (side == "buy") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   moved = 0; err = ""; bool allOk = true;

   double point    = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stopsPts = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double minStop  = stopsPts * point;     // SL must clear this from market or broker rejects
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);

   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != sym
         || posInfo.PositionType() != want) continue;
      double be = posInfo.PriceOpen();
      double tp = posInfo.TakeProfit();
      // only tighten toward breakeven (never loosen an existing protective stop)
      double curSL = posInfo.StopLoss();
      bool improve = (want == POSITION_TYPE_BUY) ? (curSL <= 0 || be > curSL)
                                                 : (curSL <= 0 || be < curSL);
      if(!improve) continue;
      // Freeze guard: BE-SL must clear the broker stops-level from the CURRENT market, or
      // PositionModify is rejected. When price is still within minStop of entry (BE inside
      // the freeze band) we CAN'T set it yet — skip quietly (ok, moved=0) so the server
      // doesn't spam-retry the same un-settable BE every poll (was 100s of "modify fail").
      // A BUY's SL sits below price (must be ≤ bid-minStop); a SELL's above (≥ ask+minStop).
      bool blocked = (want == POSITION_TYPE_BUY) ? (be > bid - minStop)
                                                 : (be < ask + minStop);
      if(blocked) continue;   // can't place BE here yet — not a failure, just not now
      if(trade.PositionModify(posInfo.Ticket(), be, tp)) moved++;
      else { allOk = false; err = "modify fail #" + IntegerToString((long)posInfo.Ticket()); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Refresh TP on one side's FILLED positions (SL left unchanged).    |
//| Pending legs track the HVN via MODIFY_PENDING; this keeps filled   |
//| legs on the same moving structural target. side ""=both.           |
//+------------------------------------------------------------------+
bool ExecModifyPosition(const string cmd, int &modified, string &err)
{
   string sym   = JsonGetString(cmd, "symbol");
   long   magic = (long)JsonGetNumber(cmd, "magic");
   string side  = JsonGetString(cmd, "side");
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double newTp = NormalizeDouble(JsonGetNumber(cmd, "tp"), digits);
   modified = 0; err = ""; bool allOk = true;
   if(newTp <= 0) { err = "no tp"; return false; }

   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != sym) continue;
      bool isBuy = (posInfo.PositionType() == POSITION_TYPE_BUY);
      if(side == "buy"  && !isBuy) continue;
      if(side == "sell" &&  isBuy) continue;
      double curTp = NormalizeDouble(posInfo.TakeProfit(), digits);
      if(MathAbs(curTp - newTp) < SymbolInfoDouble(sym, SYMBOL_POINT)) continue;  // no-op
      double sl = posInfo.StopLoss();   // keep existing SL
      if(trade.PositionModify(posInfo.Ticket(), sl, newTp)) modified++;
      else { allOk = false; err = "modify fail #" + IntegerToString((long)posInfo.Ticket()); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Poll the queue, execute commands, build + POST the ack array.    |
//+------------------------------------------------------------------+
void PollAndExecute()
{
   //--- report live quote (rebasing) + open-state (cycle monitor): per-side counts
   //    and basket floating P&L drive the server-side exit triggers.
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   int    buys  = CountMyBuys();
   int    sells = CountMySells();
   string magicsJson = BuildMagicsJson();   // per-(strategy×TF) breakdown for per-TF cycles
   long   stopsPts = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);  // broker freeze (points)
   double point    = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   // Aggregate fields kept for back-compat/diagnostics; `magics` drives the per-TF monitor.
   // stops_pts/point let the server floor the grid step so no leg lands inside the freeze.
   double acctBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   double acctEquity  = AccountInfoDouble(ACCOUNT_EQUITY);
   string pollBody = StringFormat(
      "{\"account\":\"%s\",\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"positions\":%d,\"pendings\":%d,\"buys\":%d,\"sells\":%d,\"pnl\":%.2f,"
      "\"stops_pts\":%d,\"point\":%.5f,\"balance\":%.2f,\"equity\":%.2f,\"magics\":[%s]}",
      gAccount, _Symbol, bid, ask, buys + sells, CountMyPendings(), buys, sells, SumMyPnL(),
      (int)stopsPts, point, acctBalance, acctEquity, magicsJson);
   string resp;
   int code = HttpPost(InpBridgeURL + "/exec/poll", pollBody, resp);
   if(code != 200) return;

   //--- cache per-magic fulcrums (every poll, even with no commands) for the
   //    hedged-loss dashboard: each cycle measured against ITS OWN fulcrum.
   string fz[];
   int nf = JsonSplitArray(resp, "fulcrums", fz);
   ArrayResize(gFulcMagic, nf); ArrayResize(gFulcPrice, nf);
   for(int i = 0; i < nf; i++)
   {
      gFulcMagic[i] = (long)JsonGetNumber(fz[i], "magic");
      gFulcPrice[i] = JsonGetNumber(fz[i], "fulcrum");
   }

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
      else if(type == "CANCEL_PENDINGS")
      {
         int cancelled = 0;
         ok = ExecCancelPendings(cmds[i], cancelled, err);
         extra = StringFormat(",\"cancelled\":%d", cancelled);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "CANCEL_PENDINGS ", JsonGetString(cmds[i], "symbol"),
                  " → cancelled ", cancelled, (err == "" ? "" : " | " + err));
      }
      else if(type == "CLOSE_SIDE")
      {
         int closedN = 0;
         ok = ExecCloseSide(cmds[i], closedN, err);
         extra = StringFormat(",\"closed\":%d", closedN);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "CLOSE_SIDE ", JsonGetString(cmds[i], "side"),
                  " → booked ", closedN, (err == "" ? "" : " | " + err));
      }
      else if(type == "MOVE_BE")
      {
         int movedN = 0;
         ok = ExecMoveBE(cmds[i], movedN, err);
         extra = StringFormat(",\"moved\":%d", movedN);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "MOVE_BE ", JsonGetString(cmds[i], "side"),
                  " → moved ", movedN, (err == "" ? "" : " | " + err));
      }
      else if(type == "MODIFY_PENDING")
      {
         int modN = 0;
         ok = ExecModifyPending(cmds[i], modN, err);
         extra = StringFormat(",\"modified\":%d", modN);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "MODIFY_PENDING magic=", (long)JsonGetNumber(cmds[i],"magic"),
                  " delta=", DoubleToString(JsonGetNumber(cmds[i],"price_delta"),_Digits),
                  " → modified ", modN, (err == "" ? "" : " | " + err));
      }
      else if(type == "MODIFY_POSITION")
      {
         int modN = 0;
         ok = ExecModifyPosition(cmds[i], modN, err);
         extra = StringFormat(",\"modified\":%d", modN);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "MODIFY_POSITION ", JsonGetString(cmds[i],"side"),
                  " tp=", DoubleToString(JsonGetNumber(cmds[i],"tp"),_Digits),
                  " → modified ", modN, (err == "" ? "" : " | " + err));
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
//| HVN/LVN zone drawing (rebased zones fetched from /exec/zones)     |
//+------------------------------------------------------------------+
void ClearZones()
{
   // Clear ALL of our objects (rectangles, VP-level HLINEs, fulcrum) — they're all
   // redrawn each refresh from the fresh /exec/zones payload.
   int total = ObjectsTotal(0, -1, -1);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, -1, -1);
      if(StringFind(name, ZONE_PREFIX) == 0) ObjectDelete(0, name);
   }
}

//--- Draw a VP point-level (POC/VAH/VAL/naked-POC) the grid triggers on, as a labeled
//    dotted HLINE. Colour by kind so they're distinct from the magenta-dashed fulcrum.
void DrawLevel(const string kind, double price)
{
   string name = ZONE_PREFIX + "lvl_" + kind;
   color  clr  = clrGray;
   if(kind == "poc")            clr = clrGold;
   else if(kind == "vah" || kind == "val") clr = clrDodgerBlue;
   else if(kind == "naked_poc") clr = clrOrangeRed;
   else if(kind == "poc_today") clr = clrYellow;
   else if(kind == "vah_today" || kind == "val_today") clr = clrDeepSkyBlue;
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, name, OBJPROP_BACK,  true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString (0, name, OBJPROP_TEXT, kind);

   if(InpShowZoneLabels)
   {
      string lbl = (kind == "poc_today") ? "POC·D"
                 : (kind == "vah_today") ? "VAH·D"
                 : (kind == "val_today") ? "VAL·D" : kind; StringToUpper(lbl);
      ZoneText("lvltxt_" + kind, TimeCurrent() + 20 * PeriodSeconds(PERIOD_CURRENT),
               price, lbl + " " + DoubleToString(price, _Digits), clr);
   }
}

//--- Touch-trigger line: green dotted HLINE at a price where a LIVE tap arms an entry
//    (an HVN edge ± hvn_touch_buffer). Named with ZONE_PREFIX so ClearZones sweeps it.
void DrawTouchLine(int idx, const string side, double price)
{
   if(price <= 0) return;
   string name = ZONE_PREFIX + "touch_" + IntegerToString(idx);
   if(ObjectFind(0, name) < 0) ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrLime);
   ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, name, OBJPROP_BACK,  false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString (0, name, OBJPROP_TEXT, "tap-arm " + side);
}

void DrawZone(int idx, const string kind, double lo, double hi)
{
   string name  = ZONE_PREFIX + IntegerToString(idx);
   int    secs  = PeriodSeconds(PERIOD_CURRENT);
   datetime tL  = TimeCurrent() - 120 * secs;
   datetime tR  = TimeCurrent() + 20 * secs;
   // _today variants: lighter/distinct colors for the forming current session zones.
   color  clr;
   bool   fill;
   if(kind == "hvn")        { clr = InpHVNColor;      fill = true;  }
   else if(kind == "lvn")   { clr = InpLVNColor;      fill = false; }
   else if(kind == "hvn_today") { clr = clrCornflowerBlue; fill = true;  }
   else if(kind == "lvn_today") { clr = clrPeachPuff;      fill = false; }
   else                     { clr = InpHVNColor;      fill = false; }

   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, tL, lo, tR, hi);
      ObjectSetInteger(0, name, OBJPROP_BACK,       true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
   }
   else
   {
      ObjectSetInteger(0, name, OBJPROP_TIME,  0, tL);
      ObjectSetInteger(0, name, OBJPROP_TIME,  1, tR);
      ObjectSetDouble (0, name, OBJPROP_PRICE, 0, lo);
      ObjectSetDouble (0, name, OBJPROP_PRICE, 1, hi);
   }
   ObjectSetInteger(0, name, OBJPROP_FILL,    fill);
   ObjectSetInteger(0, name, OBJPROP_COLOR,   clr);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,   1);

   if(InpShowZoneLabels)
   {
      string lbl_prefix = "";
      if(kind == "hvn")         lbl_prefix = "HVN ";
      else if(kind == "lvn")    lbl_prefix = "LVN ";
      else if(kind == "hvn_today") lbl_prefix = "HVN·D ";
      else if(kind == "lvn_today") lbl_prefix = "LVN·D ";
      else                      lbl_prefix = "ZONE ";
      ZoneText("ztxt_" + IntegerToString(idx), tR, hi,
               lbl_prefix + DoubleToString(lo, _Digits) + "–" + DoubleToString(hi, _Digits), clr);
   }
}

//--- ICT overlay primitives (named with ZONE_PREFIX so ClearZones sweeps them) -----
void IctHLine(const string suf, double price, color clr, int style, int width, const string text)
{
   if(price <= 0) return;
   string name = ZONE_PREFIX + suf;
   if(ObjectFind(0, name) < 0) ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_BACK,  false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString (0, name, OBJPROP_TEXT, text);
}

void IctRect(const string suf, datetime tL, datetime tR, double lo, double hi, color clr, bool fill)
{
   if(lo <= 0 || hi <= 0) return;
   if(hi < lo) { double t = lo; lo = hi; hi = t; }
   string name = ZONE_PREFIX + suf;
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, tL, lo, tR, hi);
      ObjectSetInteger(0, name, OBJPROP_BACK,       true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   }
   else
   {
      ObjectSetInteger(0, name, OBJPROP_TIME,  0, tL);
      ObjectSetInteger(0, name, OBJPROP_TIME,  1, tR);
      ObjectSetDouble (0, name, OBJPROP_PRICE, 0, lo);
      ObjectSetDouble (0, name, OBJPROP_PRICE, 1, hi);
   }
   ObjectSetInteger(0, name, OBJPROP_COLOR,   clr);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FILL,    fill);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,   1);
}

//--- Draw the ict_fvg setup (the "why": fib premium zone, entry/HTF FVGs, entry/SL/TP,
//    ChoCh break) from the rebased `ict` object in the /exec/zones payload.
void DrawICT(const string js)
{
   double entry = JsonGetNumber(js, "entry");
   if(entry <= 0) return;
   double sl = JsonGetNumber(js, "sl");
   double tp = JsonGetNumber(js, "tp");
   double fl = JsonGetNumber(js, "fib_lo"),  fh = JsonGetNumber(js, "fib_hi");
   double el = JsonGetNumber(js, "fvg_low"), eh = JsonGetNumber(js, "fvg_high");
   double hl = JsonGetNumber(js, "htf_fvg_low"), hh = JsonGetNumber(js, "htf_fvg_high");
   double ch = JsonGetNumber(js, "choch_level");
   string side = JsonGetString(js, "side");
   string status = JsonGetString(js, "status");
   color  sideClr = (side == "short") ? clrTomato : clrLimeGreen;

   int      secs = PeriodSeconds(PERIOD_CURRENT);
   datetime tL = TimeCurrent() - 120 * secs, tR = TimeCurrent() + 20 * secs;

   IctRect("ict_fib",  tL, tR, fl, fh, clrMediumPurple,   false);  // fib premium zone
   IctRect("ict_fvg",  tL, tR, el, eh, clrSlateGray,      true);   // entry 1m FVG
   IctRect("ict_hfvg", tL, tR, hl, hh, clrDarkSlateGray,  false);  // HTF 15m FVG
   IctHLine("ict_entry", entry, sideClr,  STYLE_SOLID, 2, "ICT " + side + " entry (" + status + ")");
   IctHLine("ict_sl",    sl,    clrRed,    STYLE_DASH,  1, "ICT SL");
   IctHLine("ict_tp",    tp,    clrGreen,  STYLE_DASH,  1, "ICT TP");
   IctHLine("ict_choch", ch,    clrGray,   STYLE_DOT,   1, "ChoCh");
}

//--- Labelled text tag (ZONE_PREFIX so ClearZones sweeps it). Anchored left at (t,price)
//    so the text sits in the right margin beside the zone/level it names.
void ZoneText(const string suf, datetime t, double price, const string text, color clr)
{
   if(price <= 0) return;
   string name = ZONE_PREFIX + suf;
   if(ObjectFind(0, name) < 0) ObjectCreate(0, name, OBJ_TEXT, 0, t, price);
   ObjectSetInteger(0, name, OBJPROP_TIME,  0, t);
   ObjectSetDouble (0, name, OBJPROP_PRICE, 0, price);
   ObjectSetString (0, name, OBJPROP_TEXT,  " " + text);
   ObjectSetString (0, name, OBJPROP_FONT,  "Consolas");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT);
   ObjectSetInteger(0, name, OBJPROP_BACK,  false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}

//--- Draw one VP histogram. t0 = session anchor time; all bars extend rightward.
//    Objects named ZONE_PREFIX+"vp"+profIdx+"_"+binIdx so ClearZones sweeps them.
void DrawOneProfile(const string profJs, const int profIdx)
{
   double vpbin = JsonGetNumber(profJs, "vp_bin");
   if(vpbin <= 0) return;
   string ps[];
   int n = JsonSplitArray(profJs, "profile", ps);
   if(n <= 0) return;

   double price[], vol[];
   ArrayResize(price, n); ArrayResize(vol, n);
   double maxv = 0.0;
   for(int i = 0; i < n; i++)
   {
      price[i] = JsonGetNumber(ps[i], "price");
      vol[i]   = JsonGetNumber(ps[i], "vol");
      if(vol[i] > maxv) maxv = vol[i];
   }
   if(maxv <= 0.0) return;

   int      secs = PeriodSeconds(PERIOD_CURRENT);
   double   half = vpbin / 2.0;

   // Anchor at the session's actual start time (unix ts from server).
   // iBarShift snaps to the nearest bar; iTime gives its chart-aligned open time.
   datetime t0;
   long start_ts = (long)JsonGetNumber(profJs, "start_ts");
   if(start_ts > 0)
   {
      int barIdx = iBarShift(_Symbol, PERIOD_CURRENT, (datetime)start_ts, false);
      t0 = (barIdx >= 0) ? iTime(_Symbol, PERIOD_CURRENT, barIdx) : (datetime)start_ts;
   }
   else if(InpVPLeft)
   {
      int firstBar = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR);
      t0 = iTime(_Symbol, PERIOD_CURRENT, firstBar);
   }
   else
   {
      t0 = TimeCurrent();
   }

   string pfx = ZONE_PREFIX + "vp" + IntegerToString(profIdx) + "_";
   for(int i = 0; i < n; i++)
   {
      double   frac  = vol[i] / maxv;
      datetime t1    = t0 + (int)(frac * InpVPMaxBars * secs) + secs;
      bool     isPoc = (vol[i] >= maxv);
      color    c     = isPoc ? InpVPPocColor : InpVPColor;
      string   name  = pfx + IntegerToString(i);
      if(ObjectFind(0, name) < 0)
      {
         ObjectCreate(0, name, OBJ_RECTANGLE, 0, t0, price[i] - half, t1, price[i] + half);
         ObjectSetInteger(0, name, OBJPROP_BACK,       true);
         ObjectSetInteger(0, name, OBJPROP_FILL,       true);
         ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      }
      else
      {
         ObjectSetInteger(0, name, OBJPROP_TIME,  0, t0);
         ObjectSetInteger(0, name, OBJPROP_TIME,  1, t1);
         ObjectSetDouble (0, name, OBJPROP_PRICE, 0, price[i] - half);
         ObjectSetDouble (0, name, OBJPROP_PRICE, 1, price[i] + half);
      }
      ObjectSetInteger(0, name, OBJPROP_COLOR,   c);
      ObjectSetInteger(0, name, OBJPROP_BGCOLOR, c);
   }
}

//--- Draw session-anchored VP histograms from /exec/zones "profiles" array.
//    Falls back to legacy flat "profile"/"vp_bin" keys if "profiles" is absent.
void DrawProfile(const string js)
{
   string profs[];
   int np = JsonSplitArray(js, "profiles", profs);
   if(np > 0)
   {
      for(int p = 0; p < np; p++)
         DrawOneProfile(profs[p], p);
      return;
   }
   // Legacy fallback: single flat profile, anchored at visible-range left or right margin.
   double vpbin = JsonGetNumber(js, "vp_bin");
   if(vpbin <= 0) return;
   string ps[];
   int n = JsonSplitArray(js, "profile", ps);
   if(n <= 0) return;
   double price[], vol[];
   ArrayResize(price, n); ArrayResize(vol, n);
   double maxv = 0.0;
   for(int i = 0; i < n; i++)
   {
      price[i] = JsonGetNumber(ps[i], "price");
      vol[i]   = JsonGetNumber(ps[i], "vol");
      if(vol[i] > maxv) maxv = vol[i];
   }
   if(maxv <= 0.0) return;
   int      secs = PeriodSeconds(PERIOD_CURRENT);
   double   half = vpbin / 2.0;
   datetime t0;
   if(InpVPLeft) { int fb = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR); t0 = iTime(_Symbol, PERIOD_CURRENT, fb); }
   else          { t0 = TimeCurrent(); }
   for(int i = 0; i < n; i++)
   {
      double   frac = vol[i] / maxv;
      datetime t1   = t0 + (int)(frac * InpVPMaxBars * secs) + secs;
      color    c    = (vol[i] >= maxv) ? InpVPPocColor : InpVPColor;
      string   name = ZONE_PREFIX + "vp0_" + IntegerToString(i);
      if(ObjectFind(0, name) < 0)
      {
         ObjectCreate(0, name, OBJ_RECTANGLE, 0, t0, price[i] - half, t1, price[i] + half);
         ObjectSetInteger(0, name, OBJPROP_BACK, true);
         ObjectSetInteger(0, name, OBJPROP_FILL, true);
         ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      }
      else
      {
         ObjectSetInteger(0, name, OBJPROP_TIME,  0, t0);
         ObjectSetInteger(0, name, OBJPROP_TIME,  1, t1);
         ObjectSetDouble (0, name, OBJPROP_PRICE, 0, price[i] - half);
         ObjectSetDouble (0, name, OBJPROP_PRICE, 1, price[i] + half);
      }
      ObjectSetInteger(0, name, OBJPROP_COLOR,   c);
      ObjectSetInteger(0, name, OBJPROP_BGCOLOR, c);
   }
}

//--- Draw CVD divergence arrows on the chart. Bearish div (price up, delta failed) →
//    red arrow-down above the candle high. Bullish div (price down, delta failed) →
//    lime arrow-up below the candle low. bar_time is the bar's close_ts (unix seconds)
//    from the server; iBarShift maps it to a chart bar index for OBJ_ARROW placement.
//    Objects named ZONE_PREFIX+"cvd_"+idx so ClearZones() sweeps them each refresh.
void DrawCvdArrows(const string js)
{
   string sigs[];
   int n = JsonSplitArray(js, "cvd_signals", sigs);
   for(int i = 0; i < n; i++)
   {
      long     bts  = (long)JsonGetNumber(sigs[i], "bar_time");
      double   px   = JsonGetNumber(sigs[i], "price");
      string   dir  = JsonGetString(sigs[i], "direction");
      if(bts <= 0 || px <= 0) continue;

      datetime bt   = (datetime)bts;
      string   name = ZONE_PREFIX + "cvd_" + IntegerToString(i);

      bool isBear = (dir == "bearish");
      color  clr  = isBear ? clrRed  : clrLime;
      // SYMBOL_ARROWUP=233 points up (bullish), SYMBOL_ARROWDOWN=234 points down (bearish)
      uchar  code = isBear ? 234 : 233;

      if(ObjectFind(0, name) < 0)
         ObjectCreate(0, name, OBJ_ARROW, 0, bt, px);
      ObjectSetInteger(0, name, OBJPROP_TIME,       0, bt);
      ObjectSetDouble (0, name, OBJPROP_PRICE,      0, px);
      ObjectSetInteger(0, name, OBJPROP_ARROWCODE,  code);
      ObjectSetInteger(0, name, OBJPROP_COLOR,      clr);
      ObjectSetInteger(0, name, OBJPROP_WIDTH,      2);
      ObjectSetInteger(0, name, OBJPROP_ANCHOR,     isBear ? ANCHOR_BOTTOM : ANCHOR_TOP);
      ObjectSetInteger(0, name, OBJPROP_BACK,       false);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetString (0, name, OBJPROP_TEXT,       (isBear ? "CVD bear div" : "CVD bull div"));
   }
}

//--- Draw the EXACT node a cycle armed on (node_low..node_high) as a bright outlined
//    rectangle + label, with the touched edge as a solid line. The daily HVN/LVN zones
//    are the cached profile; a grid arms on its per-TF ROLLING VP whose edges can differ
//    (esp. 1m), so without this the touched edge is invisible. Named ZONE_PREFIX → swept.
void DrawArmedNode(int idx, const string tf, const string edge, double lo, double hi, double fulcrum)
{
   if(lo <= 0 || hi <= 0) return;
   if(hi < lo) { double t = lo; lo = hi; hi = t; }
   int      secs = PeriodSeconds(PERIOD_CURRENT);
   datetime tL = TimeCurrent() - 120 * secs, tR = TimeCurrent() + 20 * secs;
   color    clr = clrMagenta;   // ties the node to its magenta-dashed fulcrum line

   string name = ZONE_PREFIX + "arm_" + IntegerToString(idx);
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_RECTANGLE, 0, tL, lo, tR, hi);
      ObjectSetInteger(0, name, OBJPROP_BACK,       false);   // outline on top of zones
      ObjectSetInteger(0, name, OBJPROP_FILL,       false);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
   }
   else
   {
      ObjectSetInteger(0, name, OBJPROP_TIME,  0, tL);
      ObjectSetInteger(0, name, OBJPROP_TIME,  1, tR);
      ObjectSetDouble (0, name, OBJPROP_PRICE, 0, lo);
      ObjectSetDouble (0, name, OBJPROP_PRICE, 1, hi);
   }
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
   ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);

   if(InpShowZoneLabels)
      ZoneText("armtxt_" + IntegerToString(idx), tR, (edge == "top") ? hi : lo,
               "ARM " + tf + (edge == "" ? "" : " " + edge) + " " +
               DoubleToString(lo, _Digits) + "-" + DoubleToString(hi, _Digits), clr);
}

//--- TP lines + label for each active grid cycle. Drawn dashed; color-coded by TF.
//    Objects named ZONE_PREFIX+"cyc_"+magic+"_up/dn/lbl" → swept by ClearZones().
void DrawGridCycles()
{
   int dg = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   int n  = ArraySize(gGridCycles);
   for(int i = 0; i < n; i++)
   {
      string ms   = IntegerToString(gGridCycles[i].magic);
      string ctf  = gGridCycles[i].tf;
      string cknd = gGridCycles[i].kind;
      double cup  = gGridCycles[i].tp_up;
      double cdn  = gGridCycles[i].tp_down;
      double cful = gGridCycles[i].fulcrum;
      double cnet = gGridCycles[i].net_target;
      bool   csq  = gGridCycles[i].squeeze_ok;
      // TF color: 1m=aqua 5m=lime 15m=gold 1h=orange else=silver
      color clr = clrSilver;
      if(ctf == "1m")       clr = clrAqua;
      else if(ctf == "5m")  clr = clrLime;
      else if(ctf == "15m") clr = clrGold;
      else if(ctf == "1h")  clr = clrOrange;

      // TP-up line
      string uname = ZONE_PREFIX + "cyc_" + ms + "_up";
      if(cup > 0)
      {
         if(ObjectFind(0, uname) < 0) ObjectCreate(0, uname, OBJ_HLINE, 0, 0, cup);
         ObjectSetDouble (0, uname, OBJPROP_PRICE, 0, cup);
         ObjectSetInteger(0, uname, OBJPROP_COLOR, clr);
         ObjectSetInteger(0, uname, OBJPROP_STYLE, STYLE_DASH);
         ObjectSetInteger(0, uname, OBJPROP_WIDTH, 1);
         ObjectSetInteger(0, uname, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, uname, OBJPROP_BACK, true);
         ObjectSetString (0, uname, OBJPROP_TEXT, ctf + " " + cknd + " TP↑ tgt=" + DoubleToString(cnet, 0));
      }
      else ObjectDelete(0, uname);

      // TP-down line
      string dname = ZONE_PREFIX + "cyc_" + ms + "_dn";
      if(cdn > 0)
      {
         if(ObjectFind(0, dname) < 0) ObjectCreate(0, dname, OBJ_HLINE, 0, 0, cdn);
         ObjectSetDouble (0, dname, OBJPROP_PRICE, 0, cdn);
         ObjectSetInteger(0, dname, OBJPROP_COLOR, clr);
         ObjectSetInteger(0, dname, OBJPROP_STYLE, STYLE_DASH);
         ObjectSetInteger(0, dname, OBJPROP_WIDTH, 1);
         ObjectSetInteger(0, dname, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, dname, OBJPROP_BACK, true);
         ObjectSetString (0, dname, OBJPROP_TEXT, ctf + " " + cknd + " TP↓ tgt=" + DoubleToString(cnet, 0));
      }
      else ObjectDelete(0, dname);

      // Label at TP-up or fulcrum in the right margin
      double lbPx = (cup > 0) ? cup : ((cful > 0) ? cful : 0.0);
      string lname = ZONE_PREFIX + "cyc_" + ms + "_lbl";
      if(lbPx > 0)
      {
         datetime tR = TimeCurrent() + 20 * PeriodSeconds(PERIOD_CURRENT);
         string lbtxt = ctf + " " + cknd + " net=" + DoubleToString(cnet, 0) + (csq ? " SQ" : "");
         if(ObjectFind(0, lname) < 0) ObjectCreate(0, lname, OBJ_TEXT, 0, tR, lbPx);
         ObjectSetInteger(0, lname, OBJPROP_TIME,  0, tR);
         ObjectSetDouble (0, lname, OBJPROP_PRICE, 0, lbPx);
         ObjectSetString (0, lname, OBJPROP_TEXT,  " " + lbtxt);
         ObjectSetString (0, lname, OBJPROP_FONT,  "Consolas");
         ObjectSetInteger(0, lname, OBJPROP_FONTSIZE, 7);
         ObjectSetInteger(0, lname, OBJPROP_COLOR, clr);
         ObjectSetInteger(0, lname, OBJPROP_ANCHOR, ANCHOR_LEFT);
         ObjectSetInteger(0, lname, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, lname, OBJPROP_BACK,  false);
      }
      else ObjectDelete(0, lname);
   }
}

void FetchAndDrawZones()
{
   string body = StringFormat("{\"account\":\"%s\",\"symbol\":\"%s\",\"tf\":\"%s\"}",
                              gAccount, _Symbol, InpZoneTF);
   string resp;
   int code = HttpPost(InpBridgeURL + "/exec/zones", body, resp);
   if(code != 200) return;

   string zs[];
   int n = JsonSplitArray(resp, "zones", zs);
   ClearZones();
   for(int i = 0; i < n; i++)
   {
      string kind = JsonGetString(zs[i], "kind");
      double lo   = JsonGetNumber(zs[i], "lo");
      double hi   = JsonGetNumber(zs[i], "hi");
      if(lo > 0 && hi > 0) DrawZone(i, kind, lo, hi);
   }

   //--- draw the VP point-levels the grid triggers on (POC/VAH/VAL/naked-POC)
   string lv[];
   int nl = JsonSplitArray(resp, "levels", lv);
   for(int i = 0; i < nl; i++)
   {
      string lk = JsonGetString(lv[i], "kind");
      double lp = JsonGetNumber(lv[i], "price");
      if(lp > 0 && lk != "") DrawLevel(lk, lp);
   }

   //--- green-dotted touch-trigger lines: prices where a LIVE tap arms an entry
   //    (HVN edge ± hvn_touch_buffer). Only present when touch_arm is enabled server-side.
   string tl[];
   int nt = JsonSplitArray(resp, "touch_lines", tl);
   for(int i = 0; i < nt; i++)
   {
      double tp = JsonGetNumber(tl[i], "price");
      string ts = JsonGetString(tl[i], "side");
      if(tp > 0) DrawTouchLine(i, ts, tp);
   }

   //--- armed-node overlay: the EXACT node each active cycle triggered on (any TF), drawn
   //    as a magenta outline so the touched edge is visible even when it sits off the daily
   //    HVN/LVN grid (the per-TF rolling VP differs from the cached daily profile).
   string an[];
   int nan = JsonSplitArray(resp, "armed_nodes", an);
   for(int i = 0; i < nan; i++)
   {
      double alo = JsonGetNumber(an[i], "node_low");
      double ahi = JsonGetNumber(an[i], "node_high");
      double af  = JsonGetNumber(an[i], "fulcrum");
      string atf = JsonGetString(an[i], "tf");
      string aed = JsonGetString(an[i], "edge");
      if(alo > 0 && ahi > 0) DrawArmedNode(i, atf, aed, alo, ahi, af);
   }

   //--- parse hvn_cycle_map: [{lo, hi, cycles:[{magic, tf, edge, ...}]}]
   //    flatten into gHvnCycles[] so UpdateDashboard can display which TF armed each HVN.
   ArrayResize(gHvnCycles, 0);
   string hcm[];
   int nhcm = JsonSplitArray(resp, "hvn_cycle_map", hcm);
   for(int i = 0; i < nhcm; i++)
   {
      double hlo = JsonGetNumber(hcm[i], "lo");
      double hhi = JsonGetNumber(hcm[i], "hi");
      // parse the inner cycles array inside this HVN entry
      string cycs[];
      int nc = JsonSplitArray(hcm[i], "cycles", cycs);
      if(nc == 0)
      {
         // HVN with no active cycle — still record it so dashboard shows "—"
         int idx = ArraySize(gHvnCycles); ArrayResize(gHvnCycles, idx + 1);
         gHvnCycles[idx].lo = hlo; gHvnCycles[idx].hi = hhi;
         gHvnCycles[idx].magic = 0; gHvnCycles[idx].tf = ""; gHvnCycles[idx].edge = "";
      }
      for(int j = 0; j < nc; j++)
      {
         int idx = ArraySize(gHvnCycles); ArrayResize(gHvnCycles, idx + 1);
         gHvnCycles[idx].lo    = hlo;
         gHvnCycles[idx].hi    = hhi;
         gHvnCycles[idx].magic = (long)JsonGetNumber(cycs[j], "magic");
         gHvnCycles[idx].tf    = JsonGetString(cycs[j], "tf");
         gHvnCycles[idx].edge  = JsonGetString(cycs[j], "edge");
      }
   }

   //--- draw the last-armed grid's fulcrum (the touched edge the straddle anchors on)
   double fulcrum  = JsonGetNumber(resp, "fulcrum");
   string emitTF   = JsonGetString(resp, "emit_tf");
   string emitEdge = JsonGetString(resp, "emit_edge");
   string fname    = ZONE_PREFIX + "fulcrum";

   //--- cache arm metadata for the corner dashboard (rendered every tick in OnTimer)
   gFulcrum     = fulcrum;
   gNodeLo      = JsonGetNumber(resp, "node_low");
   gNodeHi      = JsonGetNumber(resp, "node_high");
   gTriggerKind = JsonGetString(resp, "trigger_kind");
   gEmitEdge    = emitEdge;
   if(fulcrum > 0)
   {
      if(ObjectFind(0, fname) < 0)
         ObjectCreate(0, fname, OBJ_HLINE, 0, 0, fulcrum);
      ObjectSetDouble (0, fname, OBJPROP_PRICE, 0, fulcrum);
      ObjectSetInteger(0, fname, OBJPROP_COLOR, clrMagenta);
      ObjectSetInteger(0, fname, OBJPROP_STYLE, STYLE_DASH);
      ObjectSetInteger(0, fname, OBJPROP_WIDTH, 2);
      ObjectSetInteger(0, fname, OBJPROP_BACK,  false);
      ObjectSetInteger(0, fname, OBJPROP_SELECTABLE, false);
      ObjectSetString (0, fname, OBJPROP_TEXT, "fulcrum " + emitTF + " " + emitEdge);
   }
   else ObjectDelete(0, fname);

   //--- ict_fvg overlay (the paper strategy's setup, rebased onto this venue)
   DrawICT(resp);

   //--- CVD divergence arrows: red↓ above bear-div candle, lime↑ below bull-div candle
   DrawCvdArrows(resp);

   //--- computed volume profile (right-margin histogram)
   if(InpDrawVP) DrawProfile(resp);

   //--- active grid cycles: TP lines + right-margin labels (TF / strategy / net_target)
   ArrayResize(gGridCycles, 0);
   string gcs[];
   int ngc = JsonSplitArray(resp, "grid_cycles", gcs);
   ArrayResize(gGridCycles, ngc);
   for(int i = 0; i < ngc; i++)
   {
      gGridCycles[i].magic         = (long)JsonGetNumber(gcs[i], "magic");
      gGridCycles[i].tf            = JsonGetString(gcs[i], "tf");
      gGridCycles[i].kind          = JsonGetString(gcs[i], "kind");
      gGridCycles[i].fulcrum       = JsonGetNumber(gcs[i], "fulcrum");
      gGridCycles[i].tp_up         = JsonGetNumber(gcs[i], "tp_up");
      gGridCycles[i].tp_down       = JsonGetNumber(gcs[i], "tp_down");
      gGridCycles[i].buy_n         = (int)JsonGetNumber(gcs[i], "buy_n");
      gGridCycles[i].sell_n        = (int)JsonGetNumber(gcs[i], "sell_n");
      gGridCycles[i].net_target    = JsonGetNumber(gcs[i], "net_target");
      gGridCycles[i].trail_activate= JsonGetNumber(gcs[i], "trail_activate");
      gGridCycles[i].squeeze_ok    = (JsonGetNumber(gcs[i], "squeeze_ok") > 0.5);
   }
   DrawGridCycles();

   if(InpVerbose && (n > 0 || nl > 0))
      Print("🟦 Drew ", n, " zones + ", nl, " VP levels | fulcrum ",
            DoubleToString(fulcrum, _Digits), " (", emitTF, " ", emitEdge, ")");
}

//+------------------------------------------------------------------+
//| Corner dashboard: trigger, which HVN, max loss if fully hedged    |
//+------------------------------------------------------------------+

//--- Per-magic fulcrum lookup (from the poll response cache). Returns 0 if unknown.
double FulcrumForMagic(long magic)
{
   for(int k = 0; k < ArraySize(gFulcMagic); k++)
      if(gFulcMagic[k] == magic) return gFulcPrice[k];
   return 0.0;
}

//--- Worst-case loss if EVERY grid leg fills (pendings assumed filled) and price returns
//    to the fulcrum: the straddle locks buys-above + sells-below, so the realized loss
//    when whipsawed back to mid = Σ lot·|entry − fulcrum|·money/point. CORRECT for N
//    PARALLEL cycles: each leg is measured against ITS OWN cycle's fulcrum (by magic),
//    never one shared value. Legs whose magic has no known fulcrum are skipped (can't
//    place them on a straddle → not a hedged-whipsaw leg).
double HedgedLossAtFulcrum(int &nOpen, int &nPend)
{
   nOpen = 0; nPend = 0;
   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) return 0.0;
   double perPoint = tickVal / tickSize;   // account currency per 1.0 price unit per lot

   double loss = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol || !IsMine(posInfo.Magic())) continue;
      double f = FulcrumForMagic(posInfo.Magic());
      if(f <= 0) continue;   // unknown cycle fulcrum → don't fabricate a distance
      loss += posInfo.Volume() * MathAbs(posInfo.PriceOpen() - f) * perPoint;
      nOpen++;
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!orderInfo.SelectByIndex(i)) continue;
      if(orderInfo.Symbol() != _Symbol || !IsMine(orderInfo.Magic())) continue;
      double f = FulcrumForMagic(orderInfo.Magic());
      if(f <= 0) continue;
      loss += orderInfo.VolumeInitial() * MathAbs(orderInfo.PriceOpen() - f) * perPoint;
      nPend++;
   }
   return loss;
}

void DashRow(int row, const string text, color clr)
{
   string name = DASH_PREFIX + IntegerToString(row);
   if(ObjectFind(0, name) < 0)
   {
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_CORNER,     CORNER_RIGHT_UPPER);
      ObjectSetInteger(0, name, OBJPROP_ANCHOR,     ANCHOR_RIGHT_UPPER);
      ObjectSetInteger(0, name, OBJPROP_XDISTANCE,  10);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
      ObjectSetString (0, name, OBJPROP_FONT,       "Consolas");
   }
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 18 + row * (InpDashFontSize + 8));
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE,  InpDashFontSize);
   ObjectSetInteger(0, name, OBJPROP_COLOR,     clr);
   ObjectSetString (0, name, OBJPROP_TEXT,      text);
}

void UpdateDashboard()
{
   if(!InpShowDash) return;
   int dg = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   if(gHalted)
   {
      DashRow(0, "▌ FB GRID — HALTED (equity target "
              + DoubleToString(InpEquityTarget, 2) + ")", clrOrange);
      for(int r = 1; r <= 12; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
      return;
   }

   // idle = no live legs of ours AND no armed fulcrum on any cycle
   int nOpen = 0, nPend = 0;
   double loss = HedgedLossAtFulcrum(nOpen, nPend);
   if(nOpen == 0 && nPend == 0 && gFulcrum <= 0)
   {
      DashRow(0, "▌ FB GRID — idle", clrSilver);
      for(int r = 1; r <= 12; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
      return;
   }
   string ccy = AccountInfoString(ACCOUNT_CURRENCY);
   string kind = (gTriggerKind == "" ? "—" : gTriggerKind);
   string nodeStr = (gNodeLo > 0 && gNodeHi > 0)
                    ? DoubleToString(gNodeLo, dg) + "–" + DoubleToString(gNodeHi, dg)
                    : "—";

   DashRow(0, "▌ FB GRID", InpDashColor);
   DashRow(1, "Trigger:  " + kind + (gEmitEdge == "" ? "" : " " + gEmitEdge), InpDashColor);
   DashRow(2, "Trig px:  " + DoubleToString(gFulcrum, dg), clrAqua);
   DashRow(3, "HVN:      " + nodeStr, clrSteelBlue);
   DashRow(4, "Legs:     " + IntegerToString(nOpen) + " open / " + IntegerToString(nPend) + " pend", InpDashColor);
   DashRow(5, "Hedged loss: -" + DoubleToString(loss, 2) + " " + ccy,
           loss > 0 ? clrTomato : clrLimeGreen);

   // HVN → cycle map rows: one row per HVN zone showing which TF cycles are armed
   // Format: "HVN 3215–3228: 5m·top  15m·bot"  or  "HVN 3215–3228: —"
   int row = 6;
   double lastLo = -1;
   string rowText = "";
   int nh = ArraySize(gHvnCycles);
   for(int i = 0; i <= nh; i++)
   {
      bool flush = (i == nh) || (i > 0 && gHvnCycles[i].lo != lastLo);
      if(flush && lastLo >= 0)
      {
         // find hi for this zone
         double lastHi = 0;
         for(int k = 0; k < nh; k++)
            if(gHvnCycles[k].lo == lastLo) { lastHi = gHvnCycles[k].hi; break; }
         string label = "HVN " + DoubleToString(lastLo, dg) + "–" + DoubleToString(lastHi, dg) + ": ";
         DashRow(row, label + (rowText == "" ? "—" : rowText), clrSteelBlue);
         row++;
         rowText = "";
      }
      if(i < nh)
      {
         lastLo = gHvnCycles[i].lo;
         if(gHvnCycles[i].magic > 0)
         {
            string entry = gHvnCycles[i].tf + (gHvnCycles[i].edge != "" ? "·" + gHvnCycles[i].edge : "");
            rowText = (rowText == "" ? entry : rowText + "  " + entry);
         }
      }
   }
   // Active cycle rows: one per active grid cycle (TF / kind / net_target / trail)
   int ngc2 = ArraySize(gGridCycles);
   if(ngc2 > 0)
   {
      DashRow(row, "── Active cycles ──", clrDimGray); row++;
      for(int i = 0; i < ngc2 && i < 8; i++)
      {
         color gcClr = clrSilver;
         if(gGridCycles[i].tf == "1m")       gcClr = clrAqua;
         else if(gGridCycles[i].tf == "5m")  gcClr = clrLime;
         else if(gGridCycles[i].tf == "15m") gcClr = clrGold;
         else if(gGridCycles[i].tf == "1h")  gcClr = clrOrange;
         string gcTxt = gGridCycles[i].tf + " " + gGridCycles[i].kind
                        + "  tgt=" + DoubleToString(gGridCycles[i].net_target, 0)
                        + "  trail=" + DoubleToString(gGridCycles[i].trail_activate, 0)
                        + (gGridCycles[i].squeeze_ok ? " SQ" : "");
         DashRow(row, gcTxt, gcClr); row++;
      }
   }

   // clear any stale rows beyond what we just drew
   for(int r = row; r <= row + 12; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
}

void ClearDashboard()
{
   for(int r = 0; r <= 12; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
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
   Print("✅ FBExecBridge v1.03 — executor + labels + VP histogram + BB/squeeze pane");
   Print("    Account:   ", gAccount);
   Print("    Bridge:    ", InpBridgeURL, "  (poll ", InpPollMs, "ms)");
   Print("    Magic:     ", InpMagic, "..", InpMagic + InpMagicRange - 1, " (strategy×TF range)");
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

   //--- chart overlays: native 3σ Bollinger Bands on the price chart + the FBSqueeze
   //    BBW-percentile subwindow (visual twin of the server squeeze gate). OnDeinit
   //    removes them, so a reload re-adds cleanly (no duplicates).
   if(InpShowBB)
   {
      gBBHandle = iBands(_Symbol, PERIOD_CURRENT, InpBBPeriod, 0, InpBBMult, PRICE_CLOSE);
      if(gBBHandle != INVALID_HANDLE && ChartIndicatorAdd(0, 0, gBBHandle))
         Print("    Bollinger Bands: ✅ ", InpBBPeriod, "/", DoubleToString(InpBBMult, 1), "σ on price");
   }
   if(InpShowSqueezePane)
   {
      gSqHandle = iCustom(_Symbol, PERIOD_CURRENT, "FBSqueeze",
                          InpBBPeriod, InpBBMult, InpBBWPctThr, InpBBWWindow, InpSqMinOn);
      if(gSqHandle == INVALID_HANDLE)
         Print("⚠️  FBSqueeze handle failed — compile mql5/FBSqueeze.mq5 first (iCustom needs FBSqueeze.ex5)");
      else
      {
         gSqSubwin = (int)ChartGetInteger(0, CHART_WINDOWS_TOTAL);   // next free subwindow index
         if(ChartIndicatorAdd(0, gSqSubwin, gSqHandle))
            Print("    Squeeze pane: ✅ subwindow ", gSqSubwin, " (BBW% vs ",
                  DoubleToString(InpBBWPctThr * 100, 0), "% threshold)");
         else
            Print("⚠️  FBSqueeze attach failed (err ", GetLastError(), ")");
      }
   }
   Print("─────────────────────────────────────────────");

   EventSetMillisecondTimer(InpPollMs);
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   //--- equity target hard-stop (local failsafe): once account EQUITY (balance + floating
   //    P&L) reaches the target, flatten everything this EA holds on this symbol and stop
   //    trading. Latched via gHalted so it fires once. Checked BEFORE polling so no new
   //    legs place after.
   if(gHalted) return;
   if(InpEquityTarget > 0.0)
   {
      double eq = AccountInfoDouble(ACCOUNT_EQUITY);
      if(eq >= InpEquityTarget)
      {
         int closed = 0, cancelled = 0;
         CloseAllMine(closed, cancelled);
         gHalted = true;
         Print("🎯 EQUITY TARGET HIT: equity ", DoubleToString(eq, 2), " ≥ ",
               DoubleToString(InpEquityTarget, 2), " → flattened (", closed, " closed, ",
               cancelled, " cancelled). EA HALTED.");
         Alert("FB EA halted: equity ", DoubleToString(eq, 2),
               " reached target ", DoubleToString(InpEquityTarget, 2));
         UpdateDashboard();
         if(InpHaltRemovesEA) ExpertRemove();
         return;
      }
   }

   PollAndExecute();

   //--- redraw HVN/LVN zones on a slower cadence (they change per bar, not per tick)
   if(InpDrawZones && TimeCurrent() - gLastZoneFetch >= InpZoneRefreshSec)
   {
      FetchAndDrawZones();
      gLastZoneFetch = TimeCurrent();
   }

   //--- dashboard refreshes every poll: hedged loss tracks fills in near-real-time,
   //    while trigger/HVN come from the cached arm metadata (updated on zone fetch).
   UpdateDashboard();
}

//--- remove chart indicators we added whose short-name starts with `prefix` (so a
//    reload/recompile re-adds cleanly instead of stacking duplicates).
void DeleteIndicatorsByPrefix(int subwin, const string prefix)
{
   int tot = ChartIndicatorsTotal(0, subwin);
   for(int i = tot - 1; i >= 0; i--)
   {
      string nm = ChartIndicatorName(0, subwin, i);
      if(StringFind(nm, prefix) == 0) ChartIndicatorDelete(0, subwin, nm);
   }
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ClearZones();
   ClearDashboard();

   //--- tear down the BB / squeeze overlays we attached (guarded so we never touch a
   //    user's own manually-added indicators when our toggles were off).
   if(gSqHandle != INVALID_HANDLE && gSqSubwin >= 0)
      DeleteIndicatorsByPrefix(gSqSubwin, "FB Squeeze");
   if(gBBHandle != INVALID_HANDLE)
      DeleteIndicatorsByPrefix(0, "Bands");
   if(gBBHandle != INVALID_HANDLE) IndicatorRelease(gBBHandle);
   if(gSqHandle != INVALID_HANDLE) IndicatorRelease(gSqHandle);

   Print("🛑 FBExecBridge stopped. Reason: ", reason);
}

//--- OnTick unused (timer-driven), but required for an EA
void OnTick() {}
//+------------------------------------------------------------------+
