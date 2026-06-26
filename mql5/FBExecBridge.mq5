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
#property version   "1.04"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>

input string InpBridgeURL   = "http://127.0.0.1:5000"; // Python bridge base URL (whitelist host!)
input int    InpPollMs      = 1000;                    // Poll interval (ms)
input int    InpTimeoutMs   = 4000;                    // WebRequest timeout (ms)
input string InpToken       = "";                      // X-FB-Token (must match server FB_EXEC_TOKEN; blank = none)
input int    InpMagic       = 770000;                  // Magic BASE; server sends base+strat*10+tf (1m/5m/15m/1H × strategy)
input int    InpMagicRange   = 100;                    // EA owns magics [InpMagic, InpMagic+InpMagicRange)
input int    InpSlippage    = 20;                      // Deviation (points)
input bool   InpVerbose     = true;                    // Log every command

input group "=== HVN/LVN zone drawing ==="
input bool   InpDrawZones      = true;          // Draw HVN/LVN zones on the chart
input string InpZoneTF         = "15m";         // TF whose VP zones to draw (5m|15m)
input int    InpZoneRefreshSec = 30;            // Zone redraw interval (s)
input color  InpHVNColor       = clrSteelBlue;  // HVN zone fill
input color  InpLVNColor       = clrSandyBrown; // LVN zone fill
input bool   InpShowZoneLabels = true;          // Label zones/levels with name + value
input bool   InpDrawVP         = true;          // Draw computed volume profile (right margin)
input int    InpVPMaxBars      = 50;            // VP histogram max width (bars)
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
input int    InpDashRowPad      = 14;            // Extra pixels between rows (increase on Wine/Mac)
input color  InpDashColor       = clrWhite;      // Dashboard text colour
input double InpEquityTarget    = -500.0;        // Equity drawdown hard-stop (local failsafe, 0 = off)

struct HvnCycleEntry { double lo, hi; long magic; string tf, edge; };
struct GridCycleEntry { long magic; string tf, kind; double net_target, trail_activate; bool squeeze_ok; };

CTrade        trade;
COrderInfo    orderInfo;
CPositionInfo posInfo;

string   gAccount       = "";
datetime gLastZoneFetch = 0;

//--- last-armed grid metadata (cached from /exec/zones) for the corner dashboard
double   gFulcrum       = 0.0;
double   gNodeLo        = 0.0;
double   gNodeHi        = 0.0;
string   gTriggerKind   = "";
string   gEmitEdge      = "";

//--- per-cycle arrays refreshed from /exec/zones (hvn_cycles + grid_cycles)
HvnCycleEntry  gHvnCycles[];
GridCycleEntry gGridCycles[];

//--- chart-overlay indicator handles (3σ Bollinger Bands + FBSqueeze BBW% subwindow)
int      gBBHandle      = INVALID_HANDLE;
int      gSqHandle      = INVALID_HANDLE;
int      gSqSubwin      = -1;

#define ZONE_PREFIX "FBZone_"
#define DASH_PREFIX "FBDash_"   // separate prefix → ClearZones() won't sweep the dashboard
#define DASH_BG     "FBDash_bg" // black background rectangle for the panel

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

