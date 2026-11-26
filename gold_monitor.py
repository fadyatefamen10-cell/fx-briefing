#!/usr/bin/env python3
"""Gold monitor. ANALYSIS ONLY: no trading, no broker, no account connection.
Free data: Yahoo Finance (GC=F gold futures, ~10 min delayed, not spot), Google News RSS,
ForexFactory weekly calendar JSON. Stdlib only. Alerts only when a strong setup or a
high-impact USD event is near. Optional Telegram via TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."""
import json, os, sys, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

UA = {"User-Agent": "Mozilla/5.0"}
NOW = datetime.now(timezone.utc)
CAIRO = timezone(timedelta(hours=2))  # fixed +2; Egypt DST may make it +3
STATE_F, ALERTS_F, LATEST_F = "state.json", "reports/alerts.md", "reports/latest.txt"
FORCE = "--force" in sys.argv

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()

META = {}
def bars(sym, interval, rng):
    d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={rng}"))
    r = d["chart"]["result"][0]; q = r["indicators"]["quote"][0]; out = []; META[sym] = r["meta"]
    for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None not in (o, h, l, c): out.append((t, o, h, l, c))
    return out

def resample(b, hours):
    out, cur, key = [], None, None
    for t, o, h, l, c in b:
        k = t // (3600 * hours)
        if k != key:
            if cur: out.append(tuple(cur))
            cur, key = [t, o, h, l, c], k
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
    if cur: out.append(tuple(cur))
    return out

def ema(v, n):
    k = 2 / (n + 1); e = v[0]; o = []
    for x in v: e = x * k + e * (1 - k); o.append(e)
    return o

def rsi(c, n=14):
    g = l = 0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]; g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]; g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
    return 100 if l == 0 else 100 - 100 / (1 + g / l)

def atr(b, n=14):
    tr = [max(x[2] - x[3], abs(x[2] - b[i][4]), abs(x[3] - b[i][4])) for i, x in enumerate(b[1:])]
    return sum(tr[-n:]) / n

def sign(x, dead=0.0): return 1 if x > dead else -1 if x < -dead else 0

def tf_trend(b):
    c = [x[4] for x in b]
    if len(c) < 55: return 0
    e20, e50 = ema(c, 20)[-1], ema(c, 50)[-1]
    macd = ema(c, 12)[-1] - ema(c, 26)[-1]
    s = (c[-1] > e20) + (e20 > e50) + (macd > 0)
    return {3: 1, 2: 0.5, 1: -0.5, 0: -1}[s]

def swings(b, k=3):
    hi, lo = [], []
    for i in range(k, len(b) - k):
        if b[i][2] == max(x[2] for x in b[i - k:i + k + 1]): hi.append((i, b[i][2]))
        if b[i][3] == min(x[3] for x in b[i - k:i + k + 1]): lo.append((i, b[i][3]))
    return hi, lo

# ---------- schools: each returns (vote in [-1,1], note) ----------
def school_classic(h1):
    c = [x[4] for x in h1]
    e20, e50, e200 = ema(c, 20)[-1], ema(c, 50)[-1], ema(c, 200)[-1]
    r = rsi(c); macd = ema(c, 12)[-1] - ema(c, 26)[-1]
    m = sum(c[-20:]) / 20; sd = (sum((x - m) ** 2 for x in c[-20:]) / 20) ** .5
    pb = (c[-1] - (m - 2 * sd)) / (4 * sd) if sd else .5
    v = 0.3 * sign(c[-1] - e50) + 0.2 * sign(e20 - e50) + 0.2 * sign(macd) + 0.1 * sign(c[-1] - e200)
    if r > 70 or pb > 1: v -= 0.25
    if r < 30 or pb < 0: v += 0.25
    return max(-1, min(1, v)), f"RSI {r:.0f}, MACD {'+' if macd > 0 else '-'}, السعر {'فوق' if c[-1] > e50 else 'تحت'} EMA50"

