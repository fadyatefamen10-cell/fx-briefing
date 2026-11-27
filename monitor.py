#!/usr/bin/env python3
"""Gold monitor v2. ANALYSIS ONLY: never trades, no broker, no account connection.
Ranks live setups from 3 strategies (selective score, MACD+EMA200, NY-open ORB flip zone),
logs paper results, writes a daily Arabic analysis. Free data: Yahoo (GC=F futures candles,
~10 min delayed), gold-api.com (live spot), Google News RSS, ForexFactory calendar JSON.
Usage: python3 monitor.py [--daily] [--force] [--asof UNIX_TS]"""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import gold_monitor as g

ARGS = sys.argv[1:]
ASOF = int(ARGS[ARGS.index("--asof") + 1]) if "--asof" in ARGS else None
NOWTS = ASOF or int(datetime.now(timezone.utc).timestamp())
NOW = datetime.fromtimestamp(NOWTS, timezone.utc)
g.NOW = NOW
CAI, NY = ZoneInfo("Africa/Cairo"), ZoneInfo("America/New_York")
STATE_F, ALERTS_F, LATEST_F, PAPER_F = "state.json", "reports/alerts.md", "reports/latest.txt", "reports/paper_log.json"
EVID = {"SEL_A": "اختبار سنتين: نجاح ~65% مع جني جزئي، متوسط +0.34R للصفقة، فرصة كل 1-2 أسبوع",
        "SEL_B": "اختبار سنتين: نجاح ~48%، متوسط +0.4R، فرصة كل 1-2 أسبوع",
        "MACD": "اختبار سنتين: نجاح 46-53% و+0.1R تقريبا (ميزة ضعيفة)، تجريبية",
        "ORB": "اختبار 60 يوم فقط (19 صفقة): غير مثبتة، تجريبية (paper)"}
RANK = {"SEL_A": 3.0, "SEL_B": 2.0, "MACD": 1.5, "ORB": 1.2}

# Tuned thresholds come from the STRATEGY_CONFIG secret (kept out of the public repo);
# the values here are generic textbook defaults.
try: CFG = json.loads(os.environ.get("STRATEGY_CONFIG", "{}") or "{}")
except Exception: CFG = {}
RANK.update(CFG.get("RANK", {}))
SEL_MIN = CFG.get("sel_min_total", 3.0)    # min |schools total| for a selective setup
SEL_A_MIN = CFG.get("sel_a_total", 3.5)   # |total| that upgrades SEL_B to SEL_A
MTF_MIN = CFG.get("mtf_min", 0.4)         # min multi-timeframe agreement
TP_SEL = CFG.get("tp_sel", [1.0, 2.0, 3.0])
TP_MACD = CFG.get("tp_macd", [1.0, 1.5, 2.5])
TP_ORB = CFG.get("tp_orb", [1.0, 1.5, 2.0])

def trunc(b, step):  # keep only bars that closed before NOW
    return [x for x in b if x[0] + step <= NOWTS]

def fetch(sym, iv, rng):
    b = g.bars(sym, iv, rng)
    if ASOF: b = [x for x in b if x[0] <= ASOF]
    return b

def mk(kind, side, entry, stop, mults, inval, extra="", pending=False, key=None):
    r = abs(entry - stop)
    return dict(kind=kind, side=side, entry=entry, stop=stop, tps=[entry + side * m * r for m in mults], mults=mults,
                inval=inval, extra=extra, pending=pending, rr=mults[-1], key=key or f"{kind}|{side}|{round(entry / 5)}", t=NOWTS)

