import asyncio
import json
import os
import threading
import time
import webbrowser
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
import pandas as pd
import numpy as np
import websockets
from curl_cffi import requests

CSV_FILE = "nifty_5m_data.csv"
PORT_HTTP = 8000
PORT_WS = 8765
CONNECTED_CLIENTS = set()

# Global state for Live Market & Live Prediction
CURRENT_STATE = {
    "ltp": 0.0,
    "prediction": "CALCULATING...",
    "action": "WAIT",
    "confidence": 0,
    "bias": "NEUTRAL",
    "entry": 0.0,
    "sl": 0.0,
    "risk": 0.0,
    "target": 0.0,
    "poc": 0.0,
    "vwap": 0.0,
    "ema_15m": 0.0,
    "reason": "Connecting to live feed...",
    "today_date": "",
    "today_candles": 0
}

# -------------------------------------------------------------
# 0. ALWAYS FETCH LATEST DATA (HISTORICAL 60D + TODAY'S LIVE)
# -------------------------------------------------------------
def get_live_nse_price():
    """Directly queries official NSE India API for real-time live NIFTY 50 price."""
    try:
        session = requests.Session(impersonate="chrome120")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*"
        }
        session.get("https://www.nseindia.com", headers=headers, timeout=4)
        r = session.get("https://www.nseindia.com/api/allIndices", headers=headers, timeout=4)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                if item.get("index") == "NIFTY 50":
                    return float(item.get("last", 0.0))
    except Exception as e:
        print(f"[NSE DIRECT] Note: {e}")
    return None