def levels(h1):
    hi, lo = swings(h1[-200:]); px = h1[-1][4]
    res = sorted(p for _, p in hi if p > px); sup = sorted((p for _, p in lo if p < px), reverse=True)
    return (sup[0] if sup else None), (res[0] if res else None), sup, res

def school_price_action(h1, a):
    px = h1[-1][4]; s, r, _, _ = levels(h1); last = h1[-1]; v = 0; note = "لا إشارة"
    body = abs(last[4] - last[1]) or 1e-9
    if s and px - s < 0.6 * a and (min(last[1], last[4]) - last[3]) > 1.5 * body and last[4] > last[1]:
        v, note = 1, f"رفض من الدعم {s:.1f} (شمعة ذيل سفلي)"
    elif r and r - px < 0.6 * a and (last[2] - max(last[1], last[4])) > 1.5 * body and last[4] < last[1]:
        v, note = -1, f"رفض من المقاومة {r:.1f} (شمعة ذيل علوي)"
    else:
        prev = h1[-12:-1]
        if last[4] > max(x[2] for x in prev) and last[4] > last[1]: v, note = 0.7, "كسر أعلى قمة آخر 12 ساعة"
        elif last[4] < min(x[3] for x in prev) and last[4] < last[1]: v, note = -0.7, "كسر أدنى قاع آخر 12 ساعة"
    return v, note

def school_fib(h1, a):
    w = h1[-120:]; hi = max(range(len(w)), key=lambda i: w[i][2]); lo = min(range(len(w)), key=lambda i: w[i][3])
    H, L = w[hi][2], w[lo][3]; px = w[-1][4]; rng = H - L
    if rng < 2 * a: return 0, "نطاق صغير"
    up = lo < hi  # last leg up: look for buy on pullback
    ret = (H - px) / rng if up else (px - L) / rng
    lv = f"{H - .618 * rng:.1f}-{H - .382 * rng:.1f}" if up else f"{L + .382 * rng:.1f}-{L + .618 * rng:.1f}"
    if .382 <= ret <= .65:
        return (0.8 if up else -0.8), f"تصحيح {ret * 100:.0f}% في منطقة فيبوناتشي ({lv}) ضمن موجة {'صاعدة' if up else 'هابطة'}"
    return 0, f"تصحيح {ret * 100:.0f}% (خارج المنطقة الذهبية)"

def school_momentum(d1, h1):
    c = [x[4] for x in h1]; roc = (c[-1] / c[-13] - 1) * 100
    dc = [x[4] for x in d1]; slope = ema(dc, 20)[-1] - ema(dc, 20)[-6]
    v = 0.5 * sign(roc, 0.05) + 0.5 * sign(slope)
    return v, f"زخم 12 ساعة {roc:+.2f}%، ميل EMA20 اليومي {'صاعد' if slope > 0 else 'هابط'}"

POS = "rise surge gain rally jump climb higher record safe-haven haven soar".split()
NEG = "fall drop slump slide plunge lower weak decline tumble selloff retreat".split()
def headlines():
    qs = ["gold price", "Fed rate inflation dollar", "geopolitical war tensions gold"]; out = []; sc = 0
    for q in qs:
        try:
            u = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
            for it in ET.fromstring(get(u)).findall(".//item")[:5]:
                t = it.findtext("title") or ""; w = t.lower()
                if q.startswith("gold"): sc += sum(x in w for x in POS) - sum(x in w for x in NEG)
                out.append(t)
        except Exception: pass
    return out, sc

def school_sentiment(d1_dx, tnx, sc):
    v = 0; n = []
    if d1_dx: ch = d1_dx[-1][4] / d1_dx[-9][4] - 1 if len(d1_dx) > 9 else 0; v -= 0.4 * sign(ch, 0.002); n.append(f"الدولار DXY {ch * 100:+.2f}% (عكسي للدهب)")
    if tnx: ch = tnx[-1][4] - tnx[-9][4] if len(tnx) > 9 else 0; v -= 0.3 * sign(ch, 0.03); n.append(f"عائد 10 سنين {ch:+.2f}")
    v += 0.3 * sign(sc); n.append(f"عناوين الأخبار {'إيجابية' if sc > 0 else 'سلبية' if sc < 0 else 'محايدة'} للدهب")
    return max(-1, min(1, v)), "، ".join(n)

