"""
╔══════════════════════════════════════════════════════════╗
║  PEPE/USDT Sinyal Botu — OPTİMİZE v2                   ║
║  3 kritik değişiklik (TP analizi verilerine dayalı):    ║
║  1. Giriş: teyit mumu bekleniyor (erken giriş engeli)  ║
║  2. TP1: BB orta bant (%63 başarı — eski 2ATR=%28)     ║
║  3. SL: 0.5 ATR (eski 1 ATR çok genişti)               ║
╚══════════════════════════════════════════════════════════╝

Kurulum:  pip install ccxt pandas pandas-ta python-telegram-bot
Çalıştırma: python pepe_signal_bot_v2.py
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID",        "BURAYA_CHAT_ID")

PEPE_SYMBOL    = "PEPE/USDT"
BTC_SYMBOL     = "BTC/USDT"
CHECK_INTERVAL = 60   # saniye

# ── Giriş koşulları ─────────────────────────────────────────
RSI_LONG_MAX   = 40    # 1H RSI altı → LONG bölgesi
RSI_LONG_IDEAL = 35    # Altı → güçlü LONG
RSI_SHORT_MIN  = 57    # 1H RSI üstü → SHORT bölgesi
RSI_SHORT_IDEAL= 63    # Üstü → güçlü SHORT
VOL_MIN        = 1.5   # Minimum hacim oranı

# ── OPTİMİZE: YENİ SL / TP KURALLARI ───────────────────────
SL_ATR_MULT    = 0.5   # ← ESKİ: 1.0 | YENİ: 0.5 (daha dar SL)
TP1_TARGET     = "BB_MID"   # ← ESKİ: 2x ATR | YENİ: BB orta bant (%63 başarı)
TP2_ATR_MULT   = 1.5   # TP2 → giriş + 1.5 ATR (kısmi çıkış)

# ── Minimum R/R filtresi ─────────────────────────────────────
MIN_RR         = 1.2   # BB orta bant mesafesi / SL mesafesi bu değerin altındaysa sinyal atla

# ── BTC filtresi ────────────────────────────────────────────
BTC_RSI_BLOCK_LONG  = 35
BTC_RSI_BOOST_SHORT = 55

# İndikatör periyotları
RSI_LEN, EMA_FAST, EMA_SLOW = 14, 9, 21
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
BB_LEN, BB_STD, ATR_LEN = 20, 2.0, 14
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

last_signal: dict = {"type": None, "price": 0.0}


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
    df["bb_mid"]    = bb.iloc[:, 1]   # ← TP1 hedefi
    df["atr"]       = ta.atr(h, l, c, length=ATR_LEN)
    df["vol_ma"]    = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_ma"]
    df["is_green"]  = c > df["open"]
    df["is_red"]    = c < df["open"]
    return df


# ─────────────────────────────────────────────────────────
#  OPTİMİZE GİRİŞ MANTIĞI
#  Kritik değişiklik: "teyit mumu" bekle
#  BB bandına değen mum kapandıktan SONRA
#  bir sonraki mum yönü teyit ederse gir
# ─────────────────────────────────────────────────────────

def check_confirmed_entry(df: pd.DataFrame, sig_type: str) -> bool:
    """
    Son 2 muma bakar:
    - [-2]: BB bandına değen mum (tetikleyici)
    - [-1]: Teyit mumu (kapanmış)
    LONG: teyit mumu yeşil kapanmış olmalı
    SHORT: teyit mumu kırmızı kapanmış olmalı
    """
    if len(df) < 3:
        return False

    trigger = df.iloc[-2]   # BB'ye değen mum
    confirm = df.iloc[-1]   # teyit mumu (son kapanan)

    if sig_type == "LONG":
        bb_touched    = trigger["low"] <= trigger["bb_low"] * 1.005
        confirm_green = confirm["is_green"]
        # Teyit mumu BB altında kapanmamış olmalı (daha derin düşmedi)
        not_lower     = confirm["close"] >= trigger["low"] * 0.995
        return bb_touched and confirm_green and not_lower

    else:  # SHORT
        bb_touched   = trigger["high"] >= trigger["bb_up"] * 0.995
        confirm_red  = confirm["is_red"]
        not_higher   = confirm["close"] <= trigger["high"] * 1.005
        return bb_touched and confirm_red and not_higher


def evaluate(pepe_1h: pd.DataFrame, pepe_15m: pd.DataFrame,
             btc_1h: pd.DataFrame) -> dict | None:

    h  = pepe_1h.iloc[-1]    # son 1H mumu
    m  = pepe_15m.iloc[-1]   # son 15M mumu
    m2 = pepe_15m.iloc[-2]
    b  = btc_1h.iloc[-1]
    price = h["close"]

    for val in [h["rsi"], h["bb_mid"], h["atr"], b["rsi"]]:
        if pd.isna(val):
            return None

    atr    = h["atr"]
    bb_mid = h["bb_mid"]
    bb_low = h["bb_low"]
    bb_up  = h["bb_up"]

    # ── LONG koşulları ──────────────────────────────────────
    long_conds = {
        "1H RSI aşırı satım":    h["rsi"] < RSI_LONG_MAX,
        "15M MACD/EMA teyit":    (m["macd_hist"] > 0) or
                                  (m["ema_fast"] > m["ema_slow"] and
                                   m2["ema_fast"] <= m2["ema_slow"]),
        "Hacim güçlü":           h["vol_ratio"] >= VOL_MIN,
        "BTC engel yok":         b["rsi"] >= BTC_RSI_BLOCK_LONG,
        "Teyit mumu (15M)":      check_confirmed_entry(pepe_15m, "LONG"),
    }

    # ── SHORT koşulları ─────────────────────────────────────
    short_conds = {
        "1H RSI aşırı alım":     h["rsi"] > RSI_SHORT_MIN,
        "15M MACD/EMA teyit":    (m["macd_hist"] < 0) or
                                  (m["ema_fast"] < m["ema_slow"] and
                                   m2["ema_fast"] >= m2["ema_slow"]),
        "Hacim güçlü":           h["vol_ratio"] >= VOL_MIN,
        "BTC de baskıda":        b["rsi"] > BTC_RSI_BOOST_SHORT or
                                  b["ema_fast"] < b["ema_slow"],
        "Teyit mumu (15M)":      check_confirmed_entry(pepe_15m, "SHORT"),
    }

    long_score  = sum(long_conds.values())
    short_score = sum(short_conds.values())

    if long_score < 3 and short_score < 3:
        return None
    if long_score == short_score:
        return None

    sig_type = "LONG" if long_score > short_score else "SHORT"
    conds    = long_conds if sig_type == "LONG" else short_conds
    score    = long_score if sig_type == "LONG" else short_score

    # ── OPTİMİZE SL / TP ──────────────────────────────────
    if sig_type == "LONG":
        sl  = price - atr * SL_ATR_MULT      # 0.5 ATR altı (dar SL)
        tp1 = bb_mid                          # BB orta bant (%63 başarı)
        tp2 = price + atr * TP2_ATR_MULT     # 1.5 ATR (ek hedef)
        tp3 = bb_up                           # BB üst bant (uzun hedef)
    else:
        sl  = price + atr * SL_ATR_MULT
        tp1 = bb_mid
        tp2 = price - atr * TP2_ATR_MULT
        tp3 = bb_low

    risk      = abs(price - sl)
    reward_tp1= abs(tp1 - price)
    rr        = reward_tp1 / risk if risk > 0 else 0

    # ── Minimum R/R filtresi — düşük kaliteli sinyal engelle
    if rr < MIN_RR:
        log.debug("R/R çok düşük (%.1f < %.1f), sinyal atlandı", rr, MIN_RR)
        return None

    # Güç seviyesi
    rsi_ideal = (h["rsi"] < RSI_LONG_IDEAL) if sig_type == "LONG" else (h["rsi"] > RSI_SHORT_IDEAL)
    if score == 5 and rsi_ideal:
        strength = "💎 ÇOK GÜÇLÜ"
    elif score >= 4 and rsi_ideal:
        strength = "⭐⭐⭐ GÜÇLÜ"
    elif score >= 4:
        strength = "⭐⭐ ORTA-GÜÇLÜ"
    else:
        strength = "⭐ ORTA"

    return {
        "type":      sig_type,
        "price":     price,
        "sl":        sl,
        "tp1":       tp1,      # BB orta bant
        "tp2":       tp2,      # 1.5 ATR
        "tp3":       tp3,      # BB üst/alt bant
        "rr":        rr,
        "score":     score,
        "strength":  strength,
        "rsi_1h":    h["rsi"],
        "rsi_btc":   b["rsi"],
        "vol_ratio": h["vol_ratio"],
        "btc_trend": "YUKARI" if b["ema_fast"] > b["ema_slow"] else "ASAGI",
        "conds":     {k: v for k, v in conds.items() if v},
        "atr":       atr,
        "bb_mid":    bb_mid,
        "atr_pct":   atr / price * 100,
    }


# ─────────────────────────────────────────────────────────
#  MESAJ FORMATI
# ─────────────────────────────────────────────────────────

def format_message(sig: dict) -> str:
    is_long  = sig["type"] == "LONG"
    emoji    = "🟢" if is_long else "🔴"
    p        = sig["price"]
    sl, tp1, tp2, tp3 = sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"]

    def pct(target): return (target - p) / p * 100

    cond_lines = "\n".join(f"  ✓ {c}" for c in sig["conds"])
    btc_e = "📈" if sig["btc_trend"] == "YUKARI" else "📉"
    now   = datetime.now().strftime("%d.%m %H:%M")

    return (
        f"{emoji} *PEPE/USDT — {sig['type']}* (v2 optimize)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Giriş:  `{p:.2e}`\n"
        f"🛑 SL:     `{sl:.2e}` ({pct(sl):+.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP1:    `{tp1:.2e}` ({pct(tp1):+.2f}%) ← BB Orta\n"
        f"   _TP1'de %50–60 kapat, SL'yi girişe çek_\n"
        f"🎯 TP2:    `{tp2:.2e}` ({pct(tp2):+.2f}%) ← 1.5 ATR\n"
        f"🎯 TP3:    `{tp3:.2e}` ({pct(tp3):+.2f}%) ← BB Üst\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 R/R (TP1):  `{sig['rr']:.1f}:1`\n"
        f"📡 RSI (1H):   `{sig['rsi_1h']:.0f}`\n"
        f"💹 Hacim:      `{sig['vol_ratio']:.1f}x`\n"
        f"{btc_e} BTC: `{sig['btc_trend']}` RSI=`{sig['rsi_btc']:.0f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Koşullar ({sig['score']}/5):\n{cond_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Güç: {sig['strength']}\n"
        f"⏰ {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Finansal tavsiye değildir._"
    )


def format_status(pepe_1h: pd.DataFrame, btc_1h: pd.DataFrame) -> str:
    h = pepe_1h.iloc[-1]
    b = btc_1h.iloc[-1]
    p = h["close"]
    rsi = h["rsi"]
    bb_mid_dist = (h["bb_mid"] - p) / p * 100

    if rsi < 30:
        durum = "🔵 Çok aşırı satım — LONG yakın"
    elif rsi < 40:
        durum = "🔵 Aşırı satım bölgesi — LONG izle"
    elif rsi > 70:
        durum = "🟠 Çok aşırı alım — SHORT yakın"
    elif rsi > 57:
        durum = "🟠 Aşırı alım bölgesi — SHORT izle"
    else:
        durum = "⚪ Nötr — bekle"

    # BB bantlarına mesafe
    bb_low_dist = (p - h["bb_low"]) / p * 100
    bb_up_dist  = (h["bb_up"] - p) / p * 100

    return (
        f"📊 *PEPE Durum* — {datetime.now().strftime('%H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat:      `{p:.2e}`\n"
        f"📡 RSI (1H):   `{rsi:.0f}`\n"
        f"💹 Hacim:      `{h['vol_ratio']:.1f}x`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 BB Alt'a mesafe:  `{bb_low_dist:+.2f}%`\n"
        f"📈 BB Üst'e mesafe:  `{bb_up_dist:+.2f}%`\n"
        f"🎯 BB Orta (TP1):    `{h['bb_mid']:.2e}` ({bb_mid_dist:+.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{durum}\n"
        f"{'📈' if b['ema_fast']>b['ema_slow'] else '📉'} BTC: "
        f"`{'YUKARI' if b['ema_fast']>b['ema_slow'] else 'ASAGI'}` RSI=`{b['rsi']:.0f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Aktif sinyal yok — taramaya devam"
    )


# ─────────────────────────────────────────────────────────
#  TEKRAR ENGELİ
# ─────────────────────────────────────────────────────────

def is_duplicate(new: dict) -> bool:
    if last_signal["type"] != new["type"]:
        return False
    return abs(new["price"] - last_signal["price"]) / last_signal["price"] < 0.005


# ─────────────────────────────────────────────────────────
#  ANA TARAMA
# ─────────────────────────────────────────────────────────

status_counter = 0

async def scan(bot) -> None:
    global status_counter
    try:
        pepe_1h  = add_indicators(fetch(PEPE_SYMBOL, "1h",  limit=80))
        pepe_15m = add_indicators(fetch(PEPE_SYMBOL, "15m", limit=80))
        btc_1h   = add_indicators(fetch(BTC_SYMBOL,  "1h",  limit=80))

        sig = evaluate(pepe_1h, pepe_15m, btc_1h)
        status_counter += 1

        if sig and not is_duplicate(sig):
            msg = format_message(sig)
            await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            last_signal["type"]  = sig["type"]
            last_signal["price"] = sig["price"]
            log.info("Sinyal: %s @ %.2e RR=%.1f score=%d/5",
                     sig["type"], sig["price"], sig["rr"], sig["score"])
        else:
            h = pepe_1h.iloc[-1]
            log.debug("Sinyal yok — RSI=%.0f vol=%.1fx", h["rsi"], h["vol_ratio"])

        # Saatte bir durum özeti
        if status_counter % 60 == 0:
            status_msg = format_status(pepe_1h, btc_1h)
            await bot.send_message(chat_id=CHAT_ID, text=status_msg, parse_mode="Markdown")

    except Exception as e:
        log.error("Tarama hatası: %s", e)


async def background_loop(bot) -> None:
    while True:
        await scan(bot)
        await asyncio.sleep(CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────
#  KOMUTLAR
# ─────────────────────────────────────────────────────────

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *PEPE Bot v2 — Optimize*\n\n"
        "Değişiklikler:\n"
        "✓ Teyit mumu bekleniyor (erken giriş yok)\n"
        "✓ TP1 = BB Orta Bant (%63 başarı)\n"
        "✓ SL = 0.5 ATR (dar, hızlı keser)\n\n"
        "/durum — Anlık fiyat + BB mesafeleri\n"
        "/son    — Son sinyal\n"
        "/kural  — Sinyal kuralları",
        parse_mode="Markdown",
    )


async def cmd_durum(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pepe_1h = add_indicators(fetch(PEPE_SYMBOL, "1h", limit=80))
        btc_1h  = add_indicators(fetch(BTC_SYMBOL,  "1h", limit=80))
        await update.message.reply_text(
            format_status(pepe_1h, btc_1h), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


async def cmd_son(update, context: ContextTypes.DEFAULT_TYPE):
    if not last_signal["type"]:
        await update.message.reply_text("Henüz sinyal üretilmedi.")
    else:
        await update.message.reply_text(
            f"Son sinyal: *{last_signal['type']}* @ `{last_signal['price']:.2e}`",
            parse_mode="Markdown")


async def cmd_kural(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *PEPE Bot v2 — Sinyal Kuralları*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 *LONG (3/5 koşul):*\n"
        "1. RSI(1H) < 40\n"
        "2. 15M MACD + veya EMA kesişim\n"
        "3. Hacim > 1.5x\n"
        "4. BTC RSI > 35\n"
        "5. Teyit mumu yeşil kapandı ✓\n\n"
        "🔴 *SHORT (3/5 koşul):*\n"
        "1. RSI(1H) > 57\n"
        "2. 15M MACD − veya EMA kesişim\n"
        "3. Hacim > 1.5x\n"
        "4. BTC baskıda\n"
        "5. Teyit mumu kırmızı kapandı ✓\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛑 SL: Giriş ± 0.5 ATR\n"
        "🎯 TP1: BB Orta Bant → %50 kapat\n"
        "🎯 TP2: 1.5 ATR\n"
        "🎯 TP3: BB Üst/Alt Bant\n"
        "⚠️ TP1'de SL'yi girişe çek!",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────
#  BAŞLATMA
# ─────────────────────────────────────────────────────────

async def post_init(application) -> None:
    asyncio.create_task(background_loop(application.bot))
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "✅ *PEPE Bot v2 başlatıldı!*\n"
                "⚡ Optimize edilmiş versiyon:\n"
                "  • Teyit mumu bekleniyor\n"
                "  • TP1 = BB Orta Bant\n"
                "  • SL = 0.5 ATR\n\n"
                "/durum yazarak durumu görebilirsin."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Başlangıç mesajı gönderilemedi: %s", e)


def main():
    app = (ApplicationBuilder()
           .token(TELEGRAM_TOKEN)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("durum",  cmd_durum))
    app.add_handler(CommandHandler("son",    cmd_son))
    app.add_handler(CommandHandler("kural",  cmd_kural))
    log.info("PEPE Bot v2 başlatıldı.")
    app.run_polling()


if __name__ == "__main__":
    main()
