//+------------------------------------------------------------------+
//|                                                   FBSqueeze.mq5   |
//|   Bollinger BandWidth-Percentile squeeze pane — the visual twin  |
//|   of the Python grid gate (execution/zone_triggers.squeeze_gate).|
//|                                                                  |
//|   Subwindow plots, per bar:                                      |
//|     • BBW(e) = (BB_upper − BB_lower)/mid  with BB(period, mult·σ) |
//|     • Threshold = the BBW value at the bottom `BBWPctThr` of the  |
//|       trailing `BBWWindow` distribution (the compression cutoff)  |
//|     • BBW line turns GREEN while compressed (BBW ≤ threshold)     |
//|     • ▲ arrow on the RELEASE bar (compressed→expanded after a     |
//|       coil of ≥ MinOnBars) = the expansion the straddle plays     |
//|                                                                  |
//|   Self-contained: computes from the chart's own candles. Keep the |
//|   inputs identical to settings.yaml grid_levels.squeeze_* so the  |
//|   pane matches what the server gate actually does.                |
//+------------------------------------------------------------------+
#property copyright "Aniket"
#property version   "1.00"
#property strict
#property indicator_separate_window
#property indicator_buffers 4
#property indicator_plots   3

//--- plot 0: BBW (colour line — gray normal, green compressed)
#property indicator_label1  "BBW"
#property indicator_type1   DRAW_COLOR_LINE
#property indicator_color1  clrDimGray, clrLime
#property indicator_width1  2
//--- plot 1: compression threshold (bottom-pct cutoff of trailing BBW)
#property indicator_label2  "Threshold"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrTomato
#property indicator_style2  STYLE_DOT
//--- plot 2: release (expansion) marker
#property indicator_label3  "Expansion"
#property indicator_type3   DRAW_ARROW
#property indicator_color3  clrGold
#property indicator_width3  2

input int    BBPeriod   = 20;     // Bollinger period (match squeeze_bb_period)
input double BBMult     = 3.0;    // Bollinger σ multiple (match squeeze_bb_mult)
input double BBWPctThr  = 0.15;   // compression = bottom this fraction of trailing BBW (squeeze_bbw_pct)
input int    BBWWindow  = 100;    // trailing window for the percentile (squeeze_bbw_window)
input int    MinOnBars  = 6;      // min consecutive compressed bars before a release counts (squeeze_min_on_bars)

double BBWBuf[];      // plot 0 value
double BBWColBuf[];   // plot 0 colour index (0=gray, 1=green)
double ThrBuf[];      // plot 1 value
double RelBuf[];      // plot 2 arrow (sits at threshold on release bars, else EMPTY)

int OnInit()
{
   SetIndexBuffer(0, BBWBuf,    INDICATOR_DATA);
   SetIndexBuffer(1, BBWColBuf, INDICATOR_COLOR_INDEX);
   SetIndexBuffer(2, ThrBuf,    INDICATOR_DATA);
   SetIndexBuffer(3, RelBuf,    INDICATOR_DATA);

   PlotIndexSetInteger(2, PLOT_ARROW, 233);          // ▲ up-arrow = expansion
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   PlotIndexSetDouble(2, PLOT_EMPTY_VALUE, EMPTY_VALUE);
   IndicatorSetString(INDICATOR_SHORTNAME, "FB Squeeze (BBW%)");
   IndicatorSetInteger(INDICATOR_DIGITS, 5);
   return INIT_SUCCEEDED;
}

//--- population stdev of a closes window ending at e (inclusive), length `period`
double StdevAt(const double &price[], int e, int period)
{
   double m = 0.0;
   for(int i = e - period + 1; i <= e; i++) m += price[i];
   m /= period;
   double s = 0.0;
   for(int i = e - period + 1; i <= e; i++) s += (price[i] - m) * (price[i] - m);
   return MathSqrt(s / period);
}

//--- mean of a closes window ending at e
double MeanAt(const double &price[], int e, int period)
{
   double m = 0.0;
   for(int i = e - period + 1; i <= e; i++) m += price[i];
   return m / period;
}

int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[], const double &high[],
                const double &low[], const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   int firstBBW = BBPeriod - 1;                  // first bar with a BBW value
   int firstThr = firstBBW + BBWWindow - 1;      // first bar with a full percentile window
   if(rates_total <= firstThr) return rates_total;

   ArraySetAsSeries(close, false);

   // Recompute from one bar before prev_calculated (the live bar updates each tick).
   int start = MathMax(firstBBW, prev_calculated - 1);

   // BBW for every bar from firstBBW (cheap; needed as the threshold's own history)
   for(int e = (prev_calculated > firstBBW ? prev_calculated - 1 : firstBBW); e < rates_total; e++)
   {
      double mid = MeanAt(close, e, BBPeriod);
      double sd  = StdevAt(close, e, BBPeriod);
      BBWBuf[e] = (mid > 0.0) ? (2.0 * BBMult * sd) / mid : 0.0;
   }

   for(int e = start; e < rates_total; e++)
   {
      if(e < firstThr)
      {
         ThrBuf[e]    = EMPTY_VALUE;
         BBWColBuf[e] = 0;
         RelBuf[e]    = EMPTY_VALUE;
         continue;
      }
      // threshold = BBW value at the bottom BBWPctThr quantile of the trailing window
      double win[];
      ArrayResize(win, BBWWindow);
      for(int i = 0; i < BBWWindow; i++) win[i] = BBWBuf[e - BBWWindow + 1 + i];
      ArraySort(win);                                  // ascending
      int qi = (int)MathFloor(BBWPctThr * (BBWWindow - 1));
      double thr = win[qi];
      ThrBuf[e] = thr;

      bool compressed = (BBWBuf[e] <= thr);
      BBWColBuf[e] = compressed ? 1 : 0;               // 1 = green

      // release = this bar expanded after a compressed run of >= MinOnBars
      RelBuf[e] = EMPTY_VALUE;
      if(!compressed && e > firstThr)
      {
         double prevThr = ThrBuf[e - 1];
         bool prevCompressed = (prevThr != EMPTY_VALUE && BBWBuf[e - 1] <= prevThr);
         if(prevCompressed)
         {
            int run = 0;
            for(int b = e - 1; b >= firstThr && run < MinOnBars; b--)
            {
               // recompute that bar's threshold cheaply from its own trailing window
               double w2[]; ArrayResize(w2, BBWWindow);
               for(int i = 0; i < BBWWindow; i++) w2[i] = BBWBuf[b - BBWWindow + 1 + i];
               ArraySort(w2);
               if(BBWBuf[b] <= w2[(int)MathFloor(BBWPctThr * (BBWWindow - 1))]) run++;
               else break;
            }
            if(run >= MinOnBars) RelBuf[e] = thr;      // plant ▲ at the threshold level
         }
      }
   }
   return rates_total;
}
//+------------------------------------------------------------------+