# ---------- calendar ----------
def calendar():
    try: ev = json.loads(get("https://nfs.faireconomy.media/ff_calendar_thisweek.json"))
    except Exception: return [], False
    out = []
    for e in ev:
        if e.get("country") == "USD" and e.get("impact") == "High":
            try: out.append((datetime.fromisoformat(e["date"]).astimezone(timezone.utc), e["title"], e.get("forecast", ""), e.get("previous", "")))
            except Exception: pass
    return sorted(out), True

def main():
    h1 = bars("GC=F", "1h", "3mo"); m15 = bars("GC=F", "15m", "5d"); d1 = bars("GC=F", "1d", "1y")
    try: dx = bars("DX-Y.NYB", "1d", "1mo")
    except Exception: dx = []
    try: tn = bars("^TNX", "1d", "1mo")
    except Exception: tn = []
    h4 = resample(h1, 4); px = m15[-1][4]
    try: spot = float(json.loads(get("https://api.gold-api.com/price/XAU"))["price"])
    except Exception: spot = None
    mt = META["GC=F"].get("regularMarketTime", NOW.timestamp()); age = int((NOW.timestamp() - mt) / 60)
    lag = int((mt - m15[-1][0]) / 60); stale = lag > 90  # candles must reach the latest quote
    FUT = META['GC=F']['regularMarketPrice']
    basis = (spot - META["GC=F"]["regularMarketPrice"]) if spot else 0.0
    a = atr(h1); heads, sc = headlines(); cal, cal_ok = calendar()
    votes = {"كلاسيك (EMA/RSI/MACD/بولنجر)": school_classic(h1), "حركة السعر (دعم/مقاومة)": school_price_action(h1, a),
             "فيبوناتشي": school_fib(h1, a), "اتجاه وزخم": school_momentum(d1, h1), "أخبار ومشاعر (دولار/عوائد/عناوين)": school_sentiment(dx, tn, sc)}
    tfs = {"15د": tf_trend(m15), "ساعة": tf_trend(h1), "4 ساعات": tf_trend(h4), "يومي": tf_trend(d1)}
    mtf = sum(tfs.values()) / len(tfs); votes["تعدد الأطر الزمنية"] = (mtf, " | ".join(f"{k} {'صاعد' if v > 0 else 'هابط'}" for k, v in tfs.items()))
    total = sum(v for v, _ in votes.values()); side = sign(total)
    near = [e for e in cal if -30 <= (e[0] - NOW).total_seconds() / 60 <= 90]
    s, r, sups, ress = levels(h1)
    strong = (not stale) and abs(total) >= 3.0 and sign(mtf) == side and abs(mtf) >= 0.4 and not near
    setup = None
    if strong:
        if side == 1:
            stop = min(px - 1.2 * a, (s - 0.3 * a) if s else px - 1.5 * a); stop = max(stop, px - 2.5 * a)
            tgt = r if r and (r - px) >= 1.5 * (px - stop) else px + 2 * (px - stop)
        else:
            stop = max(px + 1.2 * a, (r + 0.3 * a) if r else px + 1.5 * a); stop = min(stop, px + 2.5 * a)
            tgt = s if s and (px - s) >= 1.5 * (stop - px) else px - 2 * (stop - px)
        rr = abs(tgt - px) / abs(px - stop)
        if rr >= 1.5: setup = (side, px, stop, tgt, rr)
    # report text
    L = [f"🥇 مراقب الدهب — {NOW.astimezone(CAIRO):%Y-%m-%d %H:%M} القاهرة (توقيت +2)",
         (f"السعر الفوري XAU/USD: {spot:.1f} (حي) | " if spot else "") + f"عقود آجلة GC=F: {FUT:.1f} (متأخر ~{age} د) | الفرق {basis:+.1f} | ATR ساعة {a:.1f}",
         f"دعم قريب {s:.1f}" if s else "دعم: -", f"مقاومة قريبة {r:.1f}" if r else "مقاومة: -"]
    L = [L[0], L[1], f"{L[2]} | {L[3]} (مستويات العقود الآجلة)"]
    if stale: L.append(f"⚠️ شموع ياهو متوقفة قبل آخر سعر بـ {lag} دقيقة: التحليل غير موثوق الآن ولن تصدر توصيات.")
    for k, (v, n) in votes.items(): L.append(f"• {k}: {v:+.1f} ({n})")
    L.append(f"المجموع {total:+.1f} من 6")
    if cal_ok:
        up = [e for e in cal if 0 <= (e[0] - NOW).total_seconds() < 86400]
        for e in up[:3]: L.append(f"📅 خبر قوي: {e[1]} بعد {int((e[0] - NOW).total_seconds() / 60)} دقيقة (توقع {e[2] or '-'} / سابق {e[3] or '-'})")
    if heads: L += [f"📰 {t}" for t in heads[:3]]
    if near: L.append("⛔ خبر عالي التأثير قريب: لا نوصي بدخول جديد حول وقته (تذبذب وسبريد واسع).")
    if setup:
        sd, e_, st, tg, rr = setup; e_, st, tg = e_ + basis, st + basis, tg + basis  # shift to spot
        L.append(f"✅ فرصة قوية: {'شراء' if sd == 1 else 'بيع'} | دخول ~{e_:.1f} | وقف {st:.1f} | هدف {tg:.1f} (على السعر الفوري) | نسبة 1:{rr:.1f}")
        L.append("أبطل الفكرة لو اتقفلت شمعة ساعة عند الوقف. القرار والتنفيذ عندك يدوي.")
    else:
        L.append("لا توجد فرصة قوية الآن: انتظار.")
    L.append("⚠️ تحليل فقط، مش نصيحة مالية ولا ضمان. السعر من عقود آجلة ومتأخر، فتأكد من سعر وسيطك قبل أي قرار.")
    msg = "\n".join(L); print(msg)
    # alert decision
    try: st_ = json.load(open(STATE_F))
    except Exception: st_ = {}
    alerts = []
    if setup:
        sd = setup[0]; last = st_.get("setup")
        if FORCE or not last or last["side"] != sd or (NOW.timestamp() - last["t"]) > 4 * 3600 or abs(last["px"] - px) > 1.5 * a:
            alerts.append("فرصة قوية على الدهب:\n" + msg); st_["setup"] = {"side": sd, "px": px, "t": NOW.timestamp()}
    else:
        st_.pop("setup", None)
    done = set(st_.get("events", []))
    for e in cal:
        mins = (e[0] - NOW).total_seconds() / 60; key = f"{e[0].isoformat()}|{e[1]}"
        if 0 <= mins <= 60 and key not in done:
            alerts.append(f"📅 تنبيه: {e[1]} (USD، تأثير عالي) بعد {int(mins)} دقيقة. توقع {e[2] or '-'} / سابق {e[3] or '-'}. الدهب ممكن يتحرك بحدة وقت النشر."); done.add(key)
    st_["events"] = sorted(done)[-50:]
    hb = NOW.timestamp() - st_.get("status_t", 0) > 6 * 3600
    os.makedirs("reports", exist_ok=True)
    if alerts or hb or FORCE:
        open(LATEST_F, "w").write(msg + "\n"); st_["status_t"] = NOW.timestamp()
    if alerts:
        with open(ALERTS_F, "a") as f:
            for t in alerts: f.write(f"\n## {NOW.isoformat()}\n{t}\n")
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if tok and chat:
            for t in alerts:
                data = urllib.parse.urlencode({"chat_id": chat, "text": t}).encode()
                try: urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20)
                except Exception as ex: print("telegram failed", ex)
    json.dump(st_, open(STATE_F, "w"), ensure_ascii=False)

if __name__ == "__main__": main()