bool JsonGetBool(const string js, const string key)
{
   string pat = "\"" + key + "\"";
   int k = StringFind(js, pat);
   if(k < 0) return false;
   int colon = StringFind(js, ":", k + StringLen(pat));
   if(colon < 0) return false;
   int i = colon + 1;
   while(i < StringLen(js) && StringGetCharacter(js, i) == ' ') i++;
   return StringSubstr(js, i, 4) == "true";
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

double PnlForMagic(long magic)
{
   double total = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(posInfo.Magic() == magic && posInfo.Symbol() == _Symbol)
            total += posInfo.Profit() + posInfo.Swap() + posInfo.Commission();
   return total;
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
   // floor so at least 1 position always remains for MOVE_BE; min 1 if any exist
   int toClose = (total <= 1) ? total : (int)MathMin(MathFloor(total * frac), total - 1);
   for(int i = 0; i < toClose && i < total; i++)
   {
      if(trade.PositionClose(tk[i], InpSlippage)) closed++;
      else { allOk = false; err = "close fail #" + IntegerToString((long)tk[i]); }
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

   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != sym
         || posInfo.PositionType() != want) continue;
      double point   = SymbolInfoDouble(sym, SYMBOL_POINT);
      long   stopLvl = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
      double minDist = MathMax(stopLvl * point, 1.0 * point); // at least 1pt buffer
      double open    = posInfo.PriceOpen();
      double tp      = posInfo.TakeProfit();
      // BE = entry ± minDist so broker stop-level is always satisfied
      double be = (want == POSITION_TYPE_BUY) ? open - minDist : open + minDist;
      // only tighten toward breakeven (never loosen an existing protective stop)
      double curSL = posInfo.StopLoss();
      bool improve = (want == POSITION_TYPE_BUY) ? (curSL <= 0 || be > curSL)
                                                 : (curSL <= 0 || be < curSL);
      if(!improve) continue;
      bool ok2 = false;
      for(int attempt = 0; attempt < 3; attempt++)
      {
         if(trade.PositionModify(posInfo.Ticket(), be, tp)) { ok2 = true; break; }
         int rc = (int)trade.ResultRetcode();
         bool transient = (rc == TRADE_RETCODE_PRICE_OFF || rc == TRADE_RETCODE_REQUOTE ||
                           rc == TRADE_RETCODE_PRICE_CHANGED || rc == TRADE_RETCODE_TIMEOUT ||
                           rc == TRADE_RETCODE_CONNECTION);
         if(!transient || attempt == 2) break;
         Sleep(200);
      }
      if(ok2) moved++;
      else { allOk = false; err = "modify fail #" + IntegerToString((long)posInfo.Ticket()); }
   }
   return allOk;
}

bool ExecModifySL(const string cmd, int &modified, string &err)
{
   string sym        = JsonGetString(cmd, "symbol");
   long   magic      = (long)JsonGetNumber(cmd, "magic");
   string side       = JsonGetString(cmd, "side");
   double lockedPnl  = JsonGetNumber(cmd, "locked_pnl");  // $ amount to lock in
   ENUM_POSITION_TYPE want = (side == "buy") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
   modified = 0; err = ""; bool allOk = true;

   // Sum total lots on this side to convert locked P&L → per-position SL price
   double tickVal  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) { err = "no tick size"; return false; }
   double perPoint = tickVal / tickSize;   // $ per 1.0 price-unit per lot

   double totalLots = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(posInfo.Magic() == magic && posInfo.Symbol() == sym && posInfo.PositionType() == want)
            totalLots += posInfo.Volume();
   if(totalLots <= 0) return true;   // nothing to protect

   double point   = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stopLvl = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double minDist = MathMax(stopLvl * point, 1.0 * point);

   // sl_price for a buy = entry + locked_pnl / (totalLots × perPoint)
   // All positions share the same locked-profit distance from their own entry, so
   // the aggregate P&L at the SL equals lockedPnl regardless of entry spread.
   double pnlPerLotPerPoint = lockedPnl / (totalLots * perPoint);

   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != sym || posInfo.PositionType() != want) continue;
      double open = posInfo.PriceOpen();
      double tp   = posInfo.TakeProfit();
      double sl   = (want == POSITION_TYPE_BUY) ? open + pnlPerLotPerPoint * posInfo.Volume()
                                                : open - pnlPerLotPerPoint * posInfo.Volume();
      // Enforce minimum broker stop distance from current price
      double curBid = SymbolInfoDouble(sym, SYMBOL_BID);
      double curAsk = SymbolInfoDouble(sym, SYMBOL_ASK);
      if(want == POSITION_TYPE_BUY)
         sl = MathMin(sl, curBid - minDist);   // SL must be below current bid
      else
         sl = MathMax(sl, curAsk + minDist);   // SL must be above current ask
      // Only move SL if it's an improvement (never widen a tighter stop)
      double curSL = posInfo.StopLoss();
      bool improve = (want == POSITION_TYPE_BUY) ? (curSL <= 0 || sl > curSL)
                                                 : (curSL <= 0 || sl < curSL);
      if(!improve) continue;
      bool ok2 = false;
      for(int attempt = 0; attempt < 3; attempt++)
      {
         if(trade.PositionModify(posInfo.Ticket(), sl, tp)) { ok2 = true; break; }
         int rc = (int)trade.ResultRetcode();
         bool transient = (rc == TRADE_RETCODE_PRICE_OFF || rc == TRADE_RETCODE_REQUOTE ||
                           rc == TRADE_RETCODE_PRICE_CHANGED || rc == TRADE_RETCODE_TIMEOUT ||
                           rc == TRADE_RETCODE_CONNECTION);
         if(!transient || attempt == 2) break;
         Sleep(200);
      }
      if(ok2) modified++;
      else { allOk = false; err = "sl modify fail #" + IntegerToString((long)posInfo.Ticket()); }
   }
   return allOk;
}

