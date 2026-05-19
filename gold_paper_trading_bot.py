"""
╔══════════════════════════════════════════════════════════════╗
║       GOLD TREND SCALPER — Paper Trading Bot Live v2        ║
║       Dati: Twelve Data API                                 ║
║       Notifiche Telegram + Report Giornaliero               ║
╚══════════════════════════════════════════════════════════════╝
"""

import requests
import pandas as pd
import numpy as np
import schedule
import time
import json
import os
from datetime import datetime, timezone

# ─────────────────────────────────────────
#  CONFIGURAZIONE
# ─────────────────────────────────────────
TELEGRAM_TOKEN    = "8638374066:AAH0M7q1tB3zcRFrUpofKygpbc7uRqQpikw"
TELEGRAM_CHAT_ID  = "716275770"
TWELVEDATA_KEY    = "d8f17c18fe0f4963b52ade99a1d657ca"

CONFIG = {
    "symbol":             "XAU/USD",
    "ema_fast":           50,
    "ema_slow":           200,
    "rsi_period":         14,
    "rsi_oversold":       35,
    "rsi_overbought":     65,
    "take_profit_usd":    2.00,
    "stop_loss_usd":      1.30,
    "capital_start":      1000.0,
    "risk_pct":           0.01,
    "oz_per_lot":         100,
    "active_hours_start": 7,
    "active_hours_end":   21,
    "data_file":          "gold_bot_state.json",
    "log_file":           "gold_bot_trades.csv",
}

# ─────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────
def send_telegram(msg):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code != 200:
            print(f"⚠ Telegram errore: {r.text}")
    except Exception as e:
        print(f"⚠ Telegram non raggiungibile: {e}")

# ─────────────────────────────────────────
#  STATO BOT
# ─────────────────────────────────────────
def load_state():
    if os.path.exists(CONFIG["data_file"]):
        with open(CONFIG["data_file"]) as f:
            return json.load(f)
    return {
        "capital":      CONFIG["capital_start"],
        "open_trade":   None,
        "total_trades": 0,
        "wins":         0,
        "losses":       0,
        "daily_pnl":    0.0,
        "total_pnl":    0.0,
        "start_date":   datetime.now().strftime("%Y-%m-%d"),
    }

def save_state(state):
    with open(CONFIG["data_file"], "w") as f:
        json.dump(state, f, indent=2)

def log_trade(trade):
    file   = CONFIG["log_file"]
    exists = os.path.exists(file)
    pd.DataFrame([trade]).to_csv(file, mode="a", header=not exists, index=False)

# ─────────────────────────────────────────
#  SCARICA DATI DA TWELVE DATA
# ─────────────────────────────────────────
def fetch_candles(interval, outputsize):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     "XAU/USD",
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVEDATA_KEY,
        "format":     "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            print(f"⚠ Twelve Data errore: {data.get('message')}")
            return None
        values = data.get("values", [])
        if not values:
            return None
        df = pd.DataFrame(values)
        df["time"]  = pd.to_datetime(df["datetime"])
        df["open"]  = df["open"].astype(float)
        df["high"]  = df["high"].astype(float)
        df["low"]   = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"⚠ Errore download {interval}: {e}")
        return None

def get_data():
    print("   Scarico dati H1 e H4...")
    df_h1 = fetch_candles("1h", 100)
    df_h4 = fetch_candles("4h", 250)
    if df_h1 is None or df_h4 is None:
        return None, None
    print(f"   ✓ H1: {len(df_h1)} candele | H4: {len(df_h4)} candele")
    return df_h1, df_h4

# ─────────────────────────────────────────
#  INDICATORI
# ─────────────────────────────────────────
def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period):
    delta = series.diff()
    gain  = delta.where(delta > 0, 0.0)
    loss  = -delta.where(delta < 0, 0.0)
    ag    = gain.ewm(alpha=1/period, adjust=False).mean()
    al    = loss.ewm(alpha=1/period, adjust=False).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def prepare_indicators(df_h1, df_h4):
    # H4 indicatori
    df_h4 = df_h4.copy()
    df_h4["ema_fast"]   = calc_ema(df_h4["close"], CONFIG["ema_fast"])
    df_h4["ema_slow"]   = calc_ema(df_h4["close"], CONFIG["ema_slow"])
    df_h4["trend_bull"] = df_h4["ema_fast"] > df_h4["ema_slow"]

    # H1 indicatori
    df_h1 = df_h1.copy()
    df_h1["rsi"]      = calc_rsi(df_h1["close"], CONFIG["rsi_period"])
    df_h1["rsi_prev"] = df_h1["rsi"].shift(1)

    # Merge trend H4 su H1
    df_h4_idx = df_h4[["time","trend_bull"]].rename(columns={"time":"time_h4"})
    df_h1 = pd.merge_asof(df_h1.sort_values("time"), df_h4_idx,
                          left_on="time", right_on="time_h4",
                          direction="backward")
    return df_h1

