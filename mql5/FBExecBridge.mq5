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
//|  MULTI-BRANCH (v1.08, 2026-07-29): one EA instance can poll up   |
//|  to 3 independent Python bridges (e.g. 3 branch checkouts on     |
//|  ports 5000/5001/5002), as long as each was started with a       |
//|  DISTINCT FB_MAGIC_BASE so their magics never collide. Slot 2/3  |
//|  are optional — leave InpBridgeURL2/3 blank to run single-branch |
//|  exactly as before. Every branch is polled + executed on the     |
//|  same timer tick, in sequence, each scoped by its own magic       |
//|  range (IsMine() reads a per-iteration active base, not a fixed  |
//|  input). Chart drawing (zones/VP/dashboard) stays tied to slot 1 |
//|  only — three overlapping zone sets on one chart isn't useful;   |
//|  this only affects what's DRAWN, not what trades. The equity-    |
//|  target hard-stop flattens ALL active branches when it fires.    |
//|                                                                  |
//|  Endpoints (whitelist EVERY InpBridgeURL* host under              |
//|    Tools -> Options -> Expert Advisors -> Allow WebRequest):     |
//|    POST {url}/exec/poll  {account}  -> {commands:[...]}          |
//|    POST {url}/exec/ack   {account, results:[...]}                |
//+------------------------------------------------------------------+
#property copyright "Aniket"
#property version   "1.11"
#define EA_VERSION "1.11"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\OrderInfo.mqh>
#include <Trade\PositionInfo.mqh>

input string InpBridgeURL   = "http://127.0.0.1:5000"; // Branch 1 bridge URL (whitelist host!)
input int    InpMagic       = 770000;                  // Branch 1 magic BASE (server FB_MAGIC_BASE must match)
input string InpBridgeURL2  = "";                       // Branch 2 bridge URL — blank = disabled (single-branch mode)
input int    InpMagic2      = 771000;                   // Branch 2 magic BASE
input string InpBridgeURL3  = "";                       // Branch 3 bridge URL — blank = disabled
input int    InpMagic3      = 772000;                   // Branch 3 magic BASE
input int    InpPollMs      = 1000;                    // Poll interval (ms) — applies to the whole cycle (all branches)
input int    InpTimeoutMs   = 4000;                    // WebRequest timeout (ms), per branch
input string InpToken       = "";                      // X-FB-Token (must match server FB_EXEC_TOKEN; blank = none; same token used for all branches)
input int    InpMagicRange   = 150;                    // Each branch owns [its base, base+InpMagicRange) — covers strat decades 0-13 (lvn_edge_touch=13 needs up to base+134)
input int    InpSlippage    = 20;                      // Deviation (points)
input bool   InpVerbose     = true;                    // Log every command

input group "=== HVN/LVN zone drawing ==="
input bool   InpDrawZones      = true;          // Draw HVN/LVN zones on the chart
input string InpZoneTF         = "15m";         // TF whose VP zones to draw (5m|15m)
input int    InpZoneRefreshSec = 30;            // Zone redraw interval (s)
input color  InpHVNColor       = clrSteelBlue;  // HVN zone fill (coarse detection VP)
input color  InpLVNColor       = C'255,228,196'; // LVN zone fill — pale/washed-out (reads as transparent; MQL5 rectangles have no true alpha channel)
input color  InpHVNFineColor   = clrMagenta;    // HVN zone outline (fine tick VP — A/B compare)
input color  InpLVNFineColor   = clrAqua;       // LVN zone outline (fine tick VP — A/B compare)
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
input int    InpDashRowPad      = 14;            // Extra pixels between rows (increase on Wine/Mac)
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

//--- multi-branch: built once in OnInit from the non-blank InpBridgeURL* inputs.
//    gActiveMagicBase is the ownership scope IsMine()/counters read RIGHT NOW —
//    set at the top of each per-branch iteration in OnTimer, so every existing
//    function that filters "is this position/order mine" stays branch-correct
//    without itself knowing about the loop.
string   gBridgeURLs[];
long     gMagicBases[];
long     gActiveMagicBase = 0;   // set from InpMagic in OnInit; reassigned per iteration

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
                       double net_target; double trail_activate; bool squeeze_ok;
                       string trail_status; double bias_peak; string bias_side; };