bool ExecModifyTP(const string cmd, int &modified, string &err)
{
   string sym    = JsonGetString(cmd, "symbol");
   long   magic  = (long)JsonGetNumber(cmd, "magic");
   double buy_tp = JsonGetNumber(cmd, "buy_tp");
   double sell_tp= JsonGetNumber(cmd, "sell_tp");
   modified = 0; err = ""; bool allOk = true;

   // Update TP on open positions
   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != sym) continue;
      double tp = (posInfo.PositionType() == POSITION_TYPE_BUY) ? buy_tp : sell_tp;
      if(tp <= 0) continue;
      bool ok2 = false;
      for(int attempt = 0; attempt < 3; attempt++)
      {
         if(trade.PositionModify(posInfo.Ticket(), posInfo.StopLoss(), tp)) { ok2 = true; break; }
         int rc = (int)trade.ResultRetcode();
         bool transient = (rc == TRADE_RETCODE_PRICE_OFF || rc == TRADE_RETCODE_REQUOTE ||
                           rc == TRADE_RETCODE_PRICE_CHANGED || rc == TRADE_RETCODE_TIMEOUT ||
                           rc == TRADE_RETCODE_CONNECTION);
         if(!transient || attempt == 2) break;
         Sleep(200);
      }
      if(ok2) modified++;
      else { allOk = false; err = "pos modify fail #" + IntegerToString((long)posInfo.Ticket()); }
   }
   // Update TP on pending orders
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!orderInfo.SelectByIndex(i)) continue;
      if(orderInfo.Magic() != magic || orderInfo.Symbol() != sym) continue;
      double tp = (orderInfo.OrderType() == ORDER_TYPE_BUY_STOP) ? buy_tp : sell_tp;
      if(tp <= 0) continue;
      bool ok3 = false;
      for(int attempt = 0; attempt < 3; attempt++)
      {
         if(trade.OrderModify(orderInfo.Ticket(), orderInfo.PriceOpen(),
                              orderInfo.StopLoss(), tp, orderInfo.TypeTime(),
                              orderInfo.TimeExpiration())) { ok3 = true; break; }
         int rc = (int)trade.ResultRetcode();
         bool transient = (rc == TRADE_RETCODE_PRICE_OFF || rc == TRADE_RETCODE_REQUOTE ||
                           rc == TRADE_RETCODE_PRICE_CHANGED || rc == TRADE_RETCODE_TIMEOUT ||
                           rc == TRADE_RETCODE_CONNECTION);
         if(!transient || attempt == 2) break;
         Sleep(200);
      }
      if(ok3) modified++;
      else { allOk = false; err = "ord modify fail #" + IntegerToString((long)orderInfo.Ticket()); }
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
   string pollBody = StringFormat(
      "{\"account\":\"%s\",\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"positions\":%d,\"pendings\":%d,\"buys\":%d,\"sells\":%d,\"pnl\":%.2f,"
      "\"stops_pts\":%d,\"point\":%.5f,\"magics\":[%s]}",
      gAccount, _Symbol, bid, ask, buys + sells, CountMyPendings(), buys, sells, SumMyPnL(),
      (int)stopsPts, point, magicsJson);
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
      else if(type == "MODIFY_SL")
      {
         int modN = 0;
         ok = ExecModifySL(cmds[i], modN, err);
         extra = StringFormat(",\"modified\":%d", modN);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "MODIFY_SL magic=", (long)JsonGetNumber(cmds[i], "magic"),
                  " side=", JsonGetString(cmds[i], "side"),
                  " locked_pnl=", JsonGetNumber(cmds[i], "locked_pnl"),
                  " → modified ", modN, (err == "" ? "" : " | " + err));
      }
      else if(type == "MODIFY_TP")
      {
         int modN = 0;
         ok = ExecModifyTP(cmds[i], modN, err);
         extra = StringFormat(",\"modified\":%d", modN);
         if(InpVerbose)
            Print(ok ? "✅ " : "⚠️ ", "MODIFY_TP magic=", (long)JsonGetNumber(cmds[i], "magic"),
                  " buy_tp=", JsonGetNumber(cmds[i], "buy_tp"),
                  " sell_tp=", JsonGetNumber(cmds[i], "sell_tp"),
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
      string lbl = kind; StringToUpper(lbl);
      ZoneText("lvltxt_" + kind, TimeCurrent() + 20 * PeriodSeconds(PERIOD_CURRENT),
               price, lbl + " " + DoubleToString(price, _Digits), clr);
   }
}