# ---------- strategy 1: selective (v1 score) ----------
def s_selective(h1, m15, d1, dx, tn, sc, near):
    a = g.atr(h1); h4 = g.resample(h1, 4)
    votes = [g.school_classic(h1)[0], g.school_price_action(h1, a)[0], g.school_fib(h1, a)[0], g.school_momentum(d1, h1)[0], g.school_sentiment(dx, tn, sc)[0]]
    tfs = [g.tf_trend(m15), g.tf_trend(h1), g.tf_trend(h4), g.tf_trend(d1)]
    mtf = sum(tfs) / 4; total = sum(votes) + mtf; side = g.sign(total); px = h1[-1][4]
    info = dict(total=total, mtf=mtf, votes=votes, tfs=tfs, a=a)
    if near or abs(total) < SEL_MIN or g.sign(mtf) != side or abs(mtf) < MTF_MIN: return [], info
    s, r, _, _ = g.levels(h1)
    if side == 1:
        stop = min(px - 1.2 * a, (s - 0.3 * a) if s else px - 1.5 * a); stop = max(stop, px - 2.5 * a)
    else:
        stop = max(px + 1.2 * a, (r + 0.3 * a) if r else px + 1.5 * a); stop = min(stop, px + 2.5 * a)
    kind = "SEL_A" if abs(total) >= SEL_A_MIN else "SEL_B"
    st = mk(kind, side, px, stop, TP_SEL, "إغلاق شمعة ساعة عند الوقف أو أبعد، أو انقلاب المجموع للاتجاه المعاكس",
            f"مجموع المدارس {total:+.1f} من 6", key=f"SEL|{side}|{h1[-1][0] // 14400}")
    return [st], info

# ---------- strategy 2: MACD + EMA200 (1h) ----------
def s_macd(h1):
    C = [x[4] for x in h1]; e200 = g.ema(C, 200); m = [a - b for a, b in zip(g.ema(C, 12), g.ema(C, 26))]; sg = g.ema(m, 9)
    i = len(h1) - 1; a = g.atr(h1)
    side = 0
    if m[i] > sg[i] and m[i - 1] <= sg[i - 1] and m[i] < 0 and C[i] > e200[i]: side = 1
    if m[i] < sg[i] and m[i - 1] >= sg[i - 1] and m[i] > 0 and C[i] < e200[i]: side = -1
    if not side or (e200[i] - e200[i - 20]) * side <= 0: return []
    piv = None
    for p in range(i - 2, i - 60, -1):
        if side == 1 and h1[p][3] == min(x[3] for x in h1[p - 2:p + 3]): piv = h1[p][3]; break
        if side == -1 and h1[p][2] == max(x[2] for x in h1[p - 2:p + 3]): piv = h1[p][2]; break
    if piv is None: return []
    stop = piv - side * 2 * a; px = C[i]
    if (px - stop) * side <= 0 or not 0.3 * a <= abs(px - stop) <= 4 * a: return []
    return [mk("MACD", side, px, stop, TP_MACD, "إغلاق شمعة ساعة خلف الوقف، أو كسر متوسط 200 عكس الصفقة",
               "تقاطع الماكد مع المتوسط 200 (فريم ساعة)", key=f"MACD|{side}|{h1[-1][0]}")]

# ---------- strategy 3: NY open range breakout, flip-zone retest (5m) ----------
def s_orb(m5):
    if len(m5) < 260: return []
    nyn = NOW.astimezone(NY)
    if not (nyn.weekday() < 5 and (9, 45) <= (nyn.hour, nyn.minute) <= (12, 30)): return []
    C = [x[4] for x in m5]; e200 = g.ema(C, 200)
    day = nyn.date(); idx = {}
    for i, x in enumerate(m5):
        d = datetime.fromtimestamp(x[0], NY)
        if d.date() == day: idx[(d.hour, d.minute)] = i
    if (9, 30) not in idx or (9, 40) not in idx: return []
    i0 = idx[(9, 30)]; rng = m5[i0:i0 + 3]; hi = max(x[2] for x in rng); lo = min(x[3] for x in rng)
    tr = [max(m5[i][2] - m5[i][3], abs(m5[i][2] - m5[i - 1][4]), abs(m5[i][3] - m5[i - 1][4])) for i in range(1, len(m5))]
    a = sum(tr[i0 + 2 - 14:i0 + 2]) / 14
    if any(x[2] - x[3] > 4 * a for x in m5[i0 - 6:i0]) or hi - lo > 3 * a: return []
    side = 0; k = i0 + 3
    while k < min(i0 + 21, len(m5)):
        if m5[k][4] > hi: side = 1; break
        if m5[k][4] < lo: side = -1; break
        k += 1
    if not side or (side == 1) != (m5[k][4] > e200[k]): return []
    piv = None
    for p in range(k - 1, max(k - 100, 3), -1):
        if side == 1 and m5[p][2] == max(x[2] for x in m5[p - 3:p + 4]) and m5[k][3] - 3 * a < m5[p][2] < m5[k][4]: piv = m5[p][2]; break
        if side == -1 and m5[p][3] == min(x[3] for x in m5[p - 3:p + 4]) and m5[k][4] < m5[p][3] < m5[k][2] + 3 * a: piv = m5[p][3]; break
    if piv is None: return []
    stop = piv - side * 0.8 * a
    for q in range(k + 1, len(m5)):  # touched already?
        if (m5[q][3] <= piv if side == 1 else m5[q][2] >= piv):
            if q < len(m5) - 2: return []  # entry window passed
            return [mk("ORB", side, piv, stop, TP_ORB, "إغلاق شمعة 5 دقائق خلف الوقف", "اختراق نطاق افتتاح نيويورك + إعادة اختبار منطقة التحول (دخول الآن)", key=f"ORB|{day}|{side}")]
    if len(m5) - 1 - k > 24: return []
    return [mk("ORB", side, piv, stop, TP_ORB, "إغلاق شمعة 5 دقائق خلف الوقف", "اختراق نطاق افتتاح نيويورك: أمر معلق عند منطقة التحول (لو السعر رجع لها)", pending=True, key=f"ORB|{day}|{side}")]

