"""
╔══════════════════════════════════════════════════════════╗
║  PEPE/USDT Sinyal Botu — Gerçek Veri Analizine Dayalı  ║
║  Kurallar: 1H ana trend + 15M giriş + BTC filtresi      ║
╚══════════════════════════════════════════════════════════╝

Kurulum:
    pip install ccxt pandas pandas-ta python-telegram-bot

Çalıştırma:
    python pepe_signal_bot.py

Ayarlar için sadece aşağıdaki CONFIG bloğunu düzenle.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import ccxt
import pandas as pd
import pandas_ta as ta
from telegram import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ═══════════════════════════════════════════════════════════
#  CONFIG — sadece bu bloğu düzenle
# ═══════════════════════════════════════════════════════════
import os
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8696312422:AAFlQe3YwHgLQEBAfHy9sEm1MZacWA0Y9w8")
CHAT_ID        = os.environ.get("CHAT_ID", "1131322901")

PEPE_SYMBOL     = "PEPE/USDT"
BTC_SYMBOL      = "BTC/USDT"

CHECK_INTERVAL  = 60   # saniye — kaç saniyede bir kontrol

# ── Sinyal eşikleri (gerçek veriden türetildi) ──────────────
# LONG
RSI_LONG_MAX        = 40    # 1H RSI bu değerin altında olmalı (veriden: ortalama 38.1)
RSI_LONG_IDEAL      = 35    # Bu seviyenin altı "güçlü" LONG
BB_LONG_TOUCH       = 1.005 # Fiyat BB altının max %0.5 üstünde olabilir
VOL_MIN_LONG        = 1.5   # Hacim ortalamasının kaç katı (veriden: 1.83x ortalama)

# SHORT
RSI_SHORT_MIN       = 57    # 1H RSI bu değerin üstünde olmalı (veriden: ortalama 59.6)
RSI_SHORT_IDEAL     = 63    # Bu seviyenin üstü "güçlü" SHORT (14 Haz 21:00: RSI=71)
BB_SHORT_TOUCH      = 0.995 # Fiyat BB üstünün min %0.5 altında olabilir
VOL_MIN_SHORT       = 1.5   # Hacim ortalamasının kaç katı (veriden: 2.14x ortalama)

# BTC filtresi
BTC_RSI_BLOCK_LONG  = 35    # BTC RSI bu değerin altındaysa LONG engelle
BTC_RSI_BOOST_SHORT = 55    # BTC RSI bu değerin üstündeyse SHORT güçlü say

# İndikatör periyotları
RSI_LEN     = 14
EMA_FAST    = 9
EMA_SLOW    = 21
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIG    = 9
BB_LEN      = 20
BB_STD      = 2.0
ATR_LEN     = 14
VOL_MA_LEN  = 20

MIN_SCORE   = 3     # Kaç koşul sağlanırsa sinyal üretilsin (max 5)
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

exchange = ccxt.binance({
    "enableRateLimit": True,
    "urls": {
        "api": {
            "public": "https://api1.binance.com/api/v3",
            "private": "https://api1.binance.com/api/v3",
        }
    }
})

# Son sinyal takibi (tekrar önleme)
last_signal: dict = {"type": None, "price": 0.0, "time": None}


# ───────────────────────────────────────────────────────────
#  VERİ ÇEKME & İNDİKATÖR
# ───────────────────────────────────────────────────────────

def fetch_ohlcv(symbol: str, tf: str, limit: int = 100) -> pd.DataFrame:
    bars = exchange.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
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
    df["atr"]       = ta.atr(h, l, c, length=ATR_LEN)
    df["vol_ma"]    = v.rolling(VOL_MA_LEN).mean()
    df["vol_ratio"] = v / df["vol_ma"]
    return df


# ───────────────────────────────────────────────────────────
#  SİNYAL MOTORU
# ───────────────────────────────────────────────────────────

def evaluate(pepe_1h: pd.DataFrame, pepe_15m: pd.DataFrame, btc_1h: pd.DataFrame) -> dict | None:
    """
    Ana sinyal motoru.
    1H → ana karar   (RSI, BB, EMA hizalaması)
    15M → giriş teyit (EMA kesişimi, MACD dönüşü)
    BTC 1H → filtre
    """
    h  = pepe_1h.iloc[-1]
    h2 = pepe_1h.iloc[-2]   # önceki mum
    m  = pepe_15m.iloc[-1]
    m2 = pepe_15m.iloc[-2]
    b  = btc_1h.iloc[-1]

    # NaN kontrolü
    for val in [h["rsi"], h["bb_up"], h["bb_low"], h["atr"], b["rsi"]]:
        if pd.isna(val):
            return None

    price  = h["close"]
    atr    = h["atr"]

    # ── LONG KOŞULLARI ──────────────────────────────────────
    lc = {
        "1H RSI aşırı satım":
            h["rsi"] < RSI_LONG_MAX,
        "1H BB alt bant teması":
            price <= h["bb_low"] * BB_LONG_TOUCH,
        "1H hacim güçlü":
            h["vol_ratio"] >= VOL_MIN_LONG,
        "15M EMA yukarı kesişim / MACD +":
            (m["ema_fast"] > m["ema_slow"] and m2["ema_fast"] <= m2["ema_slow"])
            or (m["macd_hist"] > 0 and m2["macd_hist"] <= 0),
        "BTC engel yok":
            b["rsi"] >= BTC_RSI_BLOCK_LONG,
    }

    # ── SHORT KOŞULLARI ─────────────────────────────────────
    sc = {
        "1H RSI aşırı alım":
            h["rsi"] > RSI_SHORT_MIN,
        "1H BB üst bant teması":
            price >= h["bb_up"] * BB_SHORT_TOUCH,
        "1H hacim güçlü":
            h["vol_ratio"] >= VOL_MIN_SHORT,
        "15M EMA aşağı kesişim / MACD −":
            (m["ema_fast"] < m["ema_slow"] and m2["ema_fast"] >= m2["ema_slow"])
            or (m["macd_hist"] < 0 and m2["macd_hist"] >= 0),
        "BTC de baskıda":
            b["rsi"] > BTC_RSI_BOOST_SHORT
            or b["ema_fast"] < b["ema_slow"],
    }

    long_score  = sum(lc.values())
    short_score = sum(sc.values())

    best_type  = None
    best_score = 0
    best_conds = {}

    if long_score >= MIN_SCORE and long_score > short_score:
        best_type  = "LONG"
        best_score = long_score
        best_conds = lc
    elif short_score >= MIN_SCORE and short_score > long_score:
        best_type  = "SHORT"
        best_score = short_score
        best_conds = sc

    if not best_type:
        return None

    # SL / TP hesapla
    if best_type == "LONG":
        sl = h["bb_low"] - atr            # BB alt − 1 ATR
        tp1 = price + atr * 2             # 1. hedef (kısa)
        tp2 = h["bb_up"]                  # 2. hedef (BB ortası-üstü)
        rsi_ideal = h["rsi"] < RSI_LONG_IDEAL
    else:
        sl  = h["bb_up"] + atr            # BB üst + 1 ATR
        tp1 = price - atr * 2             # 1. hedef
        tp2 = h["bb_low"]                 # 2. hedef
        rsi_ideal = h["rsi"] > RSI_SHORT_IDEAL

    risk   = abs(price - sl)
    reward = abs(tp1 - price)
    rr     = reward / risk if risk > 0 else 0

    strength = "💎 ÇOK GÜÇLÜ" if best_score == 5 else \
               "⭐⭐⭐ GÜÇLÜ"  if (best_score == 4 and rsi_ideal) else \
               "⭐⭐ ORTA"     if best_score == 4 else \
               "⭐ ZAYIF"

    return {
        "type":       best_type,
        "price":      price,
        "sl":         sl,
        "tp1":        tp1,
        "tp2":        tp2,
        "rr":         rr,
        "score":      best_score,
        "strength":   strength,
        "rsi_1h":     h["rsi"],
        "rsi_btc":    b["rsi"],
        "vol_ratio":  h["vol_ratio"],
        "btc_trend":  "YUKARI" if b["ema_fast"] > b["ema_slow"] else "ASAGI",
        "conds":      {k: v for k, v in best_conds.items() if v},
        "atr":        atr,
    }


# ───────────────────────────────────────────────────────────
#  MESAJ FORMATI
# ───────────────────────────────────────────────────────────

def format_message(sig: dict) -> str:
    is_long   = sig["type"] == "LONG"
    emoji     = "🟢" if is_long else "🔴"
    direction = "LONG  ⬆" if is_long else "SHORT ⬇"
    p         = sig["price"]
    sl        = sig["sl"]
    tp1       = sig["tp1"]
    tp2       = sig["tp2"]
    sl_pct    = (sl  - p) / p * 100
    tp1_pct   = (tp1 - p) / p * 100
    tp2_pct   = (tp2 - p) / p * 100
    now       = datetime.now().strftime("%d.%m.%Y %H:%M")

    cond_lines = "\n".join(f"  ✓ {c}" for c in sig["conds"])

    btc_emoji = "📈" if sig["btc_trend"] == "YUKARI" else "📉"

    return (
        f"{emoji} *PEPE/USDT — {direction}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Giriş:  `{p:.2e}`\n"
        f"🛑 SL:     `{sl:.2e}` ({sl_pct:+.1f}%)\n"
        f"🎯 TP1:    `{tp1:.2e}` ({tp1_pct:+.1f}%)\n"
        f"🎯 TP2:    `{tp2:.2e}` ({tp2_pct:+.1f}%)\n"
        f"📊 R/R:    `{sig['rr']:.1f}:1`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 RSI (1H):  `{sig['rsi_1h']:.0f}`\n"
        f"💹 Hacim:     `{sig['vol_ratio']:.1f}x` ortalama\n"
        f"{btc_emoji} BTC trend:  `{sig['btc_trend']}`  RSI=`{sig['rsi_btc']:.0f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Sağlanan koşullar ({sig['score']}/5):\n"
        f"{cond_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Güç: {sig['strength']}\n"
        f"⏰ {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Finansal tavsiye değildir. Kendi analizini yap._"
    )


def format_no_signal_update(pepe_1h: pd.DataFrame, btc_1h: pd.DataFrame) -> str:
    """Sinyal yokken durum özeti (isteğe bağlı periyodik güncelleme)."""
    h = pepe_1h.iloc[-1]
    b = btc_1h.iloc[-1]
    p = h["close"]
    rsi = h["rsi"]
    vol = h["vol_ratio"]

    if rsi < 35:
        sentiment = "🔵 Aşırı satım bölgesi — LONG yaklaşıyor olabilir"
    elif rsi > 63:
        sentiment = "🟠 Aşırı alım bölgesi — SHORT yaklaşıyor olabilir"
    elif 45 <= rsi <= 55:
        sentiment = "⚪ Nötr bölge — bekle"
    elif rsi < 45:
        sentiment = "🔵 Satım bölgesine yaklaşıyor"
    else:
        sentiment = "🟠 Alım bölgesine yaklaşıyor"

    btc_s = "📈 YUKARI" if b["ema_fast"] > b["ema_slow"] else "📉 ASAGI"
    now = datetime.now().strftime("%H:%M")

    return (
        f"📊 *PEPE Durum Güncellemesi* — {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat:     `{p:.2e}`\n"
        f"📡 RSI(1H):   `{rsi:.0f}`\n"
        f"💹 Hacim:     `{vol:.1f}x`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{sentiment}\n"
        f"{btc_s} BTC EMA | RSI=`{b['rsi']:.0f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Aktif sinyal: yok — taramaya devam ediliyor"
    )


# ───────────────────────────────────────────────────────────
#  TEKRAR SİNYAL ENGELİ
# ───────────────────────────────────────────────────────────

def is_duplicate(new: dict) -> bool:
    global last_signal
    if last_signal["type"] != new["type"]:
        return False
    price_diff = abs(new["price"] - last_signal["price"]) / last_signal["price"]
    # Aynı yönde, fiyat %0.5'ten az değişmişse tekrar sayılır
    return price_diff < 0.005


# ───────────────────────────────────────────────────────────
#  ANA TARAMA DÖNGÜSÜ
# ───────────────────────────────────────────────────────────

status_counter = 0   # Her kaç taramada bir durum mesajı gönderilsin

async def scan(bot: Bot) -> None:
    global last_signal, status_counter
    try:
        pepe_1h  = add_indicators(fetch_ohlcv(PEPE_SYMBOL, "1h",  limit=60))
        pepe_15m = add_indicators(fetch_ohlcv(PEPE_SYMBOL, "15m", limit=60))
        btc_1h   = add_indicators(fetch_ohlcv(BTC_SYMBOL,  "1h",  limit=60))

        sig = evaluate(pepe_1h, pepe_15m, btc_1h)
        status_counter += 1

        if sig and not is_duplicate(sig):
            msg = format_message(sig)
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            last_signal = {"type": sig["type"], "price": sig["price"], "time": datetime.now()}
            log.info("Sinyal gönderildi: %s @ %.2e (skor %d/5)", sig["type"], sig["price"], sig["score"])
        else:
            log.debug("Sinyal yok (RSI=%.1f, vol=%.1fx)", pepe_1h.iloc[-1]["rsi"], pepe_1h.iloc[-1]["vol_ratio"])

        # Her 60 taramada bir (yaklaşık 1 saatte bir) durum güncellemesi gönder
        if status_counter % 60 == 0:
            update = format_no_signal_update(pepe_1h, btc_1h)
            await bot.send_message(chat_id=CHAT_ID, text=update, parse_mode="Markdown")

    except Exception as e:
        log.error("Tarama hatası: %s", e)


async def background_loop(bot: Bot) -> None:
    while True:
        await scan(bot)
        await asyncio.sleep(CHECK_INTERVAL)


# ───────────────────────────────────────────────────────────
#  TELEGRAM KOMUTLARI
# ───────────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *PEPE Sinyal Botu aktif!*\n\n"
        "Komutlar:\n"
        "/durum   — Anlık fiyat + RSI durumu\n"
        "/kural   — Sinyal kurallarını göster\n"
        "/son     — Son sinyali göster\n"
        "/tarama  — Manuel tarama başlat",
        parse_mode="Markdown",
    )


async def cmd_durum(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pepe_1h = add_indicators(fetch_ohlcv(PEPE_SYMBOL, "1h", limit=60))
        btc_1h  = add_indicators(fetch_ohlcv(BTC_SYMBOL,  "1h", limit=60))
        msg = format_no_signal_update(pepe_1h, btc_1h)
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


async def cmd_kural(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Sinyal Kuralları* (gerçek 1 haftalık veriden türetildi)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 *LONG — 3/5 koşul sağlanmalı:*\n"
        "1. RSI(1H) < 40  (ideali < 35)\n"
        "2. Fiyat Bollinger Alt Bandına değdi\n"
        "3. Hacim ortalamanın 1.5x+ üstü\n"
        "4. 15M'de EMA(9) yukarı kesti veya MACD + geçti\n"
        "5. BTC RSI > 35 (BTC panik satışı yok)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔴 *SHORT — 3/5 koşul sağlanmalı:*\n"
        "1. RSI(1H) > 57  (ideali > 63)\n"
        "2. Fiyat Bollinger Üst Bandına değdi\n"
        "3. Hacim ortalamanın 1.5x+ üstü\n"
        "4. 15M'de EMA(9) aşağı kesti veya MACD − geçti\n"
        "5. BTC RSI > 55 veya BTC EMA aşağıda\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🛑 *SL Kuralı:*\n"
        "LONG → BB alt bandı − 1 ATR\n"
        "SHORT → BB üst bandı + 1 ATR\n\n"
        "⚠️ _1M tek başına sinyal olarak kullanılmaz._",
        parse_mode="Markdown",
    )


async def cmd_son(update, context: ContextTypes.DEFAULT_TYPE):
    global last_signal
    if not last_signal["type"]:
        await update.message.reply_text("Henüz sinyal üretilmedi.")
    else:
        t = last_signal["time"].strftime("%d.%m %H:%M") if last_signal["time"] else "?"
        await update.message.reply_text(
            f"Son sinyal: *{last_signal['type']}* @ `{last_signal['price']:.2e}` — {t}",
            parse_mode="Markdown",
        )


async def cmd_tarama(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Manuel tarama başlatıldı...")
    await scan(context.bot)
    await update.message.reply_text("✅ Tarama tamamlandı.")


# ───────────────────────────────────────────────────────────
#  BAŞLATMA
# ───────────────────────────────────────────────────────────

async def post_init(application) -> None:
    asyncio.create_task(background_loop(application.bot))
    # Başlangıç bildirimi
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "✅ *PEPE Sinyal Botu başlatıldı!*\n"
                f"⏱ Kontrol aralığı: her {CHECK_INTERVAL} saniye\n"
                "📊 Zaman dilimleri: 1H (karar) + 15M (giriş teyidi)\n"
                "🔍 BTC filtresi: aktif\n\n"
                "/kural yazarak sinyal koşullarını görebilirsin."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Başlangıç mesajı gönderilemedi: %s", e)


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("durum",   cmd_durum))
    app.add_handler(CommandHandler("kural",   cmd_kural))
    app.add_handler(CommandHandler("son",     cmd_son))
    app.add_handler(CommandHandler("tarama",  cmd_tarama))

    log.info("Bot başlatıldı. Ctrl+C ile durdur.")
    app.run_polling()


if __name__ == "__main__":
    main()