void DrawZone(int idx, const string kind, double lo, double hi)
{
   string name  = ZONE_PREFIX + IntegerToString(idx);
   int    secs  = PeriodSeconds(PERIOD_CURRENT);
   datetime tL  = TimeCurrent() - 120 * secs;
   datetime tR  = TimeCurrent() + 20 * secs;
   color  clr   = (kind == "hvn") ? InpHVNColor : InpLVNColor;
   bool   fill  = (kind == "hvn");   // HVN filled, LVN outline → visually distinct

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
      ZoneText("ztxt_" + IntegerToString(idx), tR, hi,
               (kind == "hvn" ? "HVN " : "LVN ") +
               DoubleToString(lo, _Digits) + "–" + DoubleToString(hi, _Digits), clr);
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

//--- Computed volume profile drawn into the future margin (right of last candle).
//    Each bin is split into two rectangles: ASK (buyers, cyan) to the right,
//    BID (sellers, red) to the left of a shared origin at t0. Total width ∝ vol.
//    POC bin: ask=gold, bid=orange. Falls back to a single gray bar when ask/bid absent.
void DrawVPBin(int idx, double price, double half,
               double askFrac, double bidFrac, double totalFrac,
               bool isPoc, int secs)
{
   datetime t0    = TimeCurrent();
   int      maxBars = InpVPMaxBars;

   string nameA = ZONE_PREFIX + "vp_a" + IntegerToString(idx);  // ask (right)
   string nameB = ZONE_PREFIX + "vp_b" + IntegerToString(idx);  // bid (left)

   // Ask bar: t0 → t0 + askFrac * maxBars bars (cyan / gold at POC)
   datetime tA = t0 + (int)(askFrac * maxBars * secs) + secs;
   color    cA = isPoc ? clrGoldenrod   : clrCadetBlue;

   if(ObjectFind(0, nameA) < 0)
   {
      ObjectCreate(0, nameA, OBJ_RECTANGLE, 0, t0, price - half, tA, price + half);
      ObjectSetInteger(0, nameA, OBJPROP_BACK, true); ObjectSetInteger(0, nameA, OBJPROP_FILL, true);
      ObjectSetInteger(0, nameA, OBJPROP_SELECTABLE, false);
   }
   else
   {
      ObjectSetInteger(0, nameA, OBJPROP_TIME, 0, t0); ObjectSetInteger(0, nameA, OBJPROP_TIME, 1, tA);
      ObjectSetDouble (0, nameA, OBJPROP_PRICE, 0, price - half); ObjectSetDouble(0, nameA, OBJPROP_PRICE, 1, price + half);
   }
   ObjectSetInteger(0, nameA, OBJPROP_COLOR, cA); ObjectSetInteger(0, nameA, OBJPROP_BGCOLOR, cA);

   // Bid bar: stacked after ask bar, extends further right (bid share of total width)
   datetime tBstart = tA;
   datetime tBend   = t0 + (int)(totalFrac * maxBars * secs) + secs;
   color    cB = isPoc ? clrDarkOrange : clrIndianRed;

   if(tBend <= tBstart) tBend = tBstart + secs;  // always draw at least 1 bar wide
   if(ObjectFind(0, nameB) < 0)
   {
      ObjectCreate(0, nameB, OBJ_RECTANGLE, 0, tBstart, price - half, tBend, price + half);
      ObjectSetInteger(0, nameB, OBJPROP_BACK, true); ObjectSetInteger(0, nameB, OBJPROP_FILL, true);
      ObjectSetInteger(0, nameB, OBJPROP_SELECTABLE, false);
   }
   else
   {
      ObjectSetInteger(0, nameB, OBJPROP_TIME, 0, tBstart); ObjectSetInteger(0, nameB, OBJPROP_TIME, 1, tBend);
      ObjectSetDouble (0, nameB, OBJPROP_PRICE, 0, price - half); ObjectSetDouble(0, nameB, OBJPROP_PRICE, 1, price + half);
   }
   ObjectSetInteger(0, nameB, OBJPROP_COLOR, cB); ObjectSetInteger(0, nameB, OBJPROP_BGCOLOR, cB);
}

void DrawProfile(const string js)
{
   double vpbin = JsonGetNumber(js, "vp_bin");
   if(vpbin <= 0) return;
   string ps[];
   int n = JsonSplitArray(js, "profile", ps);
   if(n <= 0) return;

   double price[], vol[], askVol[], bidVol[];
   ArrayResize(price, n); ArrayResize(vol, n);
   ArrayResize(askVol, n); ArrayResize(bidVol, n);
   double maxv = 0.0;
   for(int i = 0; i < n; i++)
   {
      price[i]  = JsonGetNumber(ps[i], "price");
      vol[i]    = JsonGetNumber(ps[i], "vol");
      askVol[i] = JsonGetNumber(ps[i], "ask_vol");
      bidVol[i] = JsonGetNumber(ps[i], "bid_vol");
      if(vol[i] > maxv) maxv = vol[i];
   }
   if(maxv <= 0.0) return;

   int    secs = PeriodSeconds(PERIOD_CURRENT);
   double half = vpbin / 2.0;
   for(int i = 0; i < n; i++)
   {
      bool   isPoc     = (vol[i] >= maxv);
      double totalFrac = vol[i] / maxv;
      double askFrac   = (askVol[i] / maxv);
      DrawVPBin(i, price[i], half, askFrac, (bidVol[i] / maxv), totalFrac, isPoc, secs);
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

   //--- computed volume profile (right-margin histogram)
   if(InpDrawVP) DrawProfile(resp);

   //--- active grid cycles (for the dashboard cycle rows)
   string gcs[];
   int ngc = JsonSplitArray(resp, "grid_cycles", gcs);
   ArrayResize(gGridCycles, ngc);
   for(int i = 0; i < ngc; i++)
   {
      gGridCycles[i].magic          = (long)JsonGetNumber(gcs[i], "magic");
      gGridCycles[i].tf             = JsonGetString(gcs[i], "tf");
      gGridCycles[i].kind           = JsonGetString(gcs[i], "trigger_kind");
      gGridCycles[i].net_target     = JsonGetNumber(gcs[i], "net_target");
      gGridCycles[i].trail_activate = JsonGetNumber(gcs[i], "trail_activate");
      gGridCycles[i].squeeze_ok     = JsonGetBool(gcs[i], "squeeze_ok");
   }

   //--- HVN-to-cycle map (for the dashboard zone rows)
   string hvcs[];
   int nhvc = JsonSplitArray(resp, "hvn_cycles", hvcs);
   ArrayResize(gHvnCycles, nhvc);
   for(int i = 0; i < nhvc; i++)
   {
      gHvnCycles[i].lo    = JsonGetNumber(hvcs[i], "lo");
      gHvnCycles[i].hi    = JsonGetNumber(hvcs[i], "hi");
      gHvnCycles[i].magic = (long)JsonGetNumber(hvcs[i], "magic");
      gHvnCycles[i].tf    = JsonGetString(hvcs[i], "tf");
      gHvnCycles[i].edge  = JsonGetString(hvcs[i], "edge");
   }

   if(InpVerbose && (n > 0 || nl > 0))
      Print("🟦 Drew ", n, " zones + ", nl, " VP levels | fulcrum ",
            DoubleToString(fulcrum, _Digits), " (", emitTF, " ", emitEdge, ")");
}

//+------------------------------------------------------------------+
//| Corner dashboard: trigger, which HVN, max loss if fully hedged    |
//+------------------------------------------------------------------+

//--- Worst-case loss if EVERY grid leg fills (pendings assumed filled) and price
//    returns to the fulcrum: the straddle locks buys-above + sells-below, so the
//    realized loss when whipsawed back to mid = Σ lot·|entry − fulcrum|·money/point.
//    Open positions + this-magic pendings are both counted (= "all hedged").
double HedgedLossAtFulcrum(int &nOpen, int &nPend)
{
   nOpen = 0; nPend = 0;
   if(gFulcrum <= 0) return 0.0;
   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) return 0.0;
   double perPoint = tickVal / tickSize;   // account currency per 1.0 price unit per lot

   double loss = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Symbol() != _Symbol || !IsMine(posInfo.Magic())) continue;
      loss += posInfo.Volume() * MathAbs(posInfo.PriceOpen() - gFulcrum) * perPoint;
      nOpen++;
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!orderInfo.SelectByIndex(i)) continue;
      if(orderInfo.Symbol() != _Symbol || !IsMine(orderInfo.Magic())) continue;
      loss += orderInfo.VolumeInitial() * MathAbs(orderInfo.PriceOpen() - gFulcrum) * perPoint;
      nPend++;
   }
   return loss;
}