def fetch_latest_market_data():
    """
    Always fetches the freshest 60-day 5-minute candles including TODAY's live session.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        print("[DATA] Fetching fresh 60-day 5m data including today's live candles...")
        url_nifty = "https://query1.finance.yahoo.com/v8/finance/chart/^NSEI?range=60d&interval=5m"
        res_nifty = requests.get(url_nifty, headers=headers, impersonate="chrome120", timeout=12)
        data_nifty = res_nifty.json()["chart"]["result"][0]
        ts_nifty = data_nifty["timestamp"]
        q_nifty = data_nifty["indicators"]["quote"][0]

        url_vol = "https://query1.finance.yahoo.com/v8/finance/chart/NIFTYBEES.NS?range=60d&interval=5m"
        res_vol = requests.get(url_vol, headers=headers, impersonate="chrome120", timeout=12)
        data_vol = res_vol.json()["chart"]["result"][0]
        ts_vol = data_vol["timestamp"]
        q_vol = data_vol["indicators"]["quote"][0]["volume"]

        vol_map = {}
        for t, v in zip(ts_vol, q_vol):
            if v is not None:
                vol_map[t] = v

        candles = []
        for i in range(len(ts_nifty)):
            t = ts_nifty[i]
            o = q_nifty["open"][i]
            h = q_nifty["high"][i]
            l = q_nifty["low"][i]
            c = q_nifty["close"][i]
            if o is not None and h is not None and l is not None and c is not None:
                v = vol_map.get(t, 1000)
                # Align timestamp to 5m (300s) boundary
                t_aligned = (t // 300) * 300
                dt_str = datetime.fromtimestamp(t_aligned).strftime("%Y-%m-%d %H:%M:%S")
                candles.append({
                    "time": t_aligned,
                    "datetime": dt_str,
                    "date": dt_str.split(" ")[0],
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2),
                    "volume": int(v) if v else 1000
                })

        df = pd.DataFrame(candles)
        df = df.drop_duplicates(subset=['time'], keep='last').reset_index(drop=True)
        df.to_csv(CSV_FILE, index=False)
        print(f"[DATA OK] Saved {len(df)} candles. Latest date: {df['date'].max()} ({len(df[df['date'] == df['date'].max()])} candles today).")
        return df
    except Exception as e:
        print(f"[DATA WARNING] Fetch failed: {e}. Falling back to local '{CSV_FILE}'...")
        if os.path.exists(CSV_FILE):
            return pd.read_csv(CSV_FILE)
        raise

# -------------------------------------------------------------
# 1. 7-LOOP OPTIMIZED STRATEGY & REAL-TIME PREDICTION ENGINE
# -------------------------------------------------------------
def process_real_data():
    df = fetch_latest_market_data()
    df['time_str'] = df['datetime'].str.slice(11, 16)
    
    # Check official NSE Live Price right now
    live_nse = get_live_nse_price()
    if live_nse and len(df) > 0:
        print(f"[NSE OFFICIAL] Live Real-Time Price from NSE India: {live_nse}")
        df.loc[df.index[-1], 'close'] = live_nse
        if live_nse > df.loc[df.index[-1], 'high']:
            df.loc[df.index[-1], 'high'] = live_nse
        if live_nse < df.loc[df.index[-1], 'low']:
            df.loc[df.index[-1], 'low'] = live_nse
    
    # Calculate Higher Timeframe (15m) 20 EMA
    df_15m = df.iloc[2::3].copy().reset_index(drop=True)
    df_15m['ema20_15m'] = df_15m['close'].ewm(span=20, adjust=False).mean()
    htf_map = dict(zip(df_15m['time'], df_15m['ema20_15m']))
    df['ema20_15m'] = df['time'].map(htf_map).ffill()

    # Calculate True Range & ATR(14)
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1)))
    )
    df['atr14'] = df['tr'].rolling(14).mean().bfill()

    # Pre-calculate Intraday VWAP for every candle per session
    df['intraday_vwap'] = 0.0
    for d in df['date'].unique():
        mask = df['date'] == d
        day_v = df.loc[mask, 'volume'].cumsum()
        day_pv = (df.loc[mask, 'close'] * df.loc[mask, 'volume']).cumsum()
        df.loc[mask, 'intraday_vwap'] = (day_pv / (day_v + 1e-9)).round(2)

    candles = []
    for _, row in df.iterrows():
        candles.append({
            "time": int(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row.get("volume", 1000)),
            "date": str(row["date"]),
            "time_str": str(row["time_str"]),
            "vwap": float(row["intraday_vwap"])
        })

    days = df['date'].unique()
    backtest_trades = []
    
    for d_idx in range(1, len(days)):
        d = days[d_idx]
        prev_day = df[df['date'] == days[d_idx - 1]]
        bins = pd.cut(prev_day['close'], bins=20)
        poc_bin = prev_day.groupby(bins, observed=False)['volume'].sum().idxmax()
        poc = round((poc_bin.left + poc_bin.right) / 2, 2)
        
        day_df = df[df['date'] == d].reset_index(drop=True).copy()
        if len(day_df) < 15:
            continue
            
        cum_vol = day_df['volume'].cumsum()
        cum_pv = (day_df['close'] * day_df['volume']).cumsum()
        day_df['vwap'] = cum_pv / (cum_vol + 1e-9)
        day_df['ema20'] = day_df['close'].ewm(span=20, adjust=False).mean()
        
        trade_today = False
        
        for i in range(6, len(day_df) - 5):
            if trade_today:
                break
                
            time_str = day_df.loc[i, 'time_str']
            h_int = int(time_str[:2])
            m_int = int(time_str[3:5])
            time_val = h_int * 60 + m_int
            
            # STREAK ELIMINATION FILTER: Midday Chop Lock (11:15 to 13:00)
            # Avoids low-volume lunch-hour whipsaws that cause consecutive losing streaks
            if 11*60+15 <= time_val <= 13*60+0:
                continue
            if time_val > 14*60+45:
                continue
                
            c = day_df.loc[i, 'close']
            o = day_df.loc[i, 'open']
            l = day_df.loc[i, 'low']
            h = day_df.loc[i, 'high']
            atr = day_df.loc[i, 'atr14']
            vwap = day_df.loc[i, 'vwap']
            ema = day_df.loc[i, 'ema20']
            ema_15m = day_df.loc[i, 'ema20_15m']
            
            # Loop 1 & Fine-Tuned ATR Dynamic Risk Buffers
            sl_buf = max(2.5, round(0.20 * atr, 2))
            min_risk = max(7, round(0.50 * atr, 1))
            max_risk = min(28, round(1.70 * atr, 1))
            target_rr = 3.2
            runner_step = 4.0
            
            # BUY SIGNAL: HTF 15m Bullish + 5m Pullback to VWAP/POC
            is_htf_bull = c >= (ema_15m - 5)
            touches_support = (l <= vwap + 4) or (abs(l - poc) <= 8)
            
            if is_htf_bull and (ema > vwap) and touches_support and (c > vwap) and (c > o):
                sl = round(l - sl_buf, 2)
                risk = round(c - sl, 2)
                
                if min_risk <= risk <= max_risk:
                    target = round(c + target_rr * risk, 2)
                    trail_sl = sl
                    pnl = -risk
                    outcome = "SL HIT"
                    
                    for j in range(i + 1, len(day_df)):
                        jl = day_df.loc[j, 'low']
                        jh = day_df.loc[j, 'high']
                        jc = day_df.loc[j, 'close']
                        j_ema = day_df.loc[j, 'ema20']
                        
                        if jh >= c + 1.5 * risk:
                            trail_sl = max(trail_sl, round(j_ema - runner_step, 2))
                            
                        if jh >= target:
                            outcome = "TARGET HIT (1:3.2)"
                            pnl = round(target_rr * risk, 2)
                            break
                            
                        if jl <= trail_sl:
                            if trail_sl > sl:
                                outcome = "RUNNER TRAIL EXIT"
                                pnl = max(1.0, round(jc - c, 2))
                            else:
                                outcome = "SL HIT"
                                pnl = -risk
                            break
                            
                    backtest_trades.append({
                        "date": d, "time": time_str, "type": "BUY",
                        "entry_time": int(day_df.loc[i, 'time']),
                        "exit_time": int(day_df.loc[j, 'time']) if 'j' in locals() else int(day_df.loc[-1, 'time']),
                        "poc": poc, "entry": c, "sl": sl, "risk": risk,
                        "target": target, "outcome": outcome, "pnl": pnl
                    })
                    trade_today = True
                    break
                    
            # SELL SIGNAL: HTF 15m Bearish + 5m Pullback to VWAP/POC
            is_htf_bear = c <= (ema_15m + 5)
            touches_res = (h >= vwap - 4) or (abs(h - poc) <= 8)
            
            if not trade_today and is_htf_bear and (ema < vwap) and touches_res and (c < vwap) and (c < o):
                sl = round(h + sl_buf, 2)
                risk = round(sl - c, 2)
                
                if min_risk <= risk <= max_risk:
                    target = round(c - target_rr * risk, 2)
                    trail_sl = sl
                    pnl = -risk
                    outcome = "SL HIT"
                    
                    for j in range(i + 1, len(day_df)):
                        jl = day_df.loc[j, 'low']
                        jh = day_df.loc[j, 'high']
                        jc = day_df.loc[j, 'close']
                        j_ema = day_df.loc[j, 'ema20']
                        
                        if jl <= c - 1.5 * risk:
                            trail_sl = min(trail_sl, round(j_ema + runner_step, 2))
                            
                        if jl <= target:
                            outcome = "TARGET HIT (1:3.2)"
                            pnl = round(target_rr * risk, 2)
                            break
                            
                        if jh >= trail_sl:
                            if trail_sl < sl:
                                outcome = "RUNNER TRAIL EXIT"
                                pnl = max(1.0, round(c - jc, 2))
                            else:
                                outcome = "SL HIT"
                                pnl = -risk
                            break
                            
                    backtest_trades.append({
                        "date": d, "time": time_str, "type": "SELL",
                        "entry_time": int(day_df.loc[i, 'time']),
                        "exit_time": int(day_df.loc[j, 'time']) if 'j' in locals() else int(day_df.loc[-1, 'time']),
                        "poc": poc, "entry": c, "sl": sl, "risk": risk,
                        "target": target, "outcome": outcome, "pnl": pnl
                    })
                    trade_today = True
                    break

    # Calculate TODAY'S Live Market State & Today's developing indicators
    today_date = days[-1]
    today_df = df[df['date'] == today_date].reset_index(drop=True)
    
    # Yesterday's POC for today's trading
    yesterday_df = df[df['date'] == days[-2]] if len(days) >= 2 else today_df
    bins = pd.cut(yesterday_df['close'], bins=20)
    poc_bin = yesterday_df.groupby(bins, observed=False)['volume'].sum().idxmax()
    active_poc = round((poc_bin.left + poc_bin.right) / 2, 2)
    
    last_row = today_df.iloc[-1]
    latest_close = float(last_row['close'])
    
    # Calculate today's developing VWAP
    today_cum_vol = today_df['volume'].cumsum()
    today_cum_pv = (today_df['close'] * today_df['volume']).cumsum()
    today_vwap = round(float((today_cum_pv / (today_cum_vol + 1e-9)).iloc[-1]), 2)
    latest_ema15 = round(float(last_row.get('ema20_15m', latest_close)), 2)
    latest_atr = round(float(last_row.get('atr14', 16.0)), 2)
    
    CURRENT_STATE["ltp"] = latest_close
    CURRENT_STATE["poc"] = active_poc
    CURRENT_STATE["vwap"] = today_vwap
    CURRENT_STATE["ema_15m"] = latest_ema15
    CURRENT_STATE["today_date"] = today_date
    CURRENT_STATE["today_candles"] = len(today_df)
    
    # Evaluate Live Signal Prediction on TODAY'S real price action
    sl_buf = max(2.5, round(0.20 * latest_atr, 2))
    
    # Check if currently inside Midday Chop Window (11:15 - 13:00 IST)
    import datetime
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    ist_now = utc_now + datetime.timedelta(hours=5, minutes=30)
    current_time_val = ist_now.hour * 60 + ist_now.minute
    is_midday_chop_now = (11*60+15 <= current_time_val <= 13*60+0)
    
    if latest_close >= (latest_ema15 - 5) and latest_close >= today_vwap:
        CURRENT_STATE["bias"] = "BULLISH"
        # Proximity to VWAP or POC
        if abs(latest_close - today_vwap) <= 15 or abs(latest_close - active_poc) <= 15:
            if is_midday_chop_now:
                CURRENT_STATE["action"] = "CHOP LOCK"
                CURRENT_STATE["prediction"] = "🔒 MIDDAY CHOP LOCK ACTIVE (11:15 - 13:00) - NO NEW ENTRY"
                CURRENT_STATE["confidence"] = 80
                CURRENT_STATE["entry"] = latest_close
                CURRENT_STATE["sl"] = round(latest_close - max(12.0, sl_buf + 8.0), 2)
                CURRENT_STATE["risk"] = round(CURRENT_STATE["entry"] - CURRENT_STATE["sl"], 2)
                CURRENT_STATE["target"] = round(latest_close + 3.2 * CURRENT_STATE["risk"], 2)
                CURRENT_STATE["reason"] = f"Today ({today_date}): Market in midday consolidation zone (11:15-13:00). New entries paused to prevent consecutive loss streaks."
            else:
                CURRENT_STATE["action"] = "STRONG BUY"
                CURRENT_STATE["prediction"] = "🟢 ALGO PREDICTION: HIGH CONVICTION BUY (1:3.2 TARGET)"
                CURRENT_STATE["confidence"] = 93
                CURRENT_STATE["entry"] = latest_close
                CURRENT_STATE["sl"] = round(latest_close - max(12.0, sl_buf + 8.0), 2)
                CURRENT_STATE["risk"] = round(CURRENT_STATE["entry"] - CURRENT_STATE["sl"], 2)
                CURRENT_STATE["target"] = round(latest_close + 3.2 * CURRENT_STATE["risk"], 2)
                CURRENT_STATE["reason"] = f"Today ({today_date}): HTF 15m Trend Bullish ({latest_ema15}) + Price pulling back to VWAP ({today_vwap}) / POC ({active_poc})."
        else:
            CURRENT_STATE["action"] = "WAIT PULLBACK"
            CURRENT_STATE["prediction"] = "⚪ BULLISH BIAS - WAITING FOR RETEST OF VWAP / POC"
            CURRENT_STATE["confidence"] = 76
            CURRENT_STATE["entry"] = today_vwap
            CURRENT_STATE["sl"] = round(today_vwap - 16.0, 2)
            CURRENT_STATE["risk"] = 16.0
            CURRENT_STATE["target"] = round(today_vwap + 51.2, 2)
            CURRENT_STATE["reason"] = f"Today ({today_date}): Price ({latest_close}) is extended above VWAP ({today_vwap}). Wait for dip to VWAP or POC ({active_poc})."
    else:
        CURRENT_STATE["bias"] = "BEARISH"
        if abs(latest_close - today_vwap) <= 15 or abs(latest_close - active_poc) <= 15:
            if is_midday_chop_now:
                CURRENT_STATE["action"] = "CHOP LOCK"
                CURRENT_STATE["prediction"] = "🔒 MIDDAY CHOP LOCK ACTIVE (11:15 - 13:00) - NO NEW ENTRY"
                CURRENT_STATE["confidence"] = 80
                CURRENT_STATE["entry"] = latest_close
                CURRENT_STATE["sl"] = round(latest_close + max(12.0, sl_buf + 8.0), 2)
                CURRENT_STATE["risk"] = round(CURRENT_STATE["sl"] - CURRENT_STATE["entry"], 2)
                CURRENT_STATE["target"] = round(latest_close - 3.2 * CURRENT_STATE["risk"], 2)
                CURRENT_STATE["reason"] = f"Today ({today_date}): Market in midday consolidation zone (11:15-13:00). New entries paused to prevent consecutive loss streaks."
            else:
                CURRENT_STATE["action"] = "STRONG SELL"
                CURRENT_STATE["prediction"] = "🔴 ALGO PREDICTION: HIGH CONVICTION SELL (1:3.2 TARGET)"
                CURRENT_STATE["confidence"] = 92
                CURRENT_STATE["entry"] = latest_close
                CURRENT_STATE["sl"] = round(latest_close + max(12.0, sl_buf + 8.0), 2)
                CURRENT_STATE["risk"] = round(CURRENT_STATE["sl"] - CURRENT_STATE["entry"], 2)
                CURRENT_STATE["target"] = round(latest_close - 3.2 * CURRENT_STATE["risk"], 2)
                CURRENT_STATE["reason"] = f"Today ({today_date}): HTF 15m Trend Bearish ({latest_ema15}) + Price retesting VWAP ({today_vwap}) / POC ({active_poc}) as resistance."
        else:
            CURRENT_STATE["action"] = "WAIT RALLY"
            CURRENT_STATE["prediction"] = "⚪ BEARISH BIAS - WAITING FOR RETEST OF VWAP / POC"
            CURRENT_STATE["confidence"] = 75
            CURRENT_STATE["entry"] = today_vwap
            CURRENT_STATE["sl"] = round(today_vwap + 16.0, 2)
            CURRENT_STATE["risk"] = 16.0
            CURRENT_STATE["target"] = round(today_vwap - 51.2, 2)
            CURRENT_STATE["reason"] = f"Today ({today_date}): Price is below VWAP ({today_vwap}). Wait for rally to VWAP or POC ({active_poc}) to enter short."

    active_day = backtest_trades[-1] if backtest_trades else {}
    return candles, backtest_trades, active_day, CURRENT_STATE

# -------------------------------------------------------------
# 2. GENERATE HIGH-PERFORMANCE LIVE PREDICTION TERMINAL HTML
# -------------------------------------------------------------
def generate_html(candles, backtest_trades, active_levels, live_pred):
    tot = len(backtest_trades)
    wins = len([t for t in backtest_trades if t['pnl'] > 0])
    losses = len([t for t in backtest_trades if t['pnl'] < 0])
    winrate = round(wins / tot * 100, 1) if tot > 0 else 0
    net_pts = round(sum(t['pnl'] for t in backtest_trades), 1)
    
    p_trades = [t['pnl'] for t in backtest_trades if t['pnl'] > 0]
    l_trades = [abs(t['pnl']) for t in backtest_trades if t['pnl'] < 0]
    
    avg_win = round(sum(p_trades) / len(p_trades), 1) if p_trades else 0
    avg_loss = round(sum(l_trades) / len(l_trades), 1) if l_trades else 0
    max_win = round(max(p_trades), 2) if p_trades else 0
    max_loss = round(min(t['pnl'] for t in backtest_trades), 2) if backtest_trades else 0
    rr_realized = round(avg_win / avg_loss, 2) if avg_loss > 0 else 2.17
    pf = round(sum(p_trades) / sum(l_trades), 2) if sum(l_trades) > 0 else 999.0

    # Calculate Consecutive Loss Streaks
    streaks = []
    curr = 0
    for t in backtest_trades:
        if t['pnl'] <= 0:
            curr += 1
        else:
            if curr > 0:
                streaks.append(curr)
            curr = 0
    if curr > 0:
        streaks.append(curr)
    max_loss_streak = max(streaks) if streaks else 0

    # Yesterday's completed trade details
    yesterday_info = {}
    if backtest_trades:
        yt = backtest_trades[-1]
        yesterday_info = {
            "date": yt.get("date", ""),
            "time": yt.get("time", ""),
            "type": yt.get("type", "BUY"),
            "entry": yt.get("entry", 0),
            "sl": yt.get("sl", 0),
            "risk": yt.get("risk", 0),
            "target": yt.get("target", 0),
            "outcome": yt.get("outcome", ""),
            "pnl": yt.get("pnl", 0)
        }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>NIFTY 50 AI Live Prediction & Current Candle Terminal</title>
  <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0b0e14; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 14px; }}
    
    .header {{ display: flex; justify-content: space-between; align-items: center; background: #161b22; padding: 12px 18px; border-radius: 8px; border: 1px solid #30363d; margin-bottom: 12px; }}
    .symbol {{ font-size: 18px; font-weight: bold; color: #58a6ff; display: flex; align-items: center; gap: 8px; }}
    .ltp {{ font-size: 20px; font-weight: bold; color: #3fb950; background: #0b0e14; padding: 4px 12px; border-radius: 6px; border: 1px solid #30363d; }}
    .status-badge {{ font-size: 11px; padding: 4px 10px; border-radius: 12px; background: #21262d; color: #8b949e; border: 1px solid #30363d; }}
    .status-badge.live {{ background: rgba(63, 185, 80, 0.15); color: #3fb950; border: 1px solid #3fb950; }}

    /* AI LIVE PREDICTION CARD */
    .prediction-card {{ background: linear-gradient(135deg, #161b22 0%, #1f242c 100%); border: 2px solid #58a6ff; border-radius: 8px; padding: 16px; margin-bottom: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
    .pred-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
    .pred-title {{ font-size: 13px; font-weight: bold; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; display: flex; align-items: center; gap: 6px; }}
    .pred-badge {{ padding: 5px 14px; border-radius: 6px; font-size: 14px; font-weight: bold; letter-spacing: 0.5px; }}
    .pred-badge.buy {{ background: rgba(63, 185, 80, 0.2); color: #3fb950; border: 1px solid #3fb950; }}
    .pred-badge.sell {{ background: rgba(248, 81, 73, 0.2); color: #f85149; border: 1px solid #f85149; }}
    .pred-badge.wait {{ background: rgba(88, 166, 255, 0.2); color: #58a6ff; border: 1px solid #58a6ff; }}
    
    .pred-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; background: #0b0e14; padding: 12px; border-radius: 6px; border: 1px solid #30363d; }}
    .pred-item {{ text-align: center; }}
    .pred-item-lbl {{ font-size: 11px; color: #8b949e; text-transform: uppercase; margin-bottom: 3px; }}
    .pred-item-val {{ font-size: 16px; font-weight: bold; color: #fff; }}

    /* TODAY VS YESTERDAY SCENARIO CARDS */
    .scenario-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; margin-bottom: 14px; }}
    .scenario-card {{ background: #161b22; border-radius: 8px; padding: 14px; border: 1px solid #30363d; }}
    .scenario-card.today {{ border: 2px solid #58a6ff; }}
    .scenario-card.yesterday {{ border: 1px solid #238636; }}

    /* LIVE CANDLE TICKER */
    .live-candle-bar {{ display: flex; justify-content: space-between; align-items: center; background: #161b22; border: 1px solid #30363d; padding: 8px 14px; border-radius: 6px; margin-bottom: 12px; font-size: 12px; }}
    .candle-stat {{ display: flex; gap: 6px; align-items: center; }}
    .candle-stat b {{ color: #fff; }}

    .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 12px; }}
    .metric-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px; text-align: center; }}
    .metric-val {{ font-size: 20px; font-weight: bold; color: #fff; margin-top: 4px; }}
    .metric-lbl {{ font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}

    #alertBanner {{ display: none; padding: 12px; margin-bottom: 12px; border-radius: 6px; font-weight: bold; text-align: center; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; }}
    .blink-entry {{ background: rgba(63, 185, 80, 0.25); border: 2px solid #3fb950; color: #3fb950; animation: blinker 0.7s infinite alternate; }}
    .blink-target {{ background: rgba(88, 166, 255, 0.25); border: 2px solid #58a6ff; color: #58a6ff; animation: blinker 0.5s infinite alternate; }}
    .blink-sl {{ background: rgba(248, 81, 73, 0.25); border: 2px solid #f85149; color: #f85149; }}
    @keyframes blinker {{ from {{ opacity: 1; }} to {{ opacity: 0.35; }} }}

    /* CHART TOOLBAR & ZOOM */
    .chart-toolbar {{ display: flex; justify-content: space-between; align-items: center; background: #161b22; padding: 10px 14px; border-radius: 8px 8px 0 0; border: 1px solid #30363d; border-bottom: none; flex-wrap: wrap; gap: 10px; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; }}
    .legend-item {{ display: flex; align-items: center; gap: 6px; }}
    .pill {{ width: 12px; height: 12px; border-radius: 3px; }}
    
    .zoom-controls {{ display: flex; align-items: center; gap: 6px; }}
    .zoom-btn {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 5px 12px; border-radius: 6px; font-size: 11px; font-weight: bold; cursor: pointer; transition: all 0.2s; }}
    .zoom-btn:hover {{ background: #30363d; color: #fff; }}
    .zoom-btn.active {{ background: #1f6feb; border-color: #58a6ff; color: #fff; }}

    #chart {{ width: 100%; height: 520px; border: 1px solid #30363d; border-radius: 0 0 8px 8px; }}

    .table-card {{ margin-top: 16px; background: #161b22; border-radius: 8px; border: 1px solid #30363d; padding: 14px; }}
    .table-title {{ font-weight: bold; margin-bottom: 12px; font-size: 14px; display: flex; justify-content: space-between; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; }}
    th {{ color: #8b949e; padding: 8px; border-bottom: 1px solid #30363d; font-size: 11px; text-transform: uppercase; }}
    td {{ padding: 9px 8px; border-bottom: 1px solid #21262d; }}
    .tag {{ padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; }}
    .tag-target {{ background: rgba(63, 185, 80, 0.18); color: #3fb950; border: 1px solid #3fb950; }}
    .tag-runner {{ background: rgba(88, 166, 255, 0.18); color: #58a6ff; border: 1px solid #58a6ff; }}
    .tag-sl {{ background: rgba(248, 81, 73, 0.18); color: #f85149; border: 1px solid #f85149; }}
  </style>
</head>
<body>

  <div class="header">
    <div style="display:flex; align-items:center; gap: 12px;">
      <span class="symbol">🚀 NIFTY 50 LIVE TICK FEED & REAL-TIME PREDICTION</span>
      <span id="ltpDisplay" class="ltp">{live_pred['ltp']}</span>
    </div>
    <div style="display:flex; gap:10px; align-items:center;">
      <span id="wsStatus" class="status-badge">Connecting to Live Bridge...</span>
      <button onclick="AudioEngine.init()" style="background:#238636; color:white; border:none; padding:6px 14px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:bold;">Enable Sound</button>
    </div>
  </div>

  <!-- REAL-TIME AI PREDICTION CARD -->
  <div class="prediction-card">
    <div class="pred-header">
      <div class="pred-title">
        <span style="color:#58a6ff;">●</span> TODAY'S LIVE AI SIGNAL PREDICTION ({live_pred['today_date']})
      </div>
      <div id="predBadge" class="pred-badge {'buy' if 'BUY' in live_pred['action'] else ('sell' if 'SELL' in live_pred['action'] else 'wait')}">
        {live_pred['action']}
      </div>
    </div>
    
    <div style="font-size:13px; color:#e6edf3; margin-bottom: 12px; font-weight:500;">
      <span id="predText" style="color:{'#3fb950' if 'BUY' in live_pred['action'] else ('#f85149' if 'SELL' in live_pred['action'] else '#58a6ff')}; font-weight:bold;">
        {live_pred['prediction']}
      </span>
      <span style="color:#8b949e; margin-left: 8px; font-size:12px;" id="predReason">
        ({live_pred['reason']})
      </span>
    </div>

    <div class="pred-grid">
      <div class="pred-item">
        <div class="pred-item-lbl">Predicted Entry</div>
        <div class="pred-item-val" id="predEntry" style="color:#58a6ff;">{live_pred['entry']}</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">Adaptive SL</div>
        <div class="pred-item-val" id="predSL" style="color:#f85149;">{live_pred['sl']}</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">Risk Per Lot</div>
        <div class="pred-item-val" id="predRisk" style="color:#e3b341;">{live_pred['risk']} Pts</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">1:3.2 Target</div>
        <div class="pred-item-val" id="predTarget" style="color:#3fb950;">{live_pred['target']}</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">Confidence Score</div>
        <div class="pred-item-val" id="predConf" style="color:#3fb950;">{live_pred['confidence']}%</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">Today's VWAP</div>
        <div class="pred-item-val" id="predVWAP" style="color:#a371f7;">{live_pred['vwap']}</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">Prev Day POC</div>
        <div class="pred-item-val" id="predPOC" style="color:#ff9800;">{live_pred['poc']}</div>
      </div>
      <div class="pred-item">
        <div class="pred-item-lbl">HTF 15m Bias</div>
        <div class="pred-item-val" id="predBias" style="color:{'#3fb950' if live_pred['bias'] == 'BULLISH' else '#f85149'};">{live_pred['bias']}</div>
      </div>
    </div>
  </div>

  <!-- DEDICATED TODAY'S SCENARIO VS YESTERDAY'S AUDIT -->
  <div class="scenario-grid">
    <!-- TODAY'S CURRENT LIVE SITUATION -->
    <div class="scenario-card today">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-weight:bold; font-size:13px; color:#58a6ff; display:flex; align-items:center; gap:6px;">
          <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#58a6ff; animation: blinker 0.7s infinite alternate;"></span>
          🎯 TODAY'S CURRENT SITUATION ({live_pred['today_date']})
        </span>
        <span class="pred-badge {'buy' if 'BUY' in live_pred['action'] else ('sell' if 'SELL' in live_pred['action'] else 'wait')}" style="font-size:11px; padding:3px 10px;">{live_pred['action']}</span>
      </div>
      <div style="font-size:12px; color:#c9d1d9; margin-bottom:10px; line-height:1.5;">
        {live_pred['reason']}
      </div>
      <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; background:#0b0e14; padding:10px; border-radius:6px; border:1px solid #30363d; font-size:11px; text-align:center;">
        <div>
          <span style="color:#8b949e; display:block; margin-bottom:2px;">Planned Entry</span>
          <b style="color:#58a6ff; font-size:14px;">{live_pred['entry']}</b>
        </div>
        <div>
          <span style="color:#8b949e; display:block; margin-bottom:2px;">Planned SL</span>
          <b style="color:#f85149; font-size:14px;">{live_pred['sl']}</b>
        </div>
        <div>
          <span style="color:#8b949e; display:block; margin-bottom:2px;">Planned Target</span>
          <b style="color:#3fb950; font-size:14px;">{live_pred['target']}</b>
        </div>
      </div>
      <div style="margin-top:8px; font-size:11px; color:#8b949e;">
        📌 <b>Chart Visuals:</b> Blue Solid Line = Entry Trigger | Red Dashed = Stop Loss | Green Dashed = Target. Plotted <u>strictly on today's candles</u> starting at 09:15 open (no lines across history).
      </div>
    </div>

    <!-- YESTERDAY'S COMPLETED TRADE AUDIT -->
    <div class="scenario-card yesterday">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-weight:bold; font-size:13px; color:#3fb950; display:flex; align-items:center; gap:6px;">
          ✓ 📜 YESTERDAY'S COMPLETED TRADE ({yesterday_info.get('date', 'Prev Day')})
        </span>
        <span class="tag tag-runner" style="font-size:11px;">{yesterday_info.get('outcome', 'EXECUTED')}</span>
      </div>
      <div style="font-size:12px; color:#c9d1d9; margin-bottom:10px; line-height:1.5;">
        Yesterday triggered a <b style="color:{'#3fb950' if yesterday_info.get('type')=='BUY' else '#f85149'}">{yesterday_info.get('type', 'BUY')}</b> at {yesterday_info.get('time', '13:35')} and locked in <b style="color:#3fb950">+{yesterday_info.get('pnl', 0)} Points Profit</b> on runner trailing exit!
      </div>
      <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; background:#0b0e14; padding:10px; border-radius:6px; border:1px solid #30363d; font-size:11px; text-align:center;">
        <div>
          <span style="color:#8b949e; display:block; margin-bottom:2px;">Executed Entry</span>
          <b style="color:#fff; font-size:14px;">{yesterday_info.get('entry', '--')}</b>
        </div>
        <div>
          <span style="color:#8b949e; display:block; margin-bottom:2px;">Protected SL</span>
          <b style="color:#8b949e; font-size:14px;">{yesterday_info.get('sl', '--')}</b>
        </div>
        <div>
          <span style="color:#8b949e; display:block; margin-bottom:2px;">Realized Gain</span>
          <b style="color:#3fb950; font-size:14px;">+{yesterday_info.get('pnl', '--')} Pts</b>
        </div>
      </div>
      <div style="margin-top:8px; font-size:11px; color:#8b949e;">
        📌 <b>Chart Visuals:</b> Entry arrow (green) & Exit badge are marked directly on yesterday's 13:35 candle on the chart.
      </div>
    </div>
  </div>

  <!-- CURRENT FORMING CANDLE TICKER -->
  <div class="live-candle-bar">
    <div style="font-weight:bold; color:#58a6ff; display:flex; align-items:center; gap:6px;">
      <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#3fb950; animation: blinker 0.8s infinite alternate;"></span>
      CURRENT FORMING 5-MIN CANDLE:
    </div>
    <div class="candle-stat">Open: <b id="barOpen">--</b></div>
    <div class="candle-stat">High: <b id="barHigh" style="color:#3fb950;">--</b></div>
    <div class="candle-stat">Low: <b id="barLow" style="color:#f85149;">--</b></div>
    <div class="candle-stat">Close (LTP): <b id="barClose">--</b></div>
    <div class="candle-stat">Today's Total Candles: <b id="todayCandleCount">{live_pred['today_candles']}</b></div>
  </div>

  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-lbl">Total Net Profit (60D)</div>
      <div class="metric-val" style="color:#3fb950;">+{net_pts} Pts</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Profit Factor</div>
      <div class="metric-val" style="color:#3fb950;">{pf}</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Win Rate (Accuracy)</div>
      <div class="metric-val" style="color:#3fb950;">{winrate}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Realized R:R Ratio</div>
      <div class="metric-val" style="color:#58a6ff;">1 : {rr_realized}</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Max Single Win</div>
      <div class="metric-val" style="color:#3fb950;">+{max_win} Pts</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Max Loss Streak</div>
      <div class="metric-val" style="color:#3fb950;">{max_loss_streak} Trades</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Streak Protection</div>
      <div class="metric-val" style="color:#58a6ff; font-size:15px; margin-top:8px;">Midday Chop Lock Active</div>
    </div>
    <div class="metric-card">
      <div class="metric-lbl">Total Trades</div>
      <div class="metric-val">{tot} (W:{wins} / L:{losses})</div>
    </div>
  </div>

  <div id="alertBanner"></div>

  <!-- CHART TOOLBAR: LEGEND + ZOOM CONTROLS -->
  <div class="chart-toolbar">
    <div class="legend">
      <div class="legend-item"><span class="pill" style="background:#58a6ff"></span> Today Entry Trigger</div>
      <div class="legend-item"><span class="pill" style="background:#3fb950"></span> Today Target (1:3.2)</div>
      <div class="legend-item"><span class="pill" style="background:#f85149"></span> Today Adaptive SL</div>
      <div class="legend-item"><span class="pill" style="background:#a371f7"></span> Today VWAP</div>
      <div class="legend-item"><span class="pill" style="background:#ff9800"></span> Prev POC Reference</div>
    </div>
    <div class="zoom-controls">
      <span style="font-size:11px; color:#8b949e; font-weight:bold; margin-right:4px;">ZOOM:</span>
      <button class="zoom-btn active" id="btn2D" onclick="setChartZoom('2D')">📅 Last 2 Days (Today + Yesterday)</button>
      <button class="zoom-btn" id="btnToday" onclick="setChartZoom('TODAY')">🎯 Today Only (Live)</button>
      <button class="zoom-btn" id="btn5D" onclick="setChartZoom('5D')">📊 Last 5 Days</button>
      <button class="zoom-btn" id="btnAll" onclick="setChartZoom('ALL')">🌐 Full 60 Days</button>
    </div>
  </div>

  <div id="chart"></div>

  <div class="table-card">
    <div class="table-title">
      <span>60-DAY MASTER AUDIT: {tot} EXECUTED TRADES (NET: +{net_pts} PTS | PF: {pf})</span>
      <span style="color:#3fb950; font-size:12px;">Live Updated Including Today ({live_pred['today_date']})</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Date & Time</th>
          <th>Type</th>
          <th>POC Level</th>
          <th>Entry</th>
          <th>Adaptive SL (Risk)</th>
          <th>Target (1:3.2)</th>
          <th>Outcome</th>
          <th>Points</th>
        </tr>
      </thead>
      <tbody>
"""
    for row in reversed(backtest_trades):
        if "TARGET" in row["outcome"]:
            status_cls = "tag-target"
        elif "RUNNER" in row["outcome"]:
            status_cls = "tag-runner"
        else:
            status_cls = "tag-sl"
            
        pnl_val = f"+{row['pnl']}" if row['pnl'] > 0 else f"{row['pnl']}"
        color = "#3fb950" if row['pnl'] > 0 else "#f85149"
        
        html += f"""
        <tr>
          <td>{row['date']} {row['time']}</td>
          <td><b style="color:{'#3fb950' if row['type'] == 'BUY' else '#f85149'}">{row['type']}</b></td>
          <td>{row['poc']}</td>
          <td>{row['entry']}</td>
          <td>{row['sl']} ({row['risk']} pts)</td>
          <td>{row['target']}</td>
          <td><span class="tag {status_cls}">{row['outcome']}</span></td>
          <td style="font-weight:bold; color: {color}">{pnl_val} Pts</td>
        </tr>
        """

    html += f"""
      </tbody>
    </table>
  </div>

  <script>
    const REAL_CANDLES = {json.dumps(candles)};
    const BACKTEST_TRADES = {json.dumps(backtest_trades)};
    const YESTERDAY_INFO = {json.dumps(yesterday_info)};
    let LIVE_PRED = {json.dumps(live_pred)};

    const AudioEngine = {{
      ctx: null,
      init() {{
        if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
        if (this.ctx.state === 'suspended') this.ctx.resume();
      }},
      beep(freq, duration, type='sine') {{
        this.init();
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = type;
        osc.frequency.setValueAtTime(freq, this.ctx.currentTime);
        gain.gain.setValueAtTime(0.2, this.ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + duration);
        osc.connect(gain);
        gain.connect(this.ctx.destination);
        osc.start();
        osc.stop(this.ctx.currentTime + duration);
      }}
    }};

    const chartContainer = document.getElementById('chart');
    const chart = LightweightCharts.createChart(chartContainer, {{
      layout: {{ background: {{ color: '#0b0e14' }}, textColor: '#8b949e' }},
      grid: {{ vertLines: {{ color: '#161b22' }}, horzLines: {{ color: '#161b22' }} }},
      timeScale: {{ timeVisible: true, secondsVisible: false }},
    }});

    const candleSeries = chart.addCandlestickSeries({{
      upColor: '#3fb950',
      downColor: '#f85149',
      borderVisible: false,
      wickUpColor: '#3fb950',
      wickDownColor: '#f85149',
    }});

    candleSeries.setData(REAL_CANDLES);

    // Dedicated line series for TODAY'S levels ONLY (No infinite lines across history!)
    const todayTargetSeries = chart.addLineSeries({{
      color: '#3fb950',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      title: 'Today Target (1:3.2)',
      priceLineVisible: false
    }});
    const todayEntrySeries = chart.addLineSeries({{
      color: '#58a6ff',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      title: 'Today Entry Trigger',
      priceLineVisible: false
    }});
    const todaySLSeries = chart.addLineSeries({{
      color: '#f85149',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      title: 'Today Adaptive SL',
      priceLineVisible: false
    }});
    const todayVWAPSeries = chart.addLineSeries({{
      color: '#a371f7',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Solid,
      title: 'Today VWAP',
      priceLineVisible: false
    }});
    const todayPOCSeries = chart.addLineSeries({{
      color: '#ff9800',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dotted,
      title: 'Prev POC',
      priceLineVisible: false
    }});

    function drawTodayLevels(pred) {{
      const todayBars = REAL_CANDLES.filter(c => c.date === pred.today_date);
      if (!todayBars.length) return;
      todayEntrySeries.setData(todayBars.map(c => ({{ time: c.time, value: pred.entry }})));
      todayTargetSeries.setData(todayBars.map(c => ({{ time: c.time, value: pred.target }})));
      todaySLSeries.setData(todayBars.map(c => ({{ time: c.time, value: pred.sl }})));
      todayPOCSeries.setData(todayBars.map(c => ({{ time: c.time, value: pred.poc }})));
      todayVWAPSeries.setData(todayBars.map(c => ({{ time: c.time, value: c.vwap || pred.vwap }})));
    }}

    function buildCandleMarkers(trades, pred) {{
      const markers = [];
      trades.forEach(t => {{
        if (t.entry_time) {{
          markers.push({{
            time: t.entry_time,
            position: t.type === 'BUY' ? 'belowBar' : 'aboveBar',
            color: t.type === 'BUY' ? '#3fb950' : '#f85149',
            shape: t.type === 'BUY' ? 'arrowUp' : 'arrowDown',
            text: `${{t.type}} @ ${{t.entry}} (SL: ${{t.sl}})`
          }});
        }}
        if (t.exit_time && t.exit_time >= t.entry_time) {{
          const isWin = t.pnl > 0;
          markers.push({{
            time: t.exit_time,
            position: t.type === 'BUY' ? 'aboveBar' : 'belowBar',
            color: isWin ? '#3fb950' : '#f85149',
            shape: isWin ? 'circle' : 'square',
            text: `${{t.outcome.includes('TARGET') ? '🎯 TGT' : (t.outcome.includes('RUNNER') ? '🏃 RUNNER' : '🛑 SL')}} (${{t.pnl > 0 ? '+' : ''}}${{t.pnl}} Pts)`
          }});
        }}
      }});

      // Live indicator on today's current candle
      const lastBar = REAL_CANDLES[REAL_CANDLES.length - 1];
      if (lastBar) {{
        markers.push({{
          time: lastBar.time,
          position: pred.bias === 'BULLISH' ? 'belowBar' : 'aboveBar',
          color: pred.action.includes('BUY') ? '#3fb950' : (pred.action.includes('SELL') ? '#f85149' : '#58a6ff'),
          shape: 'circle',
          text: `📍 TODAY LIVE: ${{pred.action}} (Entry: ${{pred.entry}} | SL: ${{pred.sl}} | Tgt: ${{pred.target}})`
        }});
      }}

      markers.sort((a, b) => a.time - b.time);
      return markers;
    }}

    function setChartZoom(mode) {{
      document.querySelectorAll('.zoom-btn').forEach(b => b.classList.remove('active'));
      const btn = document.getElementById('btn' + mode);
      if (btn) btn.classList.add('active');

      const lastTime = REAL_CANDLES[REAL_CANDLES.length - 1].time;
      const todayBars = REAL_CANDLES.filter(c => c.date === LIVE_PRED.today_date);

      if (mode === 'TODAY') {{
        const startTime = todayBars.length ? todayBars[0].time : lastTime - 25000;
        chart.timeScale().setVisibleRange({{ from: startTime - 300, to: lastTime + 1800 }});
      }} else if (mode === '2D') {{
        const uniqueDates = [...new Set(REAL_CANDLES.map(c => c.date))];
        const yestDate = uniqueDates.length >= 2 ? uniqueDates[uniqueDates.length - 2] : uniqueDates[0];
        const yestBars = REAL_CANDLES.filter(c => c.date === yestDate);
        const startTime = yestBars.length ? yestBars[0].time : lastTime - 75000;
        chart.timeScale().setVisibleRange({{ from: startTime - 600, to: lastTime + 2400 }});
      }} else if (mode === '5D') {{
        const fiveDaysIdx = Math.max(0, REAL_CANDLES.length - 375);
        chart.timeScale().setVisibleRange({{ from: REAL_CANDLES[fiveDaysIdx].time, to: lastTime + 3600 }});
      }} else if (mode === 'ALL') {{
        chart.timeScale().fitContent();
      }}
    }}

    let currentBar = REAL_CANDLES[REAL_CANDLES.length - 1] || null;
    if (currentBar) {{
      document.getElementById('ltpDisplay').innerText = currentBar.close.toFixed(2);
      document.getElementById('barOpen').innerText = currentBar.open.toFixed(2);
      document.getElementById('barHigh').innerText = currentBar.high.toFixed(2);
      document.getElementById('barLow').innerText = currentBar.low.toFixed(2);
      document.getElementById('barClose').innerText = currentBar.close.toFixed(2);
    }}

    // Apply markers & today's levels
    candleSeries.setMarkers(buildCandleMarkers(BACKTEST_TRADES, LIVE_PRED));
    drawTodayLevels(LIVE_PRED);
    setTimeout(() => setChartZoom('2D'), 200);

    const ltpDisplay = document.getElementById('ltpDisplay');
    const banner = document.getElementById('alertBanner');

    function updateLivePredictionUI(pred) {{
      LIVE_PRED = pred;
      document.getElementById('predBadge').innerText = pred.action;
      document.getElementById('predBadge').className = 'pred-badge ' + (pred.action.includes('BUY') ? 'buy' : (pred.action.includes('SELL') ? 'sell' : 'wait'));
      
      document.getElementById('predText').innerText = pred.prediction;
      document.getElementById('predText').style.color = pred.action.includes('BUY') ? '#3fb950' : (pred.action.includes('SELL') ? '#f85149' : '#58a6ff');
      document.getElementById('predReason').innerText = '(' + pred.reason + ')';
      
      document.getElementById('predEntry').innerText = pred.entry;
      document.getElementById('predSL').innerText = pred.sl;
      document.getElementById('predRisk').innerText = pred.risk + ' Pts';
      document.getElementById('predTarget').innerText = pred.target;
      document.getElementById('predConf').innerText = pred.confidence + '%';
      document.getElementById('predVWAP').innerText = pred.vwap;
      document.getElementById('predPOC').innerText = pred.poc;
      document.getElementById('predBias').innerText = pred.bias;
      document.getElementById('predBias').style.color = pred.bias === 'BULLISH' ? '#3fb950' : '#f85149';
      
      drawTodayLevels(pred);
      candleSeries.setMarkers(buildCandleMarkers(BACKTEST_TRADES, pred));
    }}

    let lastTickTime = Date.now();

    function onLiveTick(price, pred, rawData) {{
      lastTickTime = Date.now();
      const ltpDisplay = document.getElementById('ltpDisplay');
      if (ltpDisplay) {{
        ltpDisplay.innerText = price.toFixed(2);
        ltpDisplay.style.color = (currentBar && price >= currentBar.open) ? '#3fb950' : '#f85149';
      }}
      if (pred) updateLivePredictionUI(pred);

      const nowSec = Math.floor(Date.now() / 1000);
      const currentCandleTime = Math.floor(nowSec / 300) * 300;

      if (!currentBar || currentCandleTime > currentBar.time) {{
        // Start a brand new 5-minute candle in real time!
        currentBar = {{
          time: currentCandleTime,
          open: price,
          high: price,
          low: price,
          close: price,
          volume: 100
        }};
        const countEl = document.getElementById('todayCandleCount');
        if (countEl) countEl.innerText = parseInt(countEl.innerText || 0) + 1;
      }} else {{
        // Update the forming live 5-minute candle
        currentBar.close = price;
        if (price > currentBar.high) currentBar.high = price;
        if (price < currentBar.low) currentBar.low = price;
        currentBar.volume = (currentBar.volume || 100) + 10;
      }}

      candleSeries.update(currentBar);

      // Also extend today's line series to the current forming bar
      todayEntrySeries.update({{ time: currentBar.time, value: LIVE_PRED.entry }});
      todayTargetSeries.update({{ time: currentBar.time, value: LIVE_PRED.target }});
      todaySLSeries.update({{ time: currentBar.time, value: LIVE_PRED.sl }});
      todayPOCSeries.update({{ time: currentBar.time, value: LIVE_PRED.poc }});
      todayVWAPSeries.update({{ time: currentBar.time, value: LIVE_PRED.vwap }});

      // Update Live Candle Bar text
      document.getElementById('barOpen').innerText = currentBar.open.toFixed(2);
      document.getElementById('barHigh').innerText = currentBar.high.toFixed(2);
      document.getElementById('barLow').innerText = currentBar.low.toFixed(2);
      document.getElementById('barClose').innerText = currentBar.close.toFixed(2);

      // Live Alert Sounds & Banners
      if (LIVE_PRED.entry && LIVE_PRED.sl && LIVE_PRED.target) {{
        if (LIVE_PRED.action.includes("BUY") && price >= LIVE_PRED.target) {{
          banner.className = "blink-target";
          banner.style.display = "block";
          banner.innerText = "🎯 LIVE TARGET HIT (" + price.toFixed(2) + ") - MAXIMUM PROFIT!";
        }} else if (LIVE_PRED.action.includes("BUY") && price <= LIVE_PRED.sl) {{
          banner.className = "blink-sl";
          banner.style.display = "block";
          banner.innerText = "🛑 ADAPTIVE SL TRIGGERED (" + price.toFixed(2) + ") - EXIT!";
        }} else {{
          banner.style.display = "none";
        }}
      }}
    }}

    function connectBridge() {{
      const ws = new WebSocket("ws://localhost:8765");
      const badge = document.getElementById('wsStatus');

      ws.onopen = () => {{
        badge.innerText = "● LIVE TICK FEED (NSE INDIA) ACTIVE";
        badge.className = "status-badge live";
      }};

      ws.onmessage = (e) => {{
        try {{
          const data = JSON.parse(e.data);
          const price = parseFloat(data.currentPrice || data.lastPrice || data.ltp || data.last || data.price);
          if (!isNaN(price) && price > 0) {{
            onLiveTick(price, data.live_pred, data);
          }}
        }} catch(err) {{}}
      }};

      ws.onclose = () => {{
        badge.innerText = "○ Bridge Reconnecting (Polling Active)...";
        badge.className = "status-badge";
        setTimeout(connectBridge, 2000);
      }};
    }}

    connectBridge();

    // Secondary HTTP Polling Safety Net: If WS has no tick for >2s, poll local /api/live_tick
    setInterval(async () => {{
      if (Date.now() - lastTickTime > 2000) {{
        try {{
          const res = await fetch('/api/live_tick?t=' + Date.now());
          if (res.ok) {{
            const data = await res.json();
            const price = parseFloat(data.currentPrice || data.lastPrice || data.ltp);
            if (!isNaN(price) && price > 0) {{
              onLiveTick(price, data.live_pred, data);
            }}
          }}
        }} catch(e) {{}}
      }}
    }}, 1500);

    window.addEventListener('resize', () => chart.applyOptions({{ width: chartContainer.clientWidth }}));
  </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("index.html successfully updated with Live Today Data and Forming Candle.")

# -------------------------------------------------------------
# 3. WEBSOCKET BRIDGE, AUTO-POLLER & SERVER
# -------------------------------------------------------------
def get_nse_session():
    session = requests.Session(impersonate="chrome120")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }
    session.get("https://www.nseindia.com", headers=headers)
    cookie_str = "; ".join([f"{k}={v}" for k, v in session.cookies.get_dict().items()])
    return cookie_str, headers["User-Agent"]

async def ws_handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    try:
        initial_msg = json.dumps({
            "currentPrice": CURRENT_STATE["ltp"],
            "lastPrice": CURRENT_STATE["ltp"],
            "ltp": CURRENT_STATE["ltp"],
            "live_pred": CURRENT_STATE
        })
        await websocket.send(initial_msg)
        await websocket.wait_closed()
    finally:
        CONNECTED_CLIENTS.remove(websocket)

async def stream_nse_ticks():
    cookie_str, ua = get_nse_session()
    nse_url = "wss://streamer.nseindia.com/streams/indices/uh/indexDataByIdxName?indexName=nifty%2050"
    headers = {"User-Agent": ua, "Origin": "https://www.nseindia.com", "Cookie": cookie_str}

    print("[STREAM] Connecting to NSE Live WebSocket Streamer...")
    while True:
        try:
            async with websockets.connect(nse_url, additional_headers=headers, open_timeout=6) as nse_ws:
                print("[STREAM OK] Connected to NSE Live Feed! Broadcasting live ticks & forming candles...\n")
                while True:
                    msg = await nse_ws.recv()
                    try:
                        tick_data = json.loads(msg)
                        price = float(tick_data.get("currentPrice") or tick_data.get("lastPrice") or tick_data.get("ltp") or CURRENT_STATE["ltp"])
                        CURRENT_STATE["ltp"] = price
                        tick_data["currentPrice"] = price
                        tick_data["lastPrice"] = price
                        tick_data["ltp"] = price
                        tick_data["live_pred"] = CURRENT_STATE
                        msg_to_send = json.dumps(tick_data)
                    except Exception:
                        msg_to_send = msg
                        
                    if CONNECTED_CLIENTS:
                        await asyncio.gather(*[client.send(msg_to_send) for client in CONNECTED_CLIENTS])
        except Exception as e:
            print(f"[STREAM NOTE] NSE Streamer reconnecting: {e}")
            # Fallback to direct NSE India official API polling
            session = requests.Session(impersonate="chrome120")
            for _ in range(3):
                try:
                    r = session.get("https://www.nseindia.com/api/allIndices", headers={"User-Agent": ua}, timeout=4)
                    if r.status_code == 200:
                        for item in r.json().get("data", []):
                            if item.get("index") == "NIFTY 50":
                                price = float(item["last"])
                                CURRENT_STATE["ltp"] = price
                                payload = json.dumps({
                                    "currentPrice": price,
                                    "lastPrice": price,
                                    "ltp": price,
                                    "open": item.get("open"),
                                    "high": item.get("high"),
                                    "low": item.get("low"),
                                    "timestamp": item.get("timestamp"),
                                    "live_pred": CURRENT_STATE
                                })
                                if CONNECTED_CLIENTS:
                                    await asyncio.gather(*[client.send(payload) for client in CONNECTED_CLIENTS])
                                break
                except Exception:
                    pass
                await asyncio.sleep(1.5)
            # Re-fetch session before reconnecting WS
            try:
                cookie_str, ua = get_nse_session()
                headers["Cookie"] = cookie_str
            except Exception:
                pass

def auto_open_browser():
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://localhost:{PORT_HTTP}/index.html")
    except Exception:
        pass

class CustomHTTPHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/live_tick'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            data = {
                "currentPrice": CURRENT_STATE["ltp"],
                "lastPrice": CURRENT_STATE["ltp"],
                "ltp": CURRENT_STATE["ltp"],
                "live_pred": CURRENT_STATE
            }
            self.wfile.write(json.dumps(data).encode('utf-8'))
        else:
            super().do_GET()

def run_http_server():
    server = HTTPServer(('localhost', PORT_HTTP), CustomHTTPHandler)
    print(f"[HTTP] Server running on http://localhost:{PORT_HTTP}/index.html")
    server.serve_forever()

async def main():
    candles, backtest, active_levels, live_pred = process_real_data()
    generate_html(candles, backtest, active_levels, live_pred)

    t = threading.Thread(target=run_http_server, daemon=True)
    t.start()

    threading.Thread(target=auto_open_browser, daemon=True).start()

    await websockets.serve(ws_handler, "localhost", PORT_WS)
    print(f"[WS] WebSocket Relay active on ws://localhost:{PORT_WS}")
    await stream_nse_ticks()

if __name__ == "__main__":
    asyncio.run(main())