GridCycleInfo gGridCycles[];   // active cycles from /exec/zones grid_cycles array

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
            "). If 4060: whitelist ", url,
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

   //--- stamp the per-order magic (squeeze legs carry their own); restored after place.
   //    Fallback is the CURRENT branch's base (gActiveMagicBase), not the fixed InpMagic —
   //    a magic=0 command during a branch-2/3 iteration must not fall back to branch 1.
   trade.SetExpertMagicNumber(magic > 0 ? magic : gActiveMagicBase);

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
//| Descending selection-sort of a (ticket, volume) pair by volume.   |
//| Small arrays (leg counts, not thousands) — O(n^2) is fine.        |
//+------------------------------------------------------------------+
void SortTicketsByVolumeDesc(ulong &tickets[], double &vols[])
{
   int n = ArraySize(tickets);
   for(int i = 0; i < n - 1; i++)
   {
      int maxIdx = i;
      for(int j = i + 1; j < n; j++)
         if(vols[j] > vols[maxIdx]) maxIdx = j;
      if(maxIdx != i)
      {
         double tv = vols[i];    vols[i] = vols[maxIdx];    vols[maxIdx] = tv;
         ulong  tt = tickets[i]; tickets[i] = tickets[maxIdx]; tickets[maxIdx] = tt;
      }
   }
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

   //--- snapshot position tickets + lots, sort biggest-lot-first, then close
   ulong pos[]; double posVol[];
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(MagicMatch(posInfo.Magic(), cmdMagic) && (sym == "" || posInfo.Symbol() == sym))
         {
            int n = ArraySize(pos); ArrayResize(pos, n + 1); ArrayResize(posVol, n + 1);
            pos[n] = posInfo.Ticket(); posVol[n] = posInfo.Volume();
         }
   SortTicketsByVolumeDesc(pos, posVol);
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
   return (magic >= gActiveMagicBase && magic < gActiveMagicBase + InpMagicRange);
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
//| Flatten everything owned by the CURRENT gActiveMagicBase's range  |
//| on this chart's symbol: cancel pendings + close positions.        |
//+------------------------------------------------------------------+
void CloseAllMineForActiveBase(int &closed, int &cancelled)
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

//+------------------------------------------------------------------+
//| Flatten EVERYTHING this EA owns across ALL active branches on     |
//| this chart's symbol. Used by the equity target hard-stop (local   |
//| failsafe, independent of any one server) — an account-wide equity |
//| breach should nuke every branch, not just whichever was active on |
//| the poll-loop iteration that happened to be running.               |
//+------------------------------------------------------------------+
void CloseAllMine(int &closed, int &cancelled)
{
   closed = 0; cancelled = 0;
   long savedBase = gActiveMagicBase;
   for(int b = 0; b < ArraySize(gMagicBases); b++)
   {
      gActiveMagicBase = gMagicBases[b];
      int c = 0, x = 0;
      CloseAllMineForActiveBase(c, x);
      closed += c; cancelled += x;
   }
   gActiveMagicBase = savedBase;
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
   int    buys[], sells[], pend[], buyPend[], sellPend[];
   double buyPnl[], sellPnl[], buyLots[], sellLots[];

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
         ArrayResize(buyLots, k + 1); ArrayResize(sellLots, k + 1);
         ArrayResize(buyPend, k + 1); ArrayResize(sellPend, k + 1);
         mg[k] = m; buys[k] = 0; sells[k] = 0; pend[k] = 0; buyPnl[k] = 0.0; sellPnl[k] = 0.0;
         buyLots[k] = 0.0; sellLots[k] = 0.0; buyPend[k] = 0; sellPend[k] = 0;
      }
      double p = posInfo.Profit() + posInfo.Swap() + posInfo.Commission();
      if(posInfo.PositionType() == POSITION_TYPE_BUY) { buys[k]++;  buyPnl[k]  += p; buyLots[k]  += posInfo.Volume(); }
      else                                            { sells[k]++; sellPnl[k] += p; sellLots[k] += posInfo.Volume(); }
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
         ArrayResize(buyLots, k + 1); ArrayResize(sellLots, k + 1);
         ArrayResize(buyPend, k + 1); ArrayResize(sellPend, k + 1);
         mg[k] = m; buys[k] = 0; sells[k] = 0; pend[k] = 0; buyPnl[k] = 0.0; sellPnl[k] = 0.0;
         buyLots[k] = 0.0; sellLots[k] = 0.0; buyPend[k] = 0; sellPend[k] = 0;
      }
      pend[k]++;
      ENUM_ORDER_TYPE ot = orderInfo.OrderType();
      if(ot == ORDER_TYPE_BUY_STOP || ot == ORDER_TYPE_BUY_LIMIT)   buyPend[k]++;
      else                                                          sellPend[k]++;
   }

   string js = "";
   for(int k = 0; k < ArraySize(mg); k++)
      js += (k == 0 ? "" : ",") +
            StringFormat("{\"magic\":%I64d,\"buys\":%d,\"sells\":%d,\"pendings\":%d,"
                         "\"pnl\":%.2f,\"buy_pnl\":%.2f,\"sell_pnl\":%.2f,"
                         "\"buy_lots\":%.4f,\"sell_lots\":%.4f,"
                         "\"buy_pendings\":%d,\"sell_pendings\":%d}",
                         mg[k], buys[k], sells[k], pend[k],
                         buyPnl[k] + sellPnl[k], buyPnl[k], sellPnl[k],
                         buyLots[k], sellLots[k], buyPend[k], sellPend[k]);
   return js;
}