//--- Closed P&L today (deals) + open floating for one magic. Used for per-strategy day PnL row.
double DailyPnlForMagic(long magic)
{
   datetime dayStart = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
   double closed = 0.0;
   if(HistorySelect(dayStart, TimeCurrent()))
   {
      int total = HistoryDealsTotal();
      for(int i = 0; i < total; i++)
      {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket == 0) continue;
         if((long)HistoryDealGetInteger(ticket, DEAL_MAGIC) != magic) continue;
         if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol) continue;
         ENUM_DEAL_ENTRY ent = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
         if(ent == DEAL_ENTRY_OUT || ent == DEAL_ENTRY_OUT_BY || ent == DEAL_ENTRY_INOUT)
            closed += HistoryDealGetDouble(ticket, DEAL_PROFIT)
                    + HistoryDealGetDouble(ticket, DEAL_SWAP)
                    + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      }
   }
   return closed + PnlForMagic(magic);
}

//--- Net position for one magic: buyLots − sellLots. netSide = "B","S","—".
void NetPosition(long magic, double &netLots, string &netSide)
{
   double buyLots = 0.0, sellLots = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != _Symbol) continue;
      if(posInfo.PositionType() == POSITION_TYPE_BUY) buyLots  += posInfo.Volume();
      else                                            sellLots += posInfo.Volume();
   }
   double net = buyLots - sellLots;
   if(net > 0.001)       { netLots = net;  netSide = "B"; }
   else if(net < -0.001) { netLots = -net; netSide = "S"; }
   else                  { netLots = 0.0;  netSide = "—"; }
}