# ─────────────────────────────────────────
#  SEGNALE
# ─────────────────────────────────────────
def check_signal(df_h1):
    if df_h1 is None or len(df_h1) < 3:
        return None, None

    row  = df_h1.iloc[-1]
    prev = df_h1.iloc[-2]

    if pd.isna(row.get("trend_bull")) or pd.isna(row["rsi"]):
        return None, None

    hour = datetime.now(timezone.utc).hour
    if not (CONFIG["active_hours_start"] <= hour < CONFIG["active_hours_end"]):
        return None, None

    bull     = row["trend_bull"]
    rsi      = row["rsi"]
    rsi_prev = prev["rsi"]
    close    = row["close"]
    open_    = row["open"]
    close_p  = prev["close"]

    if (bull and
        rsi_prev < CONFIG["rsi_oversold"] and rsi > CONFIG["rsi_oversold"] and
        close > open_ and close > close_p):
        return "long", float(close)

    if (not bull and
        rsi_prev > CONFIG["rsi_overbought"] and rsi < CONFIG["rsi_overbought"] and
        close < open_ and close < close_p):
        return "short", float(close)

    return None, None

# ─────────────────────────────────────────
#  CONTROLLA TRADE APERTO
# ─────────────────────────────────────────
def check_open_trade(state, df_h1):
    trade = state["open_trade"]
    if not trade:
        return state

    current = float(df_h1["close"].iloc[-1])
    high    = float(df_h1["high"].iloc[-1])
    low     = float(df_h1["low"].iloc[-1])
    entry   = trade["entry"]
    dire    = trade["direction"]
    lot     = trade["lot"]
    tp      = CONFIG["take_profit_usd"]
    sl      = CONFIG["stop_loss_usd"]

    hit_tp = hit_sl = False
    exit_p = current

    if dire == "long":
        if high >= entry + tp:
            hit_tp = True; exit_p = entry + tp
        elif low <= entry - sl:
            hit_sl = True; exit_p = entry - sl
    else:
        if low <= entry - tp:
            hit_tp = True; exit_p = entry - tp
        elif high >= entry + sl:
            hit_sl = True; exit_p = entry + sl

    if hit_tp or hit_sl:
        pnl = round((tp if hit_tp else -sl) * lot * CONFIG["oz_per_lot"], 2)
        state["capital"]      = round(state["capital"] + pnl, 2)
        state["total_pnl"]    = round(state["total_pnl"] + pnl, 2)
        state["daily_pnl"]    = round(state["daily_pnl"] + pnl, 2)
        state["total_trades"] += 1
        if hit_tp: state["wins"]   += 1
        else:      state["losses"] += 1

        esito = "✅ TAKE PROFIT" if hit_tp else "❌ STOP LOSS"
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        wr    = state["wins"] / max(state["total_trades"], 1) * 100

        msg = (
            f"<b>{esito}</b>\n\n"
            f"📊 <b>{dire.upper()}</b> XAU/USD\n"
            f"📅 Chiuso: {now}\n"
            f"🎯 Entry:  ${entry:.2f}\n"
            f"🏁 Exit:   ${exit_p:.2f}\n"
            f"📦 Lot:    {lot:.4f}\n"
            f"💰 PNL:    {'+'if pnl>0 else ''}{pnl:.2f}$\n"
            f"💼 Capitale: ${state['capital']:.2f}\n\n"
            f"📈 Totale: {state['wins']}W / {state['losses']}L "
            f"| WR: {wr:.0f}% "
            f"| PNL: ${state['total_pnl']:+.2f}"
        )
        send_telegram(msg)
        print(f"{now} | {esito} | PNL ${pnl:+.2f} | Cap ${state['capital']:.2f}")

        log_trade({
            "datetime":  now,
            "direction": dire,
            "entry":     entry,
            "exit":      round(exit_p, 2),
            "lot":       lot,
            "result":    "TP" if hit_tp else "SL",
            "pnl_usd":   pnl,
            "capitale":  state["capital"],
        })
        state["open_trade"] = None

    return state

