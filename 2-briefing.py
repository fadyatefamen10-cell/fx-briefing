#!/usr/bin/env python3
"""Analysis-only daily briefing (gold + forex). No trading, no broker connection.
Data: Yahoo Finance public chart endpoint + Google News RSS. Stdlib only.
Usage: python3 briefing.py [XAUUSD EURUSD GBPUSD ...]
Optional alerts: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
import json, os, sys, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

SYMS = {"XAUUSD": ("GC=F", "الدهب (XAU/USD)", "gold price"),
        "EURUSD": ("EURUSD=X", "اليورو/دولار", "EURUSD euro dollar"),
        "GBPUSD": ("GBPUSD=X", "الاسترليني/دولار", "GBPUSD pound dollar"),
        "USDJPY": ("JPY=X", "دولار/ين", "USDJPY yen dollar"),
        "USDCHF": ("CHF=X", "دولار/فرنك", "USDCHF franc dollar")}
UA = {"User-Agent": "Mozilla/5.0"}

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()

def candles(sym, interval, rng):
    d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={rng}"))
    r = d["chart"]["result"][0]; q = r["indicators"]["quote"][0]
    rows = [(h, l, c) for h, l, c in zip(q["high"], q["low"], q["close"]) if None not in (h, l, c)]
    return rows

def ema(v, n):
    k = 2 / (n + 1); e = v[0]; out = []
    for x in v: e = x * k + e * (1 - k); out.append(e)
    return out

def rsi(c, n=14):
    g = l = 0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]; g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]; g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
    return 100 if l == 0 else 100 - 100 / (1 + g / l)

def atr(rows, n=14):
    tr = [max(h - l, abs(h - rows[i - 1][2]), abs(l - rows[i - 1][2])) for i, (h, l, c) in enumerate(rows) if i]
    return sum(tr[-n:]) / n

def trend(c):
    e20, e50 = ema(c, 20)[-1], ema(c, 50)[-1]
    macd = ema(c, 12)[-1] - ema(c, 26)[-1]
    s = (1 if c[-1] > e20 else -1) + (1 if e20 > e50 else -1) + (1 if macd > 0 else -1)
    return s, macd

POS = "rise surge gain rally jump climb higher strong beat record safe-haven".split()
NEG = "fall drop slump slide plunge lower weak miss decline tumble loss selloff".split()
def news(q):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
    items = ET.fromstring(get(url)).findall(".//item")[:6]
    out, sc = [], 0
    for it in items:
        t = it.findtext("title"); out.append(t)
        w = t.lower(); sc += sum(x in w for x in POS) - sum(x in w for x in NEG)
    return out, sc

def ar_trend(s):
    return {3: "صاعد قوي", 1: "صاعد", -1: "هابط", -3: "هابط قوي"}[s]

def analyse(name):
    sym, ar, q = SYMS[name]
    d = candles(sym, "1d", "6mo"); h1 = candles(sym, "1h", "1mo")
    dc = [x[2] for x in d]; hc = [x[2] for x in h1]
    px = dc[-1]; dt, dm = trend(dc); ht, _ = trend(hc)
    r = rsi(dc); a = atr(d)
    sup = min(x[1] for x in d[-20:]); res = max(x[0] for x in d[-20:])
    try: heads, ns = news(q)
    except Exception: heads, ns = [], 0
    score = dt + ht + (1 if ns > 0 else -1 if ns < 0 else 0)
    if r > 70: score -= 1
    if r < 30: score += 1
    dec = 2 if name in ("EURUSD", "GBPUSD", "USDCHF") else (3 if name == "USDJPY" else 2)
    f = lambda x: f"{x:.{4 if dec == 2 and name != 'XAUUSD' else dec}f}"
    if score >= 3: rec, side = "شراء (ميل صاعد)", 1
    elif score <= -3: rec, side = "بيع (ميل هابط)", -1
    else: rec, side = "انتظار / مفيش أفضلية واضحة", 0
    L = [f"*{ar}* — السعر {f(px)}",
         f"الاتجاه: يومي {ar_trend(dt)} | ساعة {ar_trend(ht)} | RSI {r:.0f} | MACD {'موجب' if dm > 0 else 'سالب'}",
         f"دعم قريب {f(sup)} | مقاومة قريبة {f(res)} | متوسط الحركة اليومية (ATR) {f(a)}",
         f"الأخبار: ميل {'إيجابي' if ns > 0 else 'سلبي' if ns < 0 else 'محايد'} (تقدير بسيط من العناوين)"]
    L += [f"  • {h}" for h in heads[:3]]
    L.append(f"التوصية: {rec} (نتيجة {score:+d} من 7)")
    if side:
        L.append(f"مستويات للدراسة: دخول ~{f(px)} | وقف {f(px - side * 1.5 * a)} | هدف {f(px + side * 2.25 * a)} (نسبة 1:1.5)")
    return "\n".join(L)

def main():
    names = [a.upper() for a in sys.argv[1:]] or ["XAUUSD", "EURUSD", "GBPUSD"]
    cairo = datetime.now(timezone(timedelta(hours=2)))
    parts = [f"📊 ملخص السوق — {cairo:%Y-%m-%d %H:%M} (القاهرة)"]
    for n in names:
        try: parts.append(analyse(n))
        except Exception as e: parts.append(f"{n}: تعذر التحليل ({e})")
    parts.append("⚠️ تحليل فقط وليس نصيحة مالية. القرار والتنفيذ عندك يدوي. مفيش ضمان ربح.")
    msg = "\n\n".join(parts); print(msg)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20)

if __name__ == "__main__": main()