# ---------- paper results ----------
def resolve(rec, m5):
    side, entry, stop, tps = rec["side"], rec["entry"], rec["stop"], rec["tps"]; risk = abs(entry - stop)
    legs = [(.4, tps[0]), (.3, tps[1]), (.3, tps[2])]; k = 0; cur = stop; R = 0; rem = 1.0
    filled = not rec.get("pending")
    for x in m5:
        if x[0] < rec["t"]: continue
        if not filled:
            if (x[3] <= entry if side == 1 else x[2] >= entry): filled = True
            else: continue
        if (x[3] <= cur if side == 1 else x[2] >= cur): R += rem * (cur - entry) * side / risk; return round(R, 2)
        while k < 3 and (x[2] >= legs[k][1] if side == 1 else x[3] <= legs[k][1]):
            R += legs[k][0] * abs(legs[k][1] - entry) / risk; rem -= legs[k][0]; k += 1; cur = entry
        if k == 3: return round(R, 2)
    return None

def f1(x): return f"{x:.1f}"
def cai(dt): return dt.astimezone(CAI).strftime("%H:%M")

def fmt_setup(s, basis):
    sd = "شراء" if s["side"] == 1 else "بيع"; sh = lambda v: v + basis
    t = " | ".join(f"هدف{i + 1} {f1(sh(p))}" for i, p in enumerate(s["tps"]))
    ty = "أمر معلق عند" if s["pending"] else "دخول ~"
    return (f"{'⭐ ' if s['kind'] in ('SEL_A', 'SEL_B') else ''}{sd} ({s['kind']}) {ty} {f1(sh(s['entry']))} | وقف {f1(sh(s['stop']))} | {t}\n"
            f"   إدارة: عند هدف1 اقفل 40% وانقل الوقف للدخول، هدف2 30%، هدف3 30%.\n   إبطال: {s['inval']}\n   سبب: {s['extra']}\n   موثوقية: {EVID[s['kind']]}")

def daily(spot, basis, h1, d1, info, cal, heads, dx):
    px = h1[-1][4]; pd = d1[-2] if len(d1) > 1 else d1[-1]; P = (pd[2] + pd[3] + pd[4]) / 3
    s, r, sups, ress = g.levels(h1); t = lambda v: "صاعد" if v > 0 else "هابط" if v < 0 else "محايد"
    L = [f"📅 تحليل الدهب اليومي — {NOW.astimezone(CAI):%Y-%m-%d} (القاهرة)",
         f"السعر الفوري {f1(spot) if spot else '-'} | عقود آجلة {f1(px)} (الفرق {basis:+.1f})",
         f"اليوم السابق: أعلى {f1(pd[2] + basis)} | أدنى {f1(pd[3] + basis)} | إغلاق {f1(pd[4] + basis)}",
         f"نقاط الارتكاز: محور {f1(P + basis)} | مقاومة1 {f1(2 * P - pd[3] + basis)} | دعم1 {f1(2 * P - pd[2] + basis)}",
         f"الاتجاه: 15د {t(info['tfs'][0])} | ساعة {t(info['tfs'][1])} | 4 ساعات {t(info['tfs'][2])} | يومي {t(info['tfs'][3])}",
         f"مجموع المدارس الفنية والأخبار: {info['total']:+.1f} من 6 ({'ميل شراء' if info['total'] > 1.5 else 'ميل بيع' if info['total'] < -1.5 else 'لا اتجاه واضح'})"]
    if sups[:2] or ress[:2]:
        L.append("أقرب دعوم: " + " , ".join(f1(v + basis) for v in sups[:2]) + " | أقرب مقاومات: " + " , ".join(f1(v + basis) for v in sorted(ress)[:2]))
    if r and s: L.append(f"سيناريو: فوق {f1(r + basis)} يزيد احتمال الصعود، وتحت {f1(s + basis)} يزيد احتمال الهبوط. بينهم انتظر.")
    ev = [e for e in cal if 0 <= (e[0] - NOW).total_seconds() < 86400]
    L.append("📅 أخبار قوية اليوم (دولار): " + ("; ".join(f"{e[1]} الساعة {cai(e[0])}" for e in ev) if ev else "لا يوجد"))
    L += [f"📰 {h}" for h in heads[:4]]
    L.append("⚠️ تحليل فقط وليس نصيحة. نسب النجاح من اختبار تاريخي ولا تضمن المستقبل. السعر من عقود آجلة متأخرة ~10 دقائق، تأكد من سعر وسيطك.")
    return "\n".join(L)