# ─────────────────────────────────────────
#  CICLO PRINCIPALE
# ─────────────────────────────────────────
def run_cycle():
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = load_state()
    print(f"\n{now} | 🔄 Controllo mercato...")

    df_h1, df_h4 = get_data()
    if df_h1 is None:
        print("   ⚠ Dati non disponibili, riprovo al prossimo ciclo")
        return

    current_price = float(df_h1["close"].iloc[-1])
    print(f"   💵 Prezzo XAU/USD: ${current_price:.2f}")

    df_h1 = prepare_indicators(df_h1, df_h4)

    # Controlla trade aperto
    if state["open_trade"]:
        t = state["open_trade"]
        print(f"   📂 Trade aperto: {t['direction'].upper()} da ${t['entry']:.2f}")
        state = check_open_trade(state, df_h1)

    # Cerca nuovo segnale
    if not state["open_trade"]:
        signal, entry_price = check_signal(df_h1)
        if signal:
            lot = round((state["capital"] * CONFIG["risk_pct"]) /
                        (CONFIG["stop_loss_usd"] * CONFIG["oz_per_lot"]), 4)
            lot = max(lot, 0.001)

            tp_p = entry_price + CONFIG["take_profit_usd"] if signal == "long" \
                   else entry_price - CONFIG["take_profit_usd"]
            sl_p = entry_price - CONFIG["stop_loss_usd"] if signal == "long" \
                   else entry_price + CONFIG["stop_loss_usd"]

            state["open_trade"] = {
                "direction": signal,
                "entry":     entry_price,
                "lot":       lot,
                "open_time": now,
                "tp":        round(tp_p, 2),
                "sl":        round(sl_p, 2),
            }

            emoji = "📈" if signal == "long" else "📉"
            msg = (
                f"{emoji} <b>NUOVO SEGNALE — {signal.upper()}</b>\n\n"
                f"💱 XAU/USD (Oro)\n"
                f"⏰ Orario: {now}\n"
                f"💵 Entry:  ${entry_price:.2f}\n"
                f"✅ TP:     ${tp_p:.2f} (+$2.00)\n"
                f"❌ SL:     ${sl_p:.2f} (-$1.30)\n"
                f"📦 Lot:    {lot:.4f}\n"
                f"💼 Capitale: ${state['capital']:.2f}\n\n"
                f"<i>⚠️ Paper trading — nessun ordine reale</i>"
            )
            send_telegram(msg)
            print(f"   🎯 SEGNALE {signal.upper()} a ${entry_price:.2f}")
        else:
            print(f"   ⏳ Nessun segnale — in attesa")

    save_state(state)

# ─────────────────────────────────────────
#  REPORT GIORNALIERO
# ─────────────────────────────────────────
def daily_report():
    state = load_state()
    now   = datetime.now().strftime("%Y-%m-%d")
    wr    = state["wins"] / max(state["total_trades"], 1) * 100

    trade_aperto = ""
    if state["open_trade"]:
        t = state["open_trade"]
        trade_aperto = (
            f"\n\n📂 <b>Trade aperto:</b>\n"
            f"   {t['direction'].upper()} da ${t['entry']:.2f}\n"
            f"   TP: ${t['tp']:.2f} | SL: ${t['sl']:.2f}\n"
            f"   Aperto: {t['open_time']}"
        )

    msg = (
        f"📊 <b>REPORT GIORNALIERO — {now}</b>\n"
        f"{'═'*28}\n\n"
        f"💼 Capitale: <b>${state['capital']:.2f}</b>\n"
        f"📈 PNL oggi: <b>${state['daily_pnl']:+.2f}</b>\n"
        f"💰 PNL totale: <b>${state['total_pnl']:+.2f}</b>\n\n"
        f"📋 Trade totali: {state['total_trades']}\n"
        f"✅ Vincenti: {state['wins']}\n"
        f"❌ Perdenti: {state['losses']}\n"
        f"🎯 Win rate: {wr:.1f}%\n"
        f"📅 Attivo dal: {state['start_date']}"
        f"{trade_aperto}\n\n"
        f"<i>Gold Trend Scalper — Paper Trading</i>"
    )
    send_telegram(msg)
    print(f"\n📊 Report giornaliero inviato")
    state["daily_pnl"] = 0.0
    save_state(state)

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════╗")
    print("║   GOLD TREND SCALPER — Paper Trading v2     ║")
    print("╚══════════════════════════════════════════════╝\n")

    state = load_state()
    print(f"💼 Capitale: ${state['capital']:.2f}")
    print(f"📅 Attivo dal: {state['start_date']}")
    print(f"📊 Trade totali: {state['total_trades']}")
    print("\n✅ Bot avviato — controllo ogni ora")
    print("📱 Notifiche Telegram attive")
    print("⏰ Report giornaliero alle 21:00")
    print("\nPremi CTRL+C per fermare\n")

    send_telegram(
        "🚀 <b>Gold Trend Scalper v2 avviato!</b>\n\n"
        f"💼 Capitale: ${state['capital']:.2f}\n"
        f"📅 Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"⚙️ TP: $2.00 | SL: $1.30 | Rischio: 1%/trade\n"
        f"🕐 Controllo ogni ora\n"
        f"📊 Report alle 21:00\n\n"
        "<i>Paper trading — nessun ordine reale</i>"
    )

    run_cycle()

    schedule.every(1).hours.do(run_cycle)
    schedule.every().day.at("21:00").do(daily_report)

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