//--- Black background rectangle behind the label rows. Height resizes to fit.
void DrawDashBg(int totalRows)
{
   int h = 10 + (totalRows + 1) * (InpDashFontSize + InpDashRowPad);
   if(ObjectFind(0, DASH_BG) < 0)
   {
      ObjectCreate(0, DASH_BG, OBJ_RECTANGLE_LABEL, 0, 0, 0);
      ObjectSetInteger(0, DASH_BG, OBJPROP_CORNER,     CORNER_LEFT_UPPER);
      ObjectSetInteger(0, DASH_BG, OBJPROP_XDISTANCE,  0);
      ObjectSetInteger(0, DASH_BG, OBJPROP_YDISTANCE,  0);
      ObjectSetInteger(0, DASH_BG, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, DASH_BG, OBJPROP_HIDDEN,     true);
   }
   ObjectSetInteger(0, DASH_BG, OBJPROP_XSIZE,       410);
   ObjectSetInteger(0, DASH_BG, OBJPROP_YSIZE,       h);
   ObjectSetInteger(0, DASH_BG, OBJPROP_BGCOLOR,     clrBlack);
   ObjectSetInteger(0, DASH_BG, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, DASH_BG, OBJPROP_COLOR,       clrDimGray);
   ObjectSetInteger(0, DASH_BG, OBJPROP_WIDTH,       1);
   ObjectSetInteger(0, DASH_BG, OBJPROP_BACK,        true);
}

