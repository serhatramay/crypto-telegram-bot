#!/usr/bin/env python3
"""
Kripto Saatlik Rapor Botu
GitHub Actions cron job olarak her saat başı çalışır.
Binance API'den veri çeker, teknik analiz yapar, Telegram'a rapor gönderir.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from urllib import request, error

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "USDP",
    "EUR", "GBP", "TRY", "BRL", "AEUR",
    "USD1", "U", "PAXG", "WBTC", "WBETH", "STETH", "CBBTC",
    "BFUSD", "PYUSD", "EURI", "IDRT",
}
TOP_N = 10

# ─── HTTP Helper ──────────────────────────────────────────────────────────────
def api_get(url, retries=3):
    """GET isteği gönder, JSON döndür"""
    for i in range(retries):
        try:
            req = request.Request(url, headers={"User-Agent": "CryptoBot/1.0"})
            resp = request.urlopen(req, timeout=15)
            return json.loads(resp.read().decode())
        except Exception as e:
            if i == retries - 1:
                print(f"[API] {url[:80]}... HATA: {e}")
                return None
            time.sleep(1)

# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(message):
    """Telegram bildirim gönder"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Token veya Chat ID eksik!")
        print(message)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }).encode()
        req = request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        if result.get("ok"):
            print("[Telegram] Mesaj gönderildi!")
            return True
        else:
            print(f"[Telegram] API hatası: {result}")
            return False
    except Exception as e:
        print(f"[Telegram] Gönderim hatası: {e}")
        return False

# ─── Binance API ──────────────────────────────────────────────────────────────
def get_top_coins():
    """Hacim sırasına göre top N USDT paritesini getir (stablecoin hariç)"""
    data = api_get("https://api.binance.com/api/v3/ticker/24hr")
    if not data:
        return []

    usdt_pairs = []
    for t in data:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        base = symbol.replace("USDT", "")
        if base in STABLECOINS:
            continue
        try:
            volume_usd = float(t.get("quoteVolume", 0))
            price = float(t.get("lastPrice", 0))
            change_24h = float(t.get("priceChangePercent", 0))
        except (ValueError, TypeError):
            continue
        if volume_usd > 0 and price > 0:
            usdt_pairs.append({
                "symbol": symbol,
                "base": base,
                "price": price,
                "change_24h": change_24h,
                "volume_usd": volume_usd
            })

    usdt_pairs.sort(key=lambda x: x["volume_usd"], reverse=True)
    return usdt_pairs[:TOP_N]


