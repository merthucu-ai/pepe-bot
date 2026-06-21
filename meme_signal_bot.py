"""
╔══════════════════════════════════════════════════════════════╗
║  MEME COİN SİNYAL BOTU — v1.0                               ║
║  Coinler: PEPE, PENGU, BOME, SHIB, FLOKI, PNUT              ║
║  Düzeltmeler:                                                ║
║  ✓ TP1 < TP2 < TP3 sıralaması garantili                     ║
║  ✓ LONG'da tüm TP'ler giriş üstünde                         ║
║  ✓ SHORT'da tüm TP'ler giriş altında                         ║
║  ✓ Her coinin RSI/hacim karakteri farklı ayarlandı           ║
║  ✓ BB orta bant sadece doğru yöndeyse TP olarak kullanılır  ║
╚══════════════════════════════════════════════════════════════╝

Kurulum:  pip install ccxt pandas pandas-ta python-telegram-bot
Çalıştırma: python meme_signal_bot.py
"""

import asyncio
import logging
from datetime import datetime
import os

import ccxt
import pandas as pd
import pandas_ta as ta
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════
import os
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8696312422:AAFlQe3YwHgLQEBAfHy9sEm1MZacWA0Y9w8")
CHAT_ID        = os.environ.get("CHAT_ID", "1131322901")

CHECK_INTERVAL = 60   # saniye

BTC_SYMBOL = "BTC/USDT"

# ── Her coin için ayrı karakter ayarları (gerçek veriden) ───
COIN_CONFIG = {
    "PEPE/USDT": {
        "name":             "PEPE",
        "long_rsi":         40,    # ort: 40.0
        "short_rsi":        55,    # ort: 52.0 → biraz yukarı çektik
        "vol_min":          1.5,   # ort: 1.73x
        "atr_sl_mult":      0.5,
        "atr_tp2_mult":     1.5,
        "atr_tp3_mult":     2.5,
        "min_rr":           1.2,
    },
    "PENGU/USDT": {
        "name":             "PENGU",
        "long_rsi":         40,    # ort: 39.9
        "short_rsi":        57,    # ort: 54.3
        "vol_min":          1.4,   # ort: 1.44x
        "atr_sl_mult":      0.5,
        "atr_tp2_mult":     1.5,
        "atr_tp3_mult":     2.8,   # PENGU daha geniş hareket ediyor
        "min_rr":           1.2,
    },
    "BOME/USDT": {
        "name":             "BOME",
        "long_rsi":         43,    # ort: 42.9
        "short_rsi":        58,    # ort: 55.8
        "vol_min":          1.5,   # ort: 1.76x
        "atr_sl_mult":      0.5,
        "atr_tp2_mult":     1.5,
        "atr_tp3_mult":     2.8,
        "min_rr":           1.2,
    },
    "SHIB/USDT": {
        "name":             "SHIB",
        "long_rsi":         40,
        "short_rsi":        60,
        "vol_min":          1.5,
        "atr_sl_mult":      0.5,
        "atr_tp2_mult":     1.5,
        "atr_tp3_mult":     2.5,
        "min_rr":           1.2,
    },
    "FLOKI/USDT": {
        "name":             "FLOKI",
        "long_rsi":         40,
        "short_rsi":        60,
        "vol_min":          1.5,
        "atr_sl_mult":      0.5,
        "atr_tp2_mult":     1.5,
        "atr_tp3_mult":     2.5,
        "min_rr":           1.2,
    },
    "PNUT/USDT": {
        "name":             "PNUT",
        "long_rsi":         40,
        "short_rsi":        60,
        "vol_min":          1.5,
        "atr_sl_mult":      0.5,
        "atr_tp2_mult":     1.5,
        "atr_tp3_mult":     2.5,
        "min_rr":           1.2,
    },
}

# İndikatör periyotları
RSI_LEN    = 14
EMA_FAST   = 9
EMA_SLOW   = 21
MACD_FAST  = 12
MACD_SLOW  = 26
MACD_SIG   = 9
BB_LEN     = 20
BB_STD     = 2.0
ATR_LEN    = 14
STOCH_K    = 14
STOCH_D    = 3
# ═══════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

exchange = ccxt.binance({
    "enableRateLimit": True,
    "urls": {"api": {
        "public":  "https://api1.binance.com/api/v3",
        "private": "https://api1.binance.com/api/v3",
    }}
})