//--- CLOSED Vantage bars for one TF (last `count`, OLDEST-FIRST) as a JSON array —
//    lets candle_sweep detect directly on the EXECUTION venue's own OHLC instead of
//    the analysis-feed (Binance/Bybit) candle, then rebasing. `start=1` skips the
//    forming bar so every emitted bar is closed.
//    ORDER FIX (2026-07-09): a non-series MqlRates array from CopyRates is OLDEST-FIRST
//    (rates[0] = oldest of the requested range). The old `copied-1 downto 0` loop
//    REVERSED that → emitted newest-first, and the Python sweep detector (which reads
//    bars[i-1]=prev, bars[i]=cur, i.e. oldest-first) saw inverted pairs → NEVER detected
//    a real Vantage sweep. Emit ascending (i=0..copied-1) so JSON is truly oldest-first.
string BuildBarsJson(ENUM_TIMEFRAMES period, int count)
{
   MqlRates rates[];
   int copied = CopyRates(_Symbol, period, 1, count, rates);   // start=1 skips forming bar
   if(copied <= 0) return "";
   string js = "";
   for(int i = 0; i < copied; i++)   // rates[] is oldest-first; emit in the same order
   {
      js += (i == 0 ? "" : ",") +
            StringFormat("{\"ts\":%I64d,\"o\":%.5f,\"h\":%.5f,\"l\":%.5f,\"c\":%.5f}",
                         (long)rates[i].time, rates[i].open, rates[i].high,
                         rates[i].low, rates[i].close);
   }
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
   string side = JsonGetString(cmd, "side");              // "buy","sell","" = both
   cancelled = 0; err = "";
   bool allOk = true;

   ulong pend[];
   for(int i = 0; i < OrdersTotal(); i++)
      if(orderInfo.SelectByIndex(i))
         if(MagicMatch(orderInfo.Magic(), cmdMagic) && (sym == "" || orderInfo.Symbol() == sym))
         {
            ENUM_ORDER_TYPE ot = orderInfo.OrderType();
            bool isBuy  = (ot == ORDER_TYPE_BUY_STOP  || ot == ORDER_TYPE_BUY_LIMIT);
            bool isSell = (ot == ORDER_TYPE_SELL_STOP || ot == ORDER_TYPE_SELL_LIMIT);
            if(side == "buy"  && !isBuy)  continue;
            if(side == "sell" && !isSell) continue;
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

   ulong tk[]; double tkVol[];
   for(int i = 0; i < PositionsTotal(); i++)
      if(posInfo.SelectByIndex(i))
         if(posInfo.Magic() == magic && posInfo.Symbol() == sym
            && posInfo.PositionType() == want)
         {
            int n = ArraySize(tk); ArrayResize(tk, n + 1); ArrayResize(tkVol, n + 1);
            tk[n] = posInfo.Ticket(); tkVol[n] = posInfo.Volume();
         }
   SortTicketsByVolumeDesc(tk, tkVol);   // biggest lots booked first; smallest survive as BE runner
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
   double newTp      = JsonGetNumber(cmd, "tp");           // 0 = leave unchanged, <0 = clear to 0
   string side       = JsonGetString(cmd, "side");         // "buy","sell","" = both
   modified = 0; err = ""; bool allOk = true;

   int    digits    = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point     = SymbolInfoDouble(sym, SYMBOL_POINT);
   long   stopsPts  = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   double minStop   = stopsPts * point;

   ulong tickets[];
   double prices[], tps[], sls[];
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
                                    : (newTp < 0) ? 0.0
                                    : NormalizeDouble(orderInfo.TakeProfit(), digits);
      // SL rides the shift. Previously this passed 0.0 to OrderModify, which WIPED the
      // broker-side disaster stop on every fulcrum shift / pending TP refresh — the leg
      // then sat unprotected for the rest of the cycle. Translate it by the same delta
      // as the price so the stop distance from entry stays constant (the ladder shifts
      // rigidly; the stop must shift with it). 0 stays 0.
      double curSl    = orderInfo.StopLoss();
      double useSl    = (curSl > 0) ? NormalizeDouble(curSl + priceDelta, digits) : 0.0;
      // freeze guard: buy_stop must be above ask+minStop; sell_stop below bid-minStop
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      if(isBuy  && newPrice < ask + minStop + point) continue;
      if(isSell && newPrice > bid - minStop - point) continue;
      int n = ArraySize(tickets);
      ArrayResize(tickets, n+1); ArrayResize(prices, n+1); ArrayResize(tps, n+1);
      ArrayResize(sls, n+1);
      tickets[n] = orderInfo.Ticket(); prices[n] = newPrice; tps[n] = useTp; sls[n] = useSl;
   }
   for(int i = 0; i < ArraySize(tickets); i++)
   {
      if(trade.OrderModify(tickets[i], prices[i], sls[i], tps[i], ORDER_TIME_GTC, 0)) modified++;
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
   double newSl = NormalizeDouble(JsonGetNumber(cmd, "sl"), digits);
   modified = 0; err = ""; bool allOk = true;
   // Require at least one of tp or sl to be set.
   bool clearTp = (newTp < 0);
   bool clearSl = (newSl < 0);
   if(newTp <= 0 && newSl <= 0 && !clearTp && !clearSl) { err = "no tp or sl"; return false; }

   for(int i = 0; i < PositionsTotal(); i++)
   {
      if(!posInfo.SelectByIndex(i)) continue;
      if(posInfo.Magic() != magic || posInfo.Symbol() != sym) continue;
      bool isBuy = (posInfo.PositionType() == POSITION_TYPE_BUY);
      if(side == "buy"  && !isBuy) continue;
      if(side == "sell" &&  isBuy) continue;
      double sl = clearSl ? 0.0 : ((newSl > 0) ? newSl : posInfo.StopLoss());    // use cmd SL, clear (<0), or keep existing
      double tp = clearTp ? 0.0 : ((newTp > 0) ? newTp : posInfo.TakeProfit());  // use cmd TP, clear (<0), or keep existing
      double curTp = NormalizeDouble(posInfo.TakeProfit(), digits);
      double curSl = NormalizeDouble(posInfo.StopLoss(), digits);
      double pt    = SymbolInfoDouble(sym, SYMBOL_POINT);
      if(MathAbs(curTp - tp) < pt && MathAbs(curSl - sl) < pt) continue;  // no-op
      if(trade.PositionModify(posInfo.Ticket(), sl, tp)) modified++;
      else { allOk = false; err = "modify fail #" + IntegerToString((long)posInfo.Ticket()); }
   }
   return allOk;
}

//+------------------------------------------------------------------+
//| Poll the queue, execute commands, build + POST the ack array.    |
//+------------------------------------------------------------------+
void PollAndExecute(const string url)
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
   // Vantage-native closed bars for candle_sweep detection (5m/15m) — avoids rebasing
   // the analysis-feed candle onto this venue; the sweep pattern is checked on the
   // SAME OHLC the broker will fill against.
   string bars5m  = BuildBarsJson(PERIOD_M5,  10);
   string bars15m = BuildBarsJson(PERIOD_M15, 10);
   // Self-report the build and the magic window this EA OWNS. Both are silent failure
   // modes the server otherwise cannot see:
   //   ea_version  — the per-leg disaster SL is inert on a build whose
   //                 ExecModifyPending passes sl=0.0, because OrderModify takes an
   //                 ABSOLUTE stop and every fulcrum shift then wipes it. The server
   //                 places the stop and has no way to know it is being discarded.
   //   magic_lo/hi — InpMagicRange gates REPORTING, not execution. A stale chart input
   //                 narrows the window, the EA keeps trading magics it no longer
   //                 reports, and the server runs zero exit logic against live cycles.
   string pollBody = StringFormat(
      "{\"account\":\"%s\",\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"positions\":%d,\"pendings\":%d,\"buys\":%d,\"sells\":%d,\"pnl\":%.2f,"
      "\"stops_pts\":%d,\"point\":%.5f,\"balance\":%.2f,\"equity\":%.2f,"
      "\"ea_version\":\"%s\",\"magic_lo\":%d,\"magic_hi\":%d,\"magics\":[%s],"
      "\"bars_5m\":[%s],\"bars_15m\":[%s]}",
      gAccount, _Symbol, bid, ask, buys + sells, CountMyPendings(), buys, sells, SumMyPnL(),
      (int)stopsPts, point, acctBalance, acctEquity,
      EA_VERSION, (int)InpMagic, (int)(InpMagic + InpMagicRange), magicsJson, bars5m, bars15m);
   string resp;
   int code = HttpPost(url + "/exec/poll", pollBody, resp);
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
   int ackCode = HttpPost(url + "/exec/ack", ackBody, ackResp);
   if(ackCode != 200)
      Print("⚠️  ack POST (", url, ") returned ", ackCode, " — commands executed but not confirmed: ",
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
   // fine tick-VP levels (A/B vs coarse): distinct hues so overlap/offset is obvious
   else if(kind == "poc_fine")  clr = clrMagenta;
   else if(kind == "vah_fine" || kind == "val_fine") clr = clrAqua;
   else if(kind == "naked_poc_fine") clr = clrHotPink;
   // tick-VP levels (count-weighted): lime/orange, distinct from volume-VP
   else if(kind == "poc_tick")  clr = clrLime;
   else if(kind == "vah_tick" || kind == "val_tick") clr = clrOrange;
   else if(kind == "naked_poc_tick") clr = clrTomato;
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
                 : (kind == "val_today") ? "VAL·D"
                 : (kind == "poc_fine")  ? "POC·F"
                 : (kind == "vah_fine")  ? "VAH·F"
                 : (kind == "val_fine")  ? "VAL·F"
                 : (kind == "naked_poc_fine") ? "nPOC·F"
                 : (kind == "poc_tick")  ? "POC·T"
                 : (kind == "vah_tick")  ? "VAH·T"
                 : (kind == "val_tick")  ? "VAL·T"
                 : (kind == "naked_poc_tick") ? "nPOC·T" : kind; StringToUpper(lbl);
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

//--- True when a zone (matched by lo/hi) has a live cycle armed against it
//    (gHvnCycles entry with magic > 0). Small array, linear scan is fine.
bool IsZoneActive(double lo, double hi)
{
   int nh = ArraySize(gHvnCycles);
   for(int i = 0; i < nh; i++)
      if(gHvnCycles[i].magic > 0 && gHvnCycles[i].lo == lo && gHvnCycles[i].hi == hi)
         return true;
   return false;
}

void DrawZone(int idx, const string kind, double lo, double hi, bool isActive = false)
{
   string name  = ZONE_PREFIX + IntegerToString(idx);
   int    secs  = PeriodSeconds(PERIOD_CURRENT);
   datetime tL  = TimeCurrent() - 120 * secs;
   datetime tR  = TimeCurrent() + 20 * secs;
   // _today variants: lighter/distinct colors for the forming current session zones.
   color  clr;
   bool   fill;
   if(kind == "hvn")        { clr = InpHVNColor;      fill = true;  }
   else if(kind == "lvn")   { clr = InpLVNColor;      fill = true;  }  // filled + pale color reads as translucent
   else if(kind == "hvn_today") { clr = clrCornflowerBlue; fill = true;  }
   else if(kind == "lvn_today") { clr = clrPeachPuff;      fill = false; }
   else if(kind == "hvn_fine")  { clr = InpHVNFineColor; fill = false; }  // fine VOLUME-VP HVN (outline)
   else if(kind == "lvn_fine")  { clr = InpLVNFineColor; fill = false; }  // fine VOLUME-VP LVN (outline)
   else if(kind == "hvn_tick")  { clr = clrLime;         fill = false; }  // TICK-VP HVN (count-weighted)
   else if(kind == "lvn_tick")  { clr = clrOrange;       fill = false; }  // TICK-VP LVN (count-weighted)
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
   ObjectSetInteger(0, name, OBJPROP_COLOR,   isActive ? clrGold : clr);   // ACTIVE zone: bright gold border
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,   isActive ? 3 : 1);           // ACTIVE zone: thicker border
   ObjectSetInteger(0, name, OBJPROP_STYLE,   STYLE_SOLID);

   if(InpShowZoneLabels)
   {
      string lbl_prefix = "";
      if(kind == "hvn")         lbl_prefix = "HVN ";
      else if(kind == "lvn")    lbl_prefix = "LVN ";
      else if(kind == "hvn_today") lbl_prefix = "HVN·D ";
      else if(kind == "lvn_today") lbl_prefix = "LVN·D ";
      else if(kind == "hvn_fine") lbl_prefix = "HVN·F ";
      else if(kind == "lvn_fine") lbl_prefix = "LVN·F ";
      else if(kind == "hvn_tick") lbl_prefix = "HVN·T ";
      else if(kind == "lvn_tick") lbl_prefix = "LVN·T ";
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
      // If session open is to the left of the visible range, anchor at visible-range left so
      // the histogram is always on screen.
      int firstBar = (int)ChartGetInteger(0, CHART_FIRST_VISIBLE_BAR);
      datetime visLeft = iTime(_Symbol, PERIOD_CURRENT, firstBar);
      if(t0 < visLeft) t0 = visLeft;
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
      string ctrl = gGridCycles[i].trail_status;
      double cpk  = gGridCycles[i].bias_peak;
      string cbs  = gGridCycles[i].bias_side;
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
         // trail suffix: "TRAIL armed buy@327" (peak tracking, watching giveback%) or
         // "TRAIL BOOKED" (fired — half closed + rest moved to BE, one-shot per cycle)
         string trailtxt = "";
         if(ctrl == "booked")      trailtxt = " | TRAIL BOOKED";
         else if(ctrl == "armed")  trailtxt = " | TRAIL armed " + cbs + "@" + DoubleToString(cpk, 0);
         string lbtxt = ctf + " " + cknd + " net=" + DoubleToString(cnet, 0) + (csq ? " SQ" : "") + trailtxt;
         color  lclr  = (ctrl == "booked") ? clrLime : (ctrl == "armed") ? clrYellow : clr;
         if(ObjectFind(0, lname) < 0) ObjectCreate(0, lname, OBJ_TEXT, 0, tR, lbPx);
         ObjectSetInteger(0, lname, OBJPROP_TIME,  0, tR);
         ObjectSetDouble (0, lname, OBJPROP_PRICE, 0, lbPx);
         ObjectSetString (0, lname, OBJPROP_TEXT,  " " + lbtxt);
         ObjectSetString (0, lname, OBJPROP_FONT,  "Consolas");
         ObjectSetInteger(0, lname, OBJPROP_FONTSIZE, 7);
         ObjectSetInteger(0, lname, OBJPROP_COLOR, lclr);
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

   //--- parse hvn_cycle_map BEFORE drawing zones so DrawZone can mark a zone as
   //    ACTIVE (has a live cycle armed against it) with a distinct border.
   //    [{lo, hi, cycles:[{magic, tf, edge, ...}]}] flattened into gHvnCycles[].
   ArrayResize(gHvnCycles, 0);
   string hcm[];
   int nhcm = JsonSplitArray(resp, "hvn_cycle_map", hcm);
   for(int i = 0; i < nhcm; i++)
   {
      double hlo = JsonGetNumber(hcm[i], "lo");
      double hhi = JsonGetNumber(hcm[i], "hi");
      string cycs[];
      int nc = JsonSplitArray(hcm[i], "cycles", cycs);
      if(nc == 0)
      {
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

   string zs[];
   int n = JsonSplitArray(resp, "zones", zs);
   ClearZones();
   for(int i = 0; i < n; i++)
   {
      string kind = JsonGetString(zs[i], "kind");
      double lo   = JsonGetNumber(zs[i], "lo");
      double hi   = JsonGetNumber(zs[i], "hi");
      if(lo > 0 && hi > 0) DrawZone(i, kind, lo, hi, IsZoneActive(lo, hi));
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
      gGridCycles[i].trail_status  = JsonGetString(gcs[i], "trail_status");
      gGridCycles[i].bias_peak     = JsonGetNumber(gcs[i], "bias_peak");
      gGridCycles[i].bias_side     = JsonGetString(gcs[i], "bias_side");
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

   if(gHalted)
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

//--- append one (url, magicBase) pair to the active-branch arrays.
void AddBranch(const string url, const long magicBase)
{
   int n = ArraySize(gBridgeURLs);
   ArrayResize(gBridgeURLs, n + 1); ArrayResize(gMagicBases, n + 1);
   gBridgeURLs[n] = url; gMagicBases[n] = magicBase;
}

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   gAccount = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));

   //--- build the active-branch list: slot 1 always on, slots 2/3 only if a URL
   //    was given. Duplicate-base check is a startup safety net — running two
   //    slots with the same magic base would make them silently fight over the
   //    same positions.
   ArrayResize(gBridgeURLs, 0); ArrayResize(gMagicBases, 0);
   AddBranch(InpBridgeURL, InpMagic);
   if(StringLen(InpBridgeURL2) > 0) AddBranch(InpBridgeURL2, InpMagic2);
   if(StringLen(InpBridgeURL3) > 0) AddBranch(InpBridgeURL3, InpMagic3);
   for(int a = 0; a < ArraySize(gMagicBases); a++)
      for(int b2 = a + 1; b2 < ArraySize(gMagicBases); b2++)
         if(gMagicBases[a] == gMagicBases[b2])
            Print("⚠️  Branches ", a + 1, " and ", b2 + 1, " share magic base ",
                  gMagicBases[a], " — they WILL collide. Fix FB_MAGIC_BASE on one server.");
   gActiveMagicBase = gMagicBases[0];

   bool tradeAllowed = TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) &&
                       MQLInfoInteger(MQL_TRADE_ALLOWED);
   Print("─────────────────────────────────────────────");
   Print("✅ FBExecBridge v1.08 — multi-branch executor (up to 3) + labels + VP histogram + BB/squeeze pane + black panel");
   Print("    Account:   ", gAccount);
   Print("    Branches:  ", ArraySize(gBridgeURLs), "  (poll ", InpPollMs, "ms, all branches per tick)");
   Print("    AutoTrade: ", tradeAllowed ? "✅ ENABLED" : "❌ DISABLED (Ctrl+E)");
   Print("    Token:     ", (InpToken == "" ? "none" : "set"));

   //--- health probe: one poll per branch (also surfaces a missing WebRequest
   //    whitelist and seeds each server's venue-quote cache)
   for(int i = 0; i < ArraySize(gBridgeURLs); i++)
   {
      string resp;
      int code = HttpPost(gBridgeURLs[i] + "/exec/poll",
                          StringFormat("{\"account\":\"%s\",\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f}",
                                       gAccount, _Symbol,
                                       SymbolInfoDouble(_Symbol, SYMBOL_BID),
                                       SymbolInfoDouble(_Symbol, SYMBOL_ASK)), resp);
      Print("    Branch ", i + 1, ": ", gBridgeURLs[i], "  magic ", gMagicBases[i], "..",
            gMagicBases[i] + InpMagicRange - 1,
            code == 200 ? "  ✅ reachable" : StringFormat("  ⚠️ UNREACHABLE (code %d) — whitelist the URL and start its Python server.", code));
   }

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

   //--- poll + execute EVERY active branch this tick, each scoped to its own magic
   //    range. gActiveMagicBase is set per-iteration so IsMine()/counters/CloseAllMine
   //    all filter correctly without needing to know about the loop themselves.
   for(int b = 0; b < ArraySize(gBridgeURLs); b++)
   {
      gActiveMagicBase = gMagicBases[b];
      PollAndExecute(gBridgeURLs[b]);
   }
   gActiveMagicBase = gMagicBases[0];   // restore branch-1 scope for zones/dashboard below

   //--- redraw HVN/LVN zones on a slower cadence (they change per bar, not per tick).
   //    Branch 1 only — three overlapping zone sets on one chart isn't readable, and
   //    this doesn't affect what trades, only what's drawn.
   if(InpDrawZones && TimeCurrent() - gLastZoneFetch >= InpZoneRefreshSec)
   {
      FetchAndDrawZones();
      gLastZoneFetch = TimeCurrent();
   }

   //--- dashboard refreshes every poll: hedged loss tracks fills in near-real-time,
   //    while trigger/HVN come from the cached arm metadata (updated on zone fetch).
   //    Branch 1 only, same reasoning as zone drawing above.
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