def get_klines(symbol, interval="1h", limit=100):
    """Binance'dan mum verileri çek"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = api_get(url)
    if not data:
        return [], [], []

    closes = []
    volumes = []
    ohlc = []
    for k in data:
        o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
        closes.append(c)
        volumes.append(v)
        ohlc.append({"open": o, "high": h, "low": l, "close": c})
    return closes, volumes, ohlc

# ─── Teknik Analiz ────────────────────────────────────────────────────────────
class TechnicalAnalyzer:
    """Tüm teknik gösterge hesaplamaları"""

    @staticmethod
    def ema_series(prices, period):
        if len(prices) < period:
            return []
        k = 2 / (period + 1)
        ema = [sum(prices[:period]) / period]
        for p in prices[period:]:
            ema.append(p * k + ema[-1] * (1 - k))
        return ema

    @staticmethod
    def sma(prices, period):
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    @staticmethod
    def rsi(prices, period=14):
        if len(prices) < period + 1:
            return 50
        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        recent = deltas[-period:]
        gains = [d for d in recent if d > 0]
        losses = [-d for d in recent if d < 0]
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.0001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(prices, fast=12, slow=26, signal_period=9):
        if len(prices) < slow + signal_period:
            return 0, 0, 0
        ema_f = TechnicalAnalyzer.ema_series(prices, fast)
        ema_s = TechnicalAnalyzer.ema_series(prices, slow)
        offset = len(ema_f) - len(ema_s)
        ema_f = ema_f[offset:]
        macd_line = [f - s for f, s in zip(ema_f, ema_s)]
        if len(macd_line) < signal_period:
            return macd_line[-1] if macd_line else 0, 0, 0
        signal_ema = TechnicalAnalyzer.ema_series(macd_line, signal_period)
        histogram = macd_line[-1] - signal_ema[-1] if signal_ema else 0
        return macd_line[-1], signal_ema[-1] if signal_ema else 0, histogram

    @staticmethod
    def bollinger_bands(prices, period=20, num_std=2):
        if len(prices) < period:
            return None, None, None
        sma = sum(prices[-period:]) / period
        variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
        std = math.sqrt(variance)
        return sma + num_std * std, sma, sma - num_std * std

    @staticmethod
    def ema_crossover(prices, short=9, long=21):
        if len(prices) < long + 2:
            return "neutral"
        ema_s_now = TechnicalAnalyzer.ema_series(prices, short)
        ema_l_now = TechnicalAnalyzer.ema_series(prices, long)
        ema_s_prev = TechnicalAnalyzer.ema_series(prices[:-1], short)
        ema_l_prev = TechnicalAnalyzer.ema_series(prices[:-1], long)
        if not all([ema_s_now, ema_l_now, ema_s_prev, ema_l_prev]):
            return "neutral"
        s_now, l_now = ema_s_now[-1], ema_l_now[-1]
        s_prev, l_prev = ema_s_prev[-1], ema_l_prev[-1]
        if s_prev < l_prev and s_now > l_now:
            return "golden_cross"
        elif s_prev > l_prev and s_now < l_now:
            return "death_cross"
        elif s_now > l_now:
            return "bullish"
        else:
            return "bearish"

    @staticmethod
    def support_resistance(prices, lookback=50):
        if len(prices) < lookback:
            lookback = len(prices)
        if lookback < 5:
            return [], []
        recent = prices[-lookback:]
        supports, resistances = [], []
        for i in range(2, len(recent) - 2):
            if recent[i] <= min(recent[i - 1], recent[i - 2], recent[i + 1], recent[i + 2]):
                supports.append(recent[i])
            if recent[i] >= max(recent[i - 1], recent[i - 2], recent[i + 1], recent[i + 2]):
                resistances.append(recent[i])
        return sorted(supports)[-3:] if supports else [], sorted(resistances)[:3] if resistances else []

    @staticmethod
    def volume_signal(volumes, period=20):
        if len(volumes) < period or not volumes[-1]:
            return "normal", 1.0
        avg = sum(volumes[-period:]) / period
        ratio = volumes[-1] / avg if avg > 0 else 1
        if ratio > 2:
            return "very_high", ratio
        elif ratio > 1.3:
            return "high", ratio
        elif ratio < 0.5:
            return "very_low", ratio
        elif ratio < 0.7:
            return "low", ratio
        return "normal", ratio

# ─── Sinyal Üretme ────────────────────────────────────────────────────────────
def analyze_coin(coin, closes, volumes, ohlc):
    """Tek coin için tam analiz yap, sinyal ve detay döndür"""
    TA = TechnicalAnalyzer
    price = coin["price"]
    change_24h = coin["change_24h"]

    rsi = TA.rsi(closes, 14)
    macd_line, macd_signal, macd_hist = TA.macd(closes)
    bb_upper, bb_mid, bb_lower = TA.bollinger_bands(closes)
    ema_cross = TA.ema_crossover(closes, 9, 21)
    ema50 = TA.ema_series(closes, 50)
    supports, resistances = TA.support_resistance(closes)
    vol_signal, vol_ratio = TA.volume_signal(volumes)

    # === SKOR SİSTEMİ ===
    scores = {}

    # 1. RSI (%15)
    if rsi < 25:
        scores['rsi'] = 90
    elif rsi < 35:
        scores['rsi'] = 60
    elif rsi < 45:
        scores['rsi'] = 25
    elif rsi > 75:
        scores['rsi'] = -90
    elif rsi > 65:
        scores['rsi'] = -60
    elif rsi > 55:
        scores['rsi'] = -25
    else:
        scores['rsi'] = 0

    # 2. MACD (%20)
    if macd_hist > 0 and macd_line > macd_signal:
        scores['macd'] = min(80, macd_hist / (abs(price) * 0.001 + 0.01) * 40)
    elif macd_hist < 0 and macd_line < macd_signal:
        scores['macd'] = max(-80, macd_hist / (abs(price) * 0.001 + 0.01) * 40)
    else:
        scores['macd'] = 0

    # 3. Bollinger Bands (%15)
    if bb_lower and bb_upper and price:
        bb_width = bb_upper - bb_lower
        if bb_width > 0:
            bb_pos = (price - bb_lower) / bb_width
            if bb_pos < 0.1:
                scores['bollinger'] = 80
            elif bb_pos < 0.3:
                scores['bollinger'] = 40
            elif bb_pos > 0.9:
                scores['bollinger'] = -80
            elif bb_pos > 0.7:
                scores['bollinger'] = -40
            else:
                scores['bollinger'] = 0
        else:
            scores['bollinger'] = 0
    else:
        scores['bollinger'] = 0

    # 4. EMA Crossover (%20)
    cross_scores = {
        "golden_cross": 85, "bullish": 30,
        "death_cross": -85, "bearish": -30,
        "neutral": 0
    }
    scores['ema_cross'] = cross_scores.get(ema_cross, 0)

    # 5. Trend - EMA50 (%10)
    if ema50 and len(ema50) > 5:
        trend_pct = (ema50[-1] - ema50[-5]) / ema50[-5] * 100 if ema50[-5] else 0
        scores['trend'] = max(-70, min(70, trend_pct * 30))
    else:
        scores['trend'] = 0

    # 6. Support/Resistance (%10)
    scores['sr'] = 0
    if supports and price:
        nearest_support = min(supports, key=lambda s: abs(s - price))
        dist_pct = (price - nearest_support) / price * 100
        if dist_pct < 1:
            scores['sr'] = 60
        elif dist_pct < 2:
            scores['sr'] = 30
    if resistances and price:
        nearest_resist = min(resistances, key=lambda r: abs(r - price))
        dist_pct = (nearest_resist - price) / price * 100
        if dist_pct < 1:
            scores['sr'] = -60
        elif dist_pct < 2:
            scores['sr'] = -30

    # 7. 24h Momentum (%15)
    if change_24h <= -5:
        scores['momentum'] = -80
    elif change_24h <= -3:
        scores['momentum'] = -50
    elif change_24h <= -1:
        scores['momentum'] = -20
    elif change_24h >= 5:
        scores['momentum'] = 80
    elif change_24h >= 3:
        scores['momentum'] = 50
    elif change_24h >= 1:
        scores['momentum'] = 20
    else:
        scores['momentum'] = 0

    # Volume çarpanı
    vol_multiplier = 1.0
    if vol_signal in ["very_high", "high"]:
        vol_multiplier = 1.3
    elif vol_signal in ["very_low", "low"]:
        vol_multiplier = 0.7

    # === AĞIRLIKLI TOPLAM ===
    weights = {
        'rsi': 0.15,
        'macd': 0.20,
        'bollinger': 0.10,
        'ema_cross': 0.20,
        'trend': 0.10,
        'sr': 0.10,
        'momentum': 0.15,
    }
    total_score = sum(scores.get(k, 0) * w for k, w in weights.items())
    total_score *= vol_multiplier
    total_score = max(-100, min(100, total_score))

    # Gösterge uyumu
    bullish_count = sum(1 for v in scores.values() if v > 10)
    bearish_count = sum(1 for v in scores.values() if v < -10)

    # === SİNYAL KARARI (her zaman LONG veya SHORT) ===
    if total_score >= 0:
        signal = "LONG"
    else:
        signal = "SHORT"

    # === GÜVEN SEVİYESİ ===
    abs_score = abs(total_score)
    if abs_score >= 70:
        confidence = "Çok Yüksek"
    elif abs_score >= 50:
        confidence = "Yüksek"
    elif abs_score >= 30:
        confidence = "Orta"
    else:
        confidence = "Düşük"

    # === HEDEF FİYAT ===
    target = None
    if signal == "LONG":
        targets_above = [r for r in resistances if r > price] if resistances else []
        if targets_above:
            target = min(targets_above)
        elif bb_upper:
            target = bb_upper
    else:  # SHORT
        targets_below = [s for s in supports if s < price] if supports else []
        if targets_below:
            target = max(targets_below)
        elif bb_lower:
            target = bb_lower

    # Hedef yoksa Bollinger band kullan
    if target is None:
        if signal == "LONG" and bb_upper:
            target = bb_upper
        elif signal == "SHORT" and bb_lower:
            target = bb_lower

    # === AÇIKLAMA ===
    reasons = []
    if ema_cross in ("golden_cross", "death_cross"):
        reasons.append("Golden Cross" if ema_cross == "golden_cross" else "Death Cross")
    elif ema_cross in ("bullish", "bearish"):
        reasons.append(f"EMA {'yükseliş' if ema_cross == 'bullish' else 'düşüş'}")
    if rsi < 30:
        reasons.append("RSI aşırı satım")
    elif rsi > 70:
        reasons.append("RSI aşırı alım")
    else:
        reasons.append(f"RSI nötr")
    if macd_hist > 0:
        reasons.append("MACD pozitif")
    elif macd_hist < 0:
        reasons.append("MACD negatif")
    if vol_signal in ("very_high", "high"):
        reasons.append(f"Hacim yüksek (x{vol_ratio:.1f})")

    return {
        "signal": signal,
        "score": round(total_score, 1),
        "confidence": confidence,
        "target": target,
        "rsi": round(rsi, 1),
        "macd_hist": macd_hist,
        "ema_cross": ema_cross,
        "reason": ", ".join(reasons[:3]),
        "supports": supports,
        "resistances": resistances,
    }

# ─── Fiyat Formatlama ─────────────────────────────────────────────────────────
def fmt_price(price):
    """Fiyatı okunabilir formata çevir"""
    if price >= 10000:
        return f"${price:,.0f}"
    elif price >= 100:
        return f"${price:,.1f}"
    elif price >= 1:
        return f"${price:,.2f}"
    elif price >= 0.01:
        return f"${price:.4f}"
    else:
        return f"${price:.6f}"

# ─── Rapor Oluşturma ─────────────────────────────────────────────────────────
def build_report(coins_analysis):
    """Telegram HTML mesajı oluştur"""
    now = datetime.now(timezone.utc)
    time_str = now.strftime("%d %b %Y - %H:%M UTC")

    lines = []
    lines.append("📊 <b>KRİPTO SAATLIK RAPOR</b>")
    lines.append(f"🕐 {time_str}")
    lines.append("")
    lines.append("━━━━━ TOP 10 HACİM ━━━━━")

    longs, shorts = [], []

    for item in coins_analysis:
        coin = item["coin"]
        a = item["analysis"]
        symbol_display = f"{coin['base']}/USDT"
        signal = a["signal"]
        score = a["score"]

        if signal == "LONG":
            emoji = "🟢"
            longs.append(coin["base"])
        else:
            emoji = "🔴"
            shorts.append(coin["base"])

        lines.append("")
        lines.append(f"{emoji} <b>{symbol_display}</b> | <b>{signal}</b>")
        lines.append(f"💰 Fiyat: {fmt_price(coin['price'])} ({coin['change_24h']:+.1f}%)")

        if a["target"]:
            lines.append(f"🎯 Hedef: {fmt_price(a['target'])}")

        lines.append(f"📊 Skor: {score:+.0f} ({a['confidence']})")

        macd_dir = "Pozitif" if a["macd_hist"] > 0 else "Negatif"
        trend_icon = "📈" if signal == "LONG" else "📉"
        lines.append(f"{trend_icon} RSI: {a['rsi']} | MACD: {macd_dir}")
        lines.append(f"💡 {a['reason']}")

    # Özet
    lines.append("")
    lines.append("━━━━━ ÖZET ━━━━━")
    if longs:
        lines.append(f"🟢 LONG: {', '.join(longs)}")
    if shorts:
        lines.append(f"🔴 SHORT: {', '.join(shorts)}")

    lines.append("")
    lines.append("⚠️ <i>Bu rapor bilgi amaçlıdır, yatırım tavsiyesi değildir.</i>")

    return "\n".join(lines)

# ─── Ana Fonksiyon ────────────────────────────────────────────────────────────
def main():
    print("🚀 Kripto rapor botu başlatılıyor...")

    # 1. Top coinleri al
    coins = get_top_coins()
    if not coins:
        print("❌ Coin verisi alınamadı!")
        return

    print(f"📋 {len(coins)} coin bulundu, analiz ediliyor...")

    # 2. Her coin için analiz yap
    results = []
    for coin in coins:
        print(f"  📊 {coin['base']}/USDT analiz ediliyor...")
        closes, volumes, ohlc = get_klines(coin["symbol"], "1h", 200)
        if len(closes) < 30:
            print(f"  ⚠️ {coin['base']}: Yetersiz veri ({len(closes)} mum)")
            continue
        analysis = analyze_coin(coin, closes, volumes, ohlc)
        results.append({"coin": coin, "analysis": analysis})
        time.sleep(0.1)  # Rate limit

    if not results:
        print("❌ Hiçbir coin analiz edilemedi!")
        return

    # 3. Rapor oluştur
    report = build_report(results)
    print(f"\n📝 Rapor oluşturuldu ({len(report)} karakter)")

    # 4. Telegram'a gönder
    success = send_telegram(report)
    if success:
        print("✅ Rapor Telegram'a gönderildi!")
    else:
        print("⚠️ Telegram gönderimi başarısız, rapor konsola yazdırıldı.")


if __name__ == "__main__":
    main()