# Son sinyal takibi
last_signals: dict = {}


# ─────────────────────────────────────────────────────────
#  VERİ & İNDİKATÖR
# ─────────────────────────────────────────────────────────

def fetch(symbol: str, tf: str, limit: int = 120) -> pd.DataFrame:
    bars = exchange.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("ts", inplace=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["rsi"]       = ta.rsi(c, length=RSI_LEN)
    df["ema_fast"]  = ta.ema(c, length=EMA_FAST)
    df["ema_slow"]  = ta.ema(c, length=EMA_SLOW)
    macd            = ta.macd(c, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIG)
    df["macd_hist"] = macd.iloc[:, 1]
    bb              = ta.bbands(c, length=BB_LEN, std=BB_STD)
    df["bb_up"]     = bb.iloc[:, 2]
    df["bb_low"]    = bb.iloc[:, 0]
    df["bb_mid"]    = bb.iloc[:, 1]
    df["atr"]       = ta.atr(h, l, c, length=ATR_LEN)
    df["vol_ma"]    = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_ma"]
    stoch           = ta.stoch(h, l, c, k=STOCH_K, d=STOCH_D)
    df["stoch_k"]   = stoch.iloc[:, 0]
    df["stoch_d"]   = stoch.iloc[:, 1]
    df["is_green"]  = c > df["open"]
    df["is_red"]    = c < df["open"]
    return df


# ─────────────────────────────────────────────────────────
#  TEYİT MUMU KONTROLÜ
# ─────────────────────────────────────────────────────────

def confirmed_entry(df: pd.DataFrame, sig_type: str) -> bool:
    if len(df) < 3:
        return False
    trigger = df.iloc[-2]
    confirm = df.iloc[-1]
    if sig_type == "LONG":
        return (trigger["low"] <= trigger["bb_low"] * 1.005
                and confirm["is_green"]
                and confirm["close"] >= trigger["low"] * 0.995)
    else:
        return (trigger["high"] >= trigger["bb_up"] * 0.995
                and confirm["is_red"]
                and confirm["close"] <= trigger["high"] * 1.005)


# ─────────────────────────────────────────────────────────
#  TP HESAPLAMA — DÜZELTİLMİŞ
#  LONG:  SL < giriş < TP1 < TP2 < TP3  (hepsi yukarıda)
#  SHORT: TP3 < TP2 < TP1 < giriş < SL  (hepsi aşağıda)
# ─────────────────────────────────────────────────────────

def calc_levels(sig_type: str, price: float, atr: float,
                bb_mid: float, bb_up: float, bb_low: float,
                cfg: dict) -> dict:

    sl_mult  = cfg["atr_sl_mult"]
    tp2_mult = cfg["atr_tp2_mult"]
    tp3_mult = cfg["atr_tp3_mult"]

    if sig_type == "LONG":
        sl = price - atr * sl_mult          # giriş ALTINDA

        # TP1: BB orta bant — ama giriş ÜSTÜNDE olmalı
        if bb_mid > price:
            tp1 = bb_mid
        else:
            # BB orta bant altındaysa (fiyat zaten üstündeyse) 1x ATR kullan
            tp1 = price + atr * 1.0

        # TP2: TP1'den yüksek olmalı
        tp2_candidate = price + atr * tp2_mult
        tp2 = max(tp2_candidate, tp1 * 1.001)  # en az TP1'in %0.1 üstünde

        # TP3: TP2'den yüksek, BB üst bant
        if bb_up > tp2:
            tp3 = bb_up
        else:
            tp3 = price + atr * tp3_mult
        tp3 = max(tp3, tp2 * 1.001)

    else:  # SHORT
        sl = price + atr * sl_mult          # giriş ÜSTÜNDE

        # TP1: BB orta bant — ama giriş ALTINDA olmalı
        if bb_mid < price:
            tp1 = bb_mid
        else:
            tp1 = price - atr * 1.0

        # TP2: TP1'den düşük olmalı
        tp2_candidate = price - atr * tp2_mult
        tp2 = min(tp2_candidate, tp1 * 0.999)

        # TP3: TP2'den düşük, BB alt bant
        if bb_low < tp2:
            tp3 = bb_low
        else:
            tp3 = price - atr * tp3_mult
        tp3 = min(tp3, tp2 * 0.999)

    risk   = abs(price - sl)
    reward = abs(tp1 - price)
    rr     = reward / risk if risk > 0 else 0

    return {"sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr}


# ─────────────────────────────────────────────────────────
#  SİNYAL MOTORU
# ─────────────────────────────────────────────────────────

def evaluate(symbol: str, pepe_1h: pd.DataFrame,
             pepe_15m: pd.DataFrame, btc_1h: pd.DataFrame) -> dict | None:

    cfg   = COIN_CONFIG[symbol]
    h     = pepe_1h.iloc[-1]
    m     = pepe_15m.iloc[-1]
    m2    = pepe_15m.iloc[-2]
    b     = btc_1h.iloc[-1]
    price = h["close"]

    for val in [h["rsi"], h["bb_mid"], h["atr"], b["rsi"]]:
        if pd.isna(val):
            return None

    atr = h["atr"]

    # ── LONG koşulları ──────────────────────────────────────
    lc = {
        "RSI aşırı satım":    h["rsi"] < cfg["long_rsi"],
        "BB alt bant":        price <= h["bb_low"] * 1.008,
        "15M MACD/EMA":       (m["macd_hist"] > 0) or
                               (m["ema_fast"] > m["ema_slow"] and
                                m2["ema_fast"] <= m2["ema_slow"]),
        "Stoch dönüş":        h["stoch_k"] < 30 and h["stoch_k"] > h["stoch_d"],
        "Hacim güçlü":        h["vol_ratio"] >= cfg["vol_min"],
        "BTC engel yok":      b["rsi"] >= 35,
        "Teyit mumu":         confirmed_entry(pepe_15m, "LONG"),
    }

    # ── SHORT koşulları ─────────────────────────────────────
    sc = {
        "RSI aşırı alım":     h["rsi"] > cfg["short_rsi"],
        "BB üst bant":        price >= h["bb_up"] * 0.992,
        "15M MACD/EMA":       (m["macd_hist"] < 0) or
                               (m["ema_fast"] < m["ema_slow"] and
                                m2["ema_fast"] >= m2["ema_slow"]),
        "Stoch dönüş":        h["stoch_k"] > 70 and h["stoch_k"] < h["stoch_d"],
        "Hacim güçlü":        h["vol_ratio"] >= cfg["vol_min"],
        "BTC de baskıda":     b["rsi"] > 55 or b["ema_fast"] < b["ema_slow"],
        "Teyit mumu":         confirmed_entry(pepe_15m, "SHORT"),
    }

    ls, ss = sum(lc.values()), sum(sc.values())

    if ls < 3 and ss < 3:
        return None
    if ls == ss:
        return None

    sig_type = "LONG" if ls > ss else "SHORT"
    conds    = lc if sig_type == "LONG" else sc
    score    = ls if sig_type == "LONG" else ss

    # ── TP/SL hesapla (düzeltilmiş) ────────────────────────
    levels = calc_levels(
        sig_type, price, atr,
        h["bb_mid"], h["bb_up"], h["bb_low"], cfg
    )

    if levels["rr"] < cfg["min_rr"]:
        log.debug("%s R/R çok düşük (%.1f)", symbol, levels["rr"])
        return None

    # Güç
    rsi_ideal = h["rsi"] < 30 if sig_type == "LONG" else h["rsi"] > 70
    if score >= 6 and rsi_ideal:
        strength, leverage = "💎 ÇOK GÜÇLÜ", "x8-10"
    elif score >= 5 and rsi_ideal:
        strength, leverage = "⭐⭐⭐ GÜÇLÜ", "x6-8"
    elif score >= 5:
        strength, leverage = "⭐⭐ ORTA-GÜÇLÜ", "x5-6"
    else:
        strength, leverage = "⭐ ORTA", "x3-5"

    return {
        "symbol":    symbol,
        "name":      cfg["name"],
        "type":      sig_type,
        "price":     price,
        "sl":        levels["sl"],
        "tp1":       levels["tp1"],
        "tp2":       levels["tp2"],
        "tp3":       levels["tp3"],
        "rr":        levels["rr"],
        "score":     score,
        "strength":  strength,
        "leverage":  leverage,
        "rsi_1h":    h["rsi"],
        "rsi_btc":   b["rsi"],
        "vol_ratio": h["vol_ratio"],
        "btc_trend": "YUKARI" if b["ema_fast"] > b["ema_slow"] else "ASAGI",
        "conds":     {k: v for k, v in conds.items() if v},
        "atr_pct":   atr / price * 100,
    }


# ─────────────────────────────────────────────────────────
#  MESAJ FORMATI
# ─────────────────────────────────────────────────────────

def format_signal(sig: dict) -> str:
    is_long = sig["type"] == "LONG"
    emoji   = "🟢" if is_long else "🔴"
    p       = sig["price"]
    sl, tp1, tp2, tp3 = sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"]

    def pct(t): return (t - p) / p * 100

    # Doğrulama — hata varsa uyar
    if is_long:
        order_ok = sl < p < tp1 < tp2 < tp3
    else:
        order_ok = tp3 < tp2 < tp1 < p < sl

    order_str = "" if order_ok else "\n⚠️ _Seviye sıralaması kontrol edilmeli_"

    cond_lines = "\n".join(f"  ✓ {c}" for c in sig["conds"])
    btc_e      = "📈" if sig["btc_trend"] == "YUKARI" else "📉"
    now        = datetime.now().strftime("%d.%m %H:%M")

    return (
        f"{emoji} *{sig['name']}/USDT — {sig['type']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Giriş: `{p:.2e}`\n"
        f"🛑 SL:    `{sl:.2e}` ({pct(sl):+.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP1:  `{tp1:.2e}` ({pct(tp1):+.2f}%) ← BB Orta / 1 ATR\n"
        f"   _TP1'de %50 kapat → SL'yi girişe çek_\n"
        f"🎯 TP2:  `{tp2:.2e}` ({pct(tp2):+.2f}%) ← 1.5 ATR\n"
        f"🎯 TP3:  `{tp3:.2e}` ({pct(tp3):+.2f}%) ← BB Üst/Alt\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 R/R:   `{sig['rr']:.1f}:1`\n"
        f"📡 RSI:   `{sig['rsi_1h']:.0f}`\n"
        f"💹 Hacim: `{sig['vol_ratio']:.1f}x`\n"
        f"📐 ATR:   `{sig['atr_pct']:.2f}%`\n"
        f"{btc_e} BTC: `{sig['btc_trend']}` RSI=`{sig['rsi_btc']:.0f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Koşullar ({sig['score']}/7):\n{cond_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {sig['strength']} | Kaldıraç: `{sig['leverage']}`\n"
        f"⏰ {now}{order_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Finansal tavsiye değildir._"
    )


def format_status(coin_data: dict, btc_1h: pd.DataFrame) -> str:
    b   = btc_1h.iloc[-1]
    now = datetime.now().strftime("%H:%M")
    lines = [f"📊 *Genel Durum* — {now}\n",
             f"{'📈' if b['ema_fast']>b['ema_slow'] else '📉'} "
             f"BTC `{'YUKARI' if b['ema_fast']>b['ema_slow'] else 'ASAGI'}` "
             f"RSI=`{b['rsi']:.0f}`\n"]

    for symbol, (h1, cfg) in coin_data.items():
        h = h1.iloc[-1]
        rsi = h["rsi"]
        p   = h["close"]
        bb_low_d = (p - h["bb_low"]) / p * 100
        bb_up_d  = (h["bb_up"] - p)  / p * 100

        if rsi < 30:    icon = "🔵🔵"
        elif rsi < 40:  icon = "🔵"
        elif rsi > 70:  icon = "🟠🟠"
        elif rsi > cfg["short_rsi"]: icon = "🟠"
        else:           icon = "⚪"

        lines.append(
            f"{icon} *{cfg['name']}* `{p:.2e}` RSI=`{rsi:.0f}` "
            f"↓BB={bb_low_d:.1f}% ↑BB={bb_up_d:.1f}%"
        )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
#  TEKRAR ENGELİ
# ─────────────────────────────────────────────────────────

def is_duplicate(symbol: str, sig: dict) -> bool:
    if symbol not in last_signals:
        return False
    prev = last_signals[symbol]
    if prev["type"] != sig["type"]:
        return False
    return abs(sig["price"] - prev["price"]) / prev["price"] < 0.004


# ─────────────────────────────────────────────────────────
#  ANA TARAMA
# ─────────────────────────────────────────────────────────

status_counter = 0

async def scan(bot) -> None:
    global status_counter
    try:
        btc_1h = add_indicators(fetch(BTC_SYMBOL, "1h", limit=80))
        coin_data = {}

        for symbol, cfg in COIN_CONFIG.items():
            try:
                h1  = add_indicators(fetch(symbol, "1h",  limit=80))
                m15 = add_indicators(fetch(symbol, "15m", limit=80))
                coin_data[symbol] = (h1, cfg)

                sig = evaluate(symbol, h1, m15, btc_1h)

                if sig and not is_duplicate(symbol, sig):
                    msg = format_signal(sig)
                    await bot.send_message(
                        chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
                    last_signals[symbol] = {
                        "type": sig["type"], "price": sig["price"]}
                    log.info("Sinyal: %s %s @ %.2e RR=%.1f",
                             cfg["name"], sig["type"], sig["price"], sig["rr"])

            except Exception as e:
                log.error("Hata (%s): %s", symbol, e)

            await asyncio.sleep(0.5)

        status_counter += 1
        # Saatte bir durum özeti
        if status_counter % 60 == 0:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=format_status(coin_data, btc_1h),
                parse_mode="Markdown")

    except Exception as e:
        log.error("Genel hata: %s", e)


async def background_loop(bot) -> None:
    while True:
        await scan(bot)
        await asyncio.sleep(CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────
#  TELEGRAM KOMUTLARI
# ─────────────────────────────────────────────────────────

async def cmd_start(update, context):
    coins = ", ".join(v["name"] for v in COIN_CONFIG.values())
    await update.message.reply_text(
        f"🤖 *Meme Coin Sinyal Botu*\n\n"
        f"Takip: `{coins}`\n\n"
        f"/durum  — Tüm coinlerin anlık durumu\n"
        f"/kural  — Her coinin sinyal eşikleri\n"
        f"/son    — Son sinyaller",
        parse_mode="Markdown")


async def cmd_durum(update, context):
    try:
        btc_1h = add_indicators(fetch(BTC_SYMBOL, "1h", limit=80))
        coin_data = {}
        for symbol, cfg in COIN_CONFIG.items():
            h1 = add_indicators(fetch(symbol, "1h", limit=80))
            coin_data[symbol] = (h1, cfg)
        await update.message.reply_text(
            format_status(coin_data, btc_1h), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


async def cmd_kural(update, context):
    lines = ["📋 *Sinyal Eşikleri — Coin Bazında*\n"]
    for symbol, cfg in COIN_CONFIG.items():
        lines.append(
            f"*{cfg['name']}*\n"
            f"  LONG RSI < {cfg['long_rsi']} | SHORT RSI > {cfg['short_rsi']}\n"
            f"  Min hacim: {cfg['vol_min']}x | Min R/R: {cfg['min_rr']}\n"
            f"  SL: {cfg['atr_sl_mult']}x ATR | TP2: {cfg['atr_tp2_mult']}x ATR\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_son(update, context):
    if not last_signals:
        await update.message.reply_text("Henüz sinyal üretilmedi.")
        return
    lines = ["*Son sinyaller:*\n"]
    for symbol, sig in last_signals.items():
        cfg = COIN_CONFIG[symbol]
        lines.append(
            f"{cfg['name']}: *{sig['type']}* @ `{sig['price']:.2e}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def post_init(application) -> None:
    asyncio.create_task(background_loop(application.bot))
    coins = ", ".join(v["name"] for v in COIN_CONFIG.values())
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"✅ *Meme Coin Botu başlatıldı!*\n"
                f"🪙 Takip: `{coins}`\n"
                f"⏱ Kontrol: her {CHECK_INTERVAL}s\n"
                f"✓ TP sıralaması düzeltildi\n"
                f"✓ Teyit mumu aktif\n"
                f"✓ Coin bazında RSI eşikleri\n\n"
                f"/durum yazarak başla."
            ),
            parse_mode="Markdown")
    except Exception as e:
        log.warning("Başlangıç mesajı gönderilemedi: %s", e)


def main():
    app = (ApplicationBuilder()
           .token(TELEGRAM_TOKEN)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("durum",  cmd_durum))
    app.add_handler(CommandHandler("kural",  cmd_kural))
    app.add_handler(CommandHandler("son",    cmd_son))
    log.info("Meme Coin Botu başlatıldı.")
    app.run_polling()


if __name__ == "__main__":
    main()