def main():
    h1 = trunc(fetch("GC=F", "1h", "3mo"), 3600); m15 = trunc(fetch("GC=F", "15m", "5d"), 900); m5 = trunc(fetch("GC=F", "5m", "5d"), 300)
    d1 = fetch("GC=F", "1d", "1y")
    try: dx = fetch("DX-Y.NYB", "1d", "1mo")
    except Exception: dx = []
    try: tn = fetch("^TNX", "1d", "1mo")
    except Exception: tn = []
    mt = g.META["GC=F"].get("regularMarketTime", NOWTS) if not ASOF else NOWTS
    fut = g.META["GC=F"]["regularMarketPrice"] if not ASOF else h1[-1][4]
    spot = None
    if not ASOF:
        spot = g.td_spot()  # fresher spot from Twelve Data; gold-api.com as fallback
        if not spot:
            try: spot = float(json.loads(g.get("https://api.gold-api.com/price/XAU"))["price"])
            except Exception: spot = None
    basis = (spot - fut) if spot else 0.0
    if not ASOF:  # near-real-time 5m candles from Twelve Data (spot shifted to futures); Yahoo as fallback
        raw5 = g.td_bars("5min", 288)
        if raw5:
            m5td = trunc([(t, o + basis, hh + basis, ll + basis, c + basis) for t, o, hh, ll, c in raw5], 300)
            if m5td and (not m5 or m5td[-1][0] > m5[-1][0] - 600): m5 = m5td
    lag5 = (NOWTS - m5[-1][0]) / 60 if m5 else 9999; lag1 = (mt - h1[-1][0]) / 60
    stale = lag5 > 60 or lag1 > 240
    heads, sc = g.headlines() if not ASOF else ([], 0); cal, cal_ok = g.calendar() if not ASOF else ([], True)
    near = [e for e in cal if -30 <= (e[0] - NOW).total_seconds() / 60 <= 90]
    setups, info = [], dict(total=0, mtf=0, votes=[], tfs=[0, 0, 0, 0], a=0)
    if not stale:
        s1, info = s_selective(h1, m15, d1, dx, tn, sc, near); setups += s1
        if not near: setups += s_macd(h1) + s_orb(m5)
    try: st = json.load(open(STATE_F))
    except Exception: st = {}
    try: paper = json.load(open(PAPER_F))
    except Exception: paper = []
    fam = lambda k: "SEL" if k.startswith("SEL") else k
    openp = {(fam(p["kind"]), p["side"]) for p in paper if p["result"] is None}
    setups = [s for s in setups if (fam(s["kind"]), s["side"]) not in openp or any(p["key"] == s["key"] for p in paper)]  # one open signal per strategy+side
    setups.sort(key=lambda s: -(RANK[s["kind"]] + 0.1 * s["rr"]))
    sent = st.get("sent", {}); alerts = []
    for s in setups:
        if s["key"] not in sent:
            sent[s["key"]] = NOWTS
            alerts.append(f"🔔 فرصة جديدة على الدهب (الأقوى أولا):\n{fmt_setup(s, basis)}")
    if len(alerts) > 1: alerts = ["🔔 فرص جديدة على الدهب، مرتبة من الأقوى:\n" + "\n\n".join(a.split("\n", 1)[1] for a in alerts)]
    done = set(st.get("events", []))
    for e in cal:
        mins = (e[0] - NOW).total_seconds() / 60; key = f"{e[0].isoformat()}|{e[1]}"
        if 0 <= mins <= 60 and key not in done: alerts.append(f"📅 تنبيه: {e[1]} (دولار، تأثير عالي) الساعة {cai(e[0])} بالقاهرة، بعد {int(mins)} دقيقة. توقع {e[2] or '-'} / سابق {e[3] or '-'}. الدهب ممكن يتحرك بحدة."); done.add(key)
    st["events"] = sorted(done)[-50:]; st["sent"] = dict(sorted(sent.items(), key=lambda kv: -kv[1])[:100])
    # paper log
    for s in setups:
        if not any(p["key"] == s["key"] for p in paper):
            paper.append(dict(key=s["key"], kind=s["kind"], side=s["side"], entry=s["entry"], stop=s["stop"], tps=s["tps"], t=s["t"], pending=s["pending"], result=None, iso=NOW.isoformat()))
    for p in paper:
        if p["result"] is None and m5:
            if NOWTS - p["t"] > 3 * 86400: p["result"] = "expired"
            else:
                r = resolve(p, m5)
                if r is not None: p["result"] = r
    paper = paper[-300:]
    fin = [p["result"] for p in paper if isinstance(p["result"], float)]
    # report
    L = [f"🥇 مراقب الدهب v2 — {NOW.astimezone(CAI):%Y-%m-%d %H:%M} القاهرة",
         (f"السعر الفوري {f1(spot)} | " if spot else "") + f"عقود آجلة {f1(fut)} | الفرق {basis:+.1f}"]
    if stale: L.append(f"⚠️ شموع ياهو متأخرة (ساعة {int(lag1)} د، 5د {int(lag5)} د): لا توصيات الآن.")
    if setups: L += ["الفرص الحالية (الأقوى أولا):"] + [fmt_setup(s, basis) for s in setups]
    else: L.append("لا توجد فرصة الآن: انتظار.")
    if near: L.append("⛔ خبر قوي قريب: لا دخول جديد حول وقته.")
    if fin: L.append(f"سجل التجربة (paper): {len(fin)} صفقة، مجموع {sum(fin):+.1f}R")
    msg = "\n".join(L); print(msg)
    os.makedirs("reports", exist_ok=True)
    hb = NOWTS - st.get("status_t", 0) > 6 * 3600
    dkey = NOW.astimezone(CAI).strftime("%Y-%m-%d")
    want_daily = "--daily" in ARGS or (NOW.hour == 6 and st.get("daily") != dkey)
    if want_daily and not stale:
        dtxt = daily(spot, basis, h1, d1, info, cal, heads, dx); st["daily"] = dkey
        open("reports/daily.txt", "w").write(dtxt + "\n"); alerts.append(dtxt)
    if not ASOF and NOW.weekday() < 5:
        if stale and NOWTS - st.get("stale_t", 0) > 4 * 3600:
            alerts.append(f"⚠️ تنبيه بيانات: شموع الدهب المجانية (ياهو) متوقفة من حوالي {int(lag5/60)} ساعة. المراقب شغال لكن مش هيطلع فرص لحد ما البيانات ترجع. السكوت دلوقتي معناه إن مفيش بيانات، مش إن مفيش فرصة.")
            st["stale_t"] = NOWTS; st["stale_on"] = True
        elif not stale and st.get("stale_on"):
            alerts.append("✅ البيانات رجعت، المراقب شغال طبيعي وهيبعت أي فرصة جديدة."); st["stale_on"] = False
    if alerts or hb or "--force" in ARGS:
        open(LATEST_F, "w").write(msg + "\n"); st["status_t"] = NOWTS
    if alerts:
        with open(ALERTS_F, "a") as f:
            for t in alerts: f.write(f"\n## {NOW.isoformat()}\n{t}\n")
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if tok and chat and not ASOF:
            for t in alerts:
                try: urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", urllib.parse.urlencode({"chat_id": chat, "text": t[:4000]}).encode(), timeout=20)
                except Exception as ex: print("telegram failed", ex)
    json.dump(paper, open(PAPER_F, "w"), ensure_ascii=False)
    json.dump(st, open(STATE_F, "w"), ensure_ascii=False)
    return setups

if __name__ == "__main__": main()