void DashRow(int row, const string text, color clr)
{
   string name = DASH_PREFIX + IntegerToString(row);
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER,     CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR,     ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE,  10);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
   ObjectSetString (0, name, OBJPROP_FONT,       "Consolas");
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 18 + row * (InpDashFontSize + InpDashRowPad));
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE,  InpDashFontSize);
   ObjectSetInteger(0, name, OBJPROP_COLOR,     clr);
   ObjectSetString (0, name, OBJPROP_TEXT,      text);
}

void UpdateDashboard()
{
   if(!InpShowDash) return;
   int dg = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   if(gFulcrum <= 0)   // nothing armed → single idle row
   {
      DrawDashBg(1);
      DashRow(0, "■ FB GRID  HALTED  eq≥" + DoubleToString(InpEquityTarget, 0), clrOrange);
      for(int r = 1; r <= 30; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
      return;
   }

   int nOpen = 0, nPend = 0;
   double loss = HedgedLossAtFulcrum(nOpen, nPend);
   if(nOpen == 0 && nPend == 0 && gFulcrum <= 0 && ArraySize(gGridCycles) == 0)
   {
      DrawDashBg(1);
      DashRow(0, "▌ FB GRID  idle", clrDimGray);
      for(int r = 1; r <= 30; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
      return;
   }

   string ccy     = AccountInfoString(ACCOUNT_CURRENCY);
   string kind    = (gTriggerKind == "" ? "—" : gTriggerKind);
   string nodeStr = (gNodeLo > 0 && gNodeHi > 0)
                    ? DoubleToString(gNodeLo, dg) + "–" + DoubleToString(gNodeHi, dg) : "—";
   string edgeTxt = (gEmitEdge == "" ? "" : "  " + gEmitEdge);

   // ── Header block ──────────────────────────────────────────────────
   DashRow(0, "▌ FB GRID", clrWhite);
   DashRow(1, "◈ " + kind + edgeTxt, clrCyan);           // setup name — pops in cyan
   DashRow(2, "⊙ " + DoubleToString(gFulcrum, dg), clrYellow);  // fulcrum price — yellow
   DashRow(3, "≡ " + nodeStr, clrDodgerBlue);             // HVN range — blue
   DashRow(4, IntegerToString(nOpen) + " open  " + IntegerToString(nPend) + " pend",
           clrSilver);
   DashRow(5, "hedge –" + DoubleToString(loss, 2) + " " + ccy,
           loss > 0.01 ? clrRed : clrDimGray);

   // ── HVN → cycle map ───────────────────────────────────────────────
   int row = 6;
   double lastLo = -1;
   string rowText = "";
   int nh = ArraySize(gHvnCycles);
   for(int i = 0; i <= nh; i++)
   {
      bool flush = (i == nh) || (i > 0 && gHvnCycles[i].lo != lastLo);
      if(flush && lastLo >= 0)
      {
         double lastHi = 0;
         for(int k = 0; k < nh; k++)
            if(gHvnCycles[k].lo == lastLo) { lastHi = gHvnCycles[k].hi; break; }
         string lbl = "HVN " + DoubleToString(lastLo, dg) + "–" + DoubleToString(lastHi, dg) + "  ";
         DashRow(row, lbl + (rowText == "" ? "—" : rowText), clrDodgerBlue);
         row++; rowText = "";
      }
      if(i < nh)
      {
         lastLo = gHvnCycles[i].lo;
         if(gHvnCycles[i].magic > 0)
         {
            string ent = gHvnCycles[i].tf + (gHvnCycles[i].edge != "" ? "·" + gHvnCycles[i].edge : "");
            rowText = (rowText == "" ? ent : rowText + "  " + ent);
         }
      }
   }

   // ── Active cycles ─────────────────────────────────────────────────
   int ngc2 = ArraySize(gGridCycles);
   if(ngc2 > 0)
   {
      DashRow(row, "── cycles ──────────────────────────", clrDimGray); row++;
      for(int i = 0; i < ngc2 && i < 8; i++)
      {
         long   mg      = gGridCycles[i].magic;
         string ctf     = gGridCycles[i].tf;
         string cknd    = gGridCycles[i].kind;
         double gcPnl   = PnlForMagic(mg);          // floating only
         double gcDay   = DailyPnlForMagic(mg);     // closed-today + floating
         double netLots; string netSide;
         NetPosition(mg, netLots, netSide);

         // TF accent color (flat/idle state)
         color tfClr = clrSilver;
         if(ctf == "1m")       tfClr = clrAqua;
         else if(ctf == "5m")  tfClr = clrLime;
         else if(ctf == "15m") tfClr = clrGold;
         else if(ctf == "1h")  tfClr = clrOrange;

         // Row color: drive off floating P&L for urgency
         color gcClr;
         if(gcPnl > 0.01)       gcClr = clrChartreuse;
         else if(gcPnl < -0.01) gcClr = clrRed;
         else                   gcClr = tfClr;

         // Format: "► 5m hvn_touch  F:+12.50  D:+45.00  T:750  net:0.25S"
         string fStr = (gcPnl  >= 0 ? "+" : "") + DoubleToString(gcPnl,  2);
         string dStr = (gcDay  >= 0 ? "+" : "") + DoubleToString(gcDay,  2);
         string nStr = (netLots > 0) ? DoubleToString(netLots, 2) + netSide : netSide;
         string gcTxt = "► " + ctf + " " + cknd
                        + "  F:" + fStr
                        + "  D:" + dStr
                        + "  T:" + DoubleToString(gGridCycles[i].net_target, 0)
                        + "  " + nStr
                        + (gGridCycles[i].squeeze_ok ? " ◈" : "");
         DashRow(row, gcTxt, gcClr); row++;
      }
   }

   DrawDashBg(row);
   for(int r = row; r <= row + 15; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
}

void ClearDashboard()
{
   ObjectDelete(0, DASH_BG);
   for(int r = 0; r <= 30; r++) ObjectDelete(0, DASH_PREFIX + IntegerToString(r));
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
   Print("✅ FBExecBridge v1.04 — executor + labels + VP histogram + BB/squeeze pane + black panel");
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
