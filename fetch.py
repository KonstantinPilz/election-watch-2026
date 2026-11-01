#!/usr/bin/env python3
"""Fetch Kalshi + Polymarket prices for the 2026 Senate and House races.

Writes <out>/data.json (read by the page) and <out>/state.json (rolling 7-day
hourly history, read back by the next run so we only backfill once).

Usage: python3 fetch.py [out_dir]
Env:   STATE_URL  URL of the previously published state.json (optional)
Stdlib only, so it runs on a bare GitHub Actions runner.
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

K = "https://api.elections.kalshi.com/trade-api/v2"
G = "https://gamma-api.polymarket.com"
C = "https://clob.polymarket.com"
HDR = {"User-Agent": "election-watch/1.0", "Accept": "application/json"}
OUT = sys.argv[1] if len(sys.argv) > 1 else "site"
STATE_URL = os.environ.get("STATE_URL", "")
NOW = int(time.time())
HOUR, WEEK = 3600, 7 * 86400
NOW_H = NOW // HOUR * HOUR
REDISCOVER_EVERY = 3 * HOUR
# Per-district House races (KP 2026-11-01: House is decided, show control odds only).
HOUSE_RACES = False

STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
ABBR = set(STATES.values())

# Seats not up in 2026 (Electoral-vote.com / Ballotpedia, checked 2026-11-01).
# Democrats' 34 include King (ME) and Sanders (VT), who caucus with them.
SENATE_BASE = {"D": 34, "R": 31}
SPECIALS = {"OH", "FL"}  # 2026 races that are special elections

# Polymarket candidates whose party isn't in the market text.
P_PARTY_OVERRIDES = {
    ("S-AK", "Mary Peltola"): "D",
    ("S-AK", "Sen. Dan Sullivan"): "R",
    ("H-AK-AL", "Bill Hill"): "I",
    ("H-CA-40", "Ken Calvert"): "R",  # R-vs-R top-two race
    ("H-CA-40", "Young Kim"): "R",
}

# Fixed chamber-level markets.
K_FIXED = {
    "senate_control": "CONTROLS-2026",
    "house_control": "CONTROLH-2026",
    "senate_dseats": "KXDSENATESEATS-27",
    "senate_dseats_hi": "KXDSENATESEATSH-27",
    "house_dseats": "KXDHOUSESEATS-27",
}
P_FIXED = {
    "senate_control": "which-party-will-win-the-senate-in-2026",
    "house_control": "which-party-will-win-the-house-in-2026",
    "senate_rseats": "republican-senate-seats-after-the-2026-midterm-elections-927",
    "senate_rseats_lo": "republican-senate-seats-after-the-midterms-low-brackets",
    "house_rseats": "republican-house-seats-after-the-2026-midterm-elections",
    "house_rseats_lo": "republican-house-seats-after-the-2026-midterm-elections-lower-brackets",
}

log = lambda *a: print(*a, file=sys.stderr, flush=True)

# ---------------------------------------------------------------- HTTP
_klock = threading.Lock()
_klast = [0.0]


def http(url, body=None, tries=7, kalshi=False):
    for i in range(tries):
        if kalshi:  # Kalshi's public API rate-limits hard; space calls out
            with _klock:
                wait = _klast[0] + 0.12 - time.time()
                if wait > 0:
                    time.sleep(wait)
                _klast[0] = time.time()
        try:
            hdr = dict(HDR)
            data = None
            if body is not None:
                hdr["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("giving up on " + url)


def kget(path):
    return http(K + path, kalshi=True)


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- prices
def k_price(m):
    """Kalshi: mid of bid/ask when the spread is tight, else last trade."""
    bid, ask, last = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars")), f(m.get("last_price_dollars"))
    if ask > 0 and ask - bid <= 0.10:
        return (bid + ask) / 2
    if last > 0:
        return last
    return (bid + ask) / 2


def p_price(m):
    """Polymarket's displayed price (mid, or last trade when the spread is wide)."""
    try:
        return f(json.loads(m.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError):
        return f(m.get("lastTradePrice"))


def k_party(m, event_ticker):
    suf = m["ticker"][len(event_ticker) + 1:].upper()
    if suf in ("R", "GOP", "REP"):
        return "R"
    if suf in ("D", "DEM"):
        return "D"
    return "I"


def p_party(m, race_id):
    g = (m.get("groupItemTitle") or "").strip()
    q = m.get("question") or ""
    if (race_id, g) in P_PARTY_OVERRIDES:
        return P_PARTY_OVERRIDES[(race_id, g)]
    if re.search(r"\(D\)", g) or re.match(r"Democrat", g):
        return "D"
    if re.search(r"\(R\)", g) or re.match(r"Republican", g):
        return "R"
    if re.search(r"\(I\)", g) or re.search(r"independent", g, re.I):
        return "I"
    if re.search(r"\bthe Democrats? win\b", q) or re.search(r"\bDemocratic Party\b", q):
        return "D"
    if re.search(r"\bthe Republicans? win\b", q) or re.search(r"\bRepublican Party\b", q):
        return "R"
    if re.search(r"\bindependent\b", q, re.I):
        return "I"
    return "O"


def norm(probs):
    tot = sum(probs.values())
    if tot <= 0:
        return {k: 0.0 for k in probs}
    return {k: v / tot for k, v in probs.items()}


def agg(cands):
    probs = {"D": 0.0, "R": 0.0, "I": 0.0, "O": 0.0}
    for c in cands:
        probs[c["party"]] += c["price"]
    return norm(probs)


# ---------------------------------------------------------------- discovery
def discover():
    log("discovering markets ...")
    reg = {"senate": {}, "house": {}, "at": NOW, "house_races": HOUSE_RACES}
    series = kget("/series?category=Elections")["series"]
    sen_s = [s["ticker"] for s in series if re.match(r"^(KX)?SENATE[A-Z]{2}S?$", s["ticker"])]
    hou_s = [s["ticker"] for s in series if re.match(r"^(KX)?HOUSE[A-Z]{2}(\d+|AL)$", s["ticker"])] if HOUSE_RACES else []

    def best(store, rid, rec):
        if rid not in store or rec["vol"] > store[rid]["vol"]:
            store[rid] = rec

    for st in sen_s:
        for ev in kget(f"/events?series_ticker={st}&with_nested_markets=true&status=open").get("events", []):
            m = re.match(r"^(.+?) Senate winner\?$", ev["title"].strip())
            if not m or "-26" not in ev["event_ticker"] or m.group(1) not in STATES:
                continue
            rid = "S-" + STATES[m.group(1)]
            vol = sum(f(x.get("volume_fp")) for x in ev.get("markets", []))
            best(reg["senate"].setdefault(rid, {}), "k", {"event": ev["event_ticker"], "series": st, "vol": vol})
    for st in hou_s:
        for ev in kget(f"/events?series_ticker={st}&with_nested_markets=true&status=open").get("events", []):
            t = ev["title"].strip()
            if "-26" not in ev["event_ticker"]:
                continue
            m = re.match(r"^([A-Z]{2})-(\d+|AL) House winner\?$", t)
            if m:
                rid = f"H-{m.group(1)}-{m.group(2) if m.group(2) == 'AL' else int(m.group(2))}"
            else:
                m = re.match(r"^(.+?) House winner\?$", t)
                if not m or m.group(1) not in STATES:
                    continue
                rid = f"H-{STATES[m.group(1)]}-AL"
            vol = sum(f(x.get("volume_fp")) for x in ev.get("markets", []))
            best(reg["house"].setdefault(rid, {}), "k", {"event": ev["event_ticker"], "series": st, "vol": vol})

    seen = {}
    for tag in ["midterms", "us-house", "house", "2026-midterms", "elections"]:
        off = 0
        while off < 5000:
            page = http(f"{G}/events?tag_slug={tag}&closed=false&limit=100&offset={off}")
            for e in page:
                seen[e["slug"]] = e
            if len(page) < 100:
                break
            off += 100
    for slug, e in seen.items():
        vol = f(e.get("volume"))
        m = re.match(r"^(.+?) Senate Election Winner\s*$", e.get("title", "").strip())
        if m and m.group(1) in STATES and re.match(r"^[a-z-]+-senate-election-winner(-\d+)?$", slug):
            best(reg["senate"].setdefault("S-" + STATES[m.group(1)], {}), "p", {"slug": slug, "vol": vol})
            continue
        m = re.match(r"^([a-z]{2})-(\d\d?|al)-house-election-winner(-individual)?(-\d+)?$", slug) or \
            re.match(r"^which-party-will-win-the-house-race-for-the-([a-z]{2})-(\d\d?|al)-seat$", slug)
        if HOUSE_RACES and m and m.group(1).upper() in ABBR:
            d = m.group(2).upper()
            rid = f"H-{m.group(1).upper()}-{d if d == 'AL' else int(d)}"
            # prefer the party market; per-candidate ("individual") ones are a fallback
            prio = 2e12 if ("house-election-winner" in slug and "individual" not in slug) else (1e12 if "individual" not in slug else 0)
            rec = {"slug": slug, "vol": vol + prio}
            best(reg["house"].setdefault(rid, {}), "p", rec)
    for ch in ("senate", "house"):
        for r in reg[ch].values():
            if "p" in r:
                r["p"]["vol"] %= 1e12  # strip the priority offset
    log(f"  senate races {len(reg['senate'])}, house districts {len(reg['house'])}")
    return reg


# ---------------------------------------------------------------- current prices
def k_markets_by_event(events):
    """Fetch nested markets for many Kalshi events."""
    out = {}
    for ev in events:
        r = kget(f"/events/{ev}?with_nested_markets=true")
        evd = r.get("event", r)
        out[ev] = {"event": evd, "markets": evd.get("markets") or r.get("markets") or []}
    return out


def p_events_by_slug(slugs):
    out = {}

    def one(s):
        r = http(f"{G}/events?slug={s}")
        return s, (r[0] if r else None)

    with cf.ThreadPoolExecutor(12) as ex:
        for s, e in ex.map(one, slugs):
            out[s] = e
    return out


def live_markets(ms):
    return [m for m in ms if m.get("active") and not m.get("closed")]


# ---------------------------------------------------------------- history
def k_history(tickers):
    """Hourly mid (or last) per Kalshi market over the past week."""
    res = {}
    tick = list(tickers)
    for i in range(0, len(tick), 20):
        batch = tick[i:i + 20]
        r = kget(f"/markets/candlesticks?market_tickers={','.join(batch)}&start_ts={NOW - WEEK - HOUR}&end_ts={NOW}&period_interval=60")
        for mk in r.get("markets", []):
            t = mk.get("market_ticker") or mk.get("ticker")
            ser = []
            for c in mk.get("candlesticks", []):
                bid = f((c.get("yes_bid") or {}).get("close_dollars"), None)
                ask = f((c.get("yes_ask") or {}).get("close_dollars"), None)
                last = f((c.get("price") or {}).get("close_dollars"), None)
                if bid is not None and ask is not None and ask > 0 and ask - bid <= 0.10:
                    p = (bid + ask) / 2
                elif last:
                    p = last
                elif bid is not None and ask is not None:
                    p = (bid + ask) / 2
                else:
                    continue
                ser.append((int(c["end_period_ts"]) // HOUR * HOUR, p))
            res[t] = ser
    return res


def p_history(tokens):
    res = {}

    def one(tok):
        try:
            r = http(f"{C}/prices-history?market={tok}&startTs={NOW - WEEK - HOUR}&fidelity=60")
            return tok, [(int(h["t"]) // HOUR * HOUR, f(h["p"])) for h in r.get("history", [])]
        except Exception as e:  # noqa: BLE001
            log("  poly history failed", tok[:10], e)
            return tok, []

    with cf.ThreadPoolExecutor(12) as ex:
        for tok, ser in ex.map(one, tokens):
            res[tok] = ser
    return res


def build_series(cands, hist_by_id):
    """Combine per-candidate hourly prices into normalized party probs per hour."""
    hours = list(range(NOW_H - WEEK, NOW_H, HOUR))
    filled = []
    for c in cands:
        ser = dict(hist_by_id.get(c["id"], []))
        first = next((ser[h] for h in sorted(ser)), c["price"]) if ser else c["price"]
        cur, col = first, []
        for h in hours:
            if h in ser:
                cur = ser[h]
            col.append(cur)
        filled.append(col)
    out = []
    for j, h in enumerate(hours):
        probs = {"D": 0.0, "R": 0.0, "I": 0.0, "O": 0.0}
        for c, col in zip(cands, filled):
            probs[c["party"]] += col[j]
        pr = norm(probs)
        out.append([h, round(pr["D"], 4), round(pr["R"], 4), round(pr["I"] + pr["O"], 4)])
    return out


# ---------------------------------------------------------------- seat buckets
def parse_label(s):
    s = s.strip().replace("–", "-").replace("—", "-").replace(" ", " ")
    pats = [
        (r"^(?:≤|<=)\s*(\d+)$", lambda a: (None, int(a))),
        (r"^<\s*(\d+)$", lambda a: (None, int(a) - 1)),
        (r"^Below\s+(\d+)$", lambda a: (None, int(a) - 1)),
        (r"^(\d+)\s*\+$", lambda a: (int(a), None)),
        (r"^(?:≥|>=)\s*(\d+)$", lambda a: (int(a), None)),
        (r"^Above\s+(\d+)$", lambda a: (int(a) + 1, None)),
        (r"^(\d+)$", lambda a: (int(a), int(a))),
    ]
    for p, fn in pats:
        m = re.match(p, s, re.I)
        if m:
            return fn(m.group(1))
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def buckets_from(markets, label_key, price_fn):
    out = []
    for m in markets:
        rng = parse_label(m.get(label_key) or "")
        if rng is None:
            log("  unparsed bucket label", m.get(label_key))
            continue
        out.append({"lo": rng[0], "hi": rng[1], "p": price_fn(m)})
    tot = sum(b["p"] for b in out) or 1
    for b in out:
        b["p"] /= tot
    return sorted(out, key=lambda b: (b["lo"] if b["lo"] is not None else -1))


def refine(main, ext):
    """Split main's open-ended buckets using a finer 'extension' market."""
    if not ext:
        return main
    res = []
    for b in main:
        if b["lo"] is not None and b["hi"] is not None:
            res.append(b)
            continue
        lo = b["lo"] if b["lo"] is not None else -10**9
        hi = b["hi"] if b["hi"] is not None else 10**9
        inside = [e for e in ext
                  if (e["lo"] if e["lo"] is not None else -10**9) >= lo
                  and (e["hi"] if e["hi"] is not None else 10**9) <= hi]
        tot = sum(e["p"] for e in inside)
        if len(inside) >= 2 and tot > 0:
            res += [{"lo": e["lo"], "hi": e["hi"], "p": b["p"] * e["p"] / tot} for e in inside]
        else:
            res.append(b)
    return sorted(res, key=lambda b: (b["lo"] if b["lo"] is not None else -1))


def flip(buckets, total):
    out = [{"lo": (total - b["hi"]) if b["hi"] is not None else None,
            "hi": (total - b["lo"]) if b["lo"] is not None else None,
            "p": b["p"]} for b in buckets]
    return sorted(out, key=lambda b: (b["lo"] if b["lo"] is not None else -1))


# ---------------------------------------------------------------- main
def load_prev_state():
    for src in (os.path.join(OUT, "state.json"), STATE_URL):
        if not src:
            continue
        try:
            if src.startswith("http"):
                st = http(f"{src}?t={NOW}", tries=2)
            else:
                with open(src) as fh:
                    st = json.load(fh)
            log("loaded previous state from", src)
            return st
        except Exception as e:  # noqa: BLE001
            log("no previous state at", src, e)
    return {}


def main():
    t0 = time.time()
    prev = load_prev_state()
    reg = prev.get("registry")
    if not reg or NOW - reg.get("at", 0) > REDISCOVER_EVERY or reg.get("house_races") != HOUSE_RACES:
        reg = discover()
    prev_series = prev.get("series", {})

    k_events = [r["k"]["event"] for ch in ("senate", "house") for r in reg[ch].values() if "k" in r]
    p_slugs = [r["p"]["slug"] for ch in ("senate", "house") for r in reg[ch].values() if "p" in r]
    log(f"fetching {len(k_events)} Kalshi events, {len(p_slugs)} Polymarket events")
    kd = k_markets_by_event(k_events + list(K_FIXED.values()))
    pd = p_events_by_slug(p_slugs + list(P_FIXED.values()))
    log(f"  fetched in {time.time() - t0:.0f}s")

    races = {"senate": [], "house": []}
    need_k, need_p = {}, {}
    for ch in ("senate", "house"):
        for rid, r in sorted(reg[ch].items()):
            rec = {"id": rid, "st": rid.split("-")[1]}
            if ch == "senate":
                rec["special"] = rec["st"] in SPECIALS
            else:
                rec["district"] = rid[2:]
            if "k" in r and kd.get(r["k"]["event"]):
                ev = r["k"]["event"]
                ms = [m for m in kd[ev]["markets"] if m.get("status") in ("active", "open", None, "")] or kd[ev]["markets"]
                cands = [{"id": m["ticker"], "name": m.get("yes_sub_title") or m["ticker"],
                          "party": k_party(m, ev), "price": round(k_price(m), 4),
                          "bid": f(m.get("yes_bid_dollars")), "ask": f(m.get("yes_ask_dollars"))} for m in ms]
                cands = [c for c in cands if c["price"] > 0 or c["party"] in "DR"]
                rec["k"] = {"event": ev, "url": f"https://kalshi.com/markets/{r['k']['series'].lower()}",
                            "cands": cands, "probs": agg(cands),
                            "vol": round(sum(f(m.get("volume_fp")) for m in ms))}
                need_k[(ch, rid)] = cands
            if "p" in r and pd.get(r["p"]["slug"]):
                e = pd[r["p"]["slug"]]
                ms = live_markets(e.get("markets", []))
                cands = []
                for m in ms:
                    try:
                        tok = json.loads(m.get("clobTokenIds") or "[]")[0]
                    except (ValueError, IndexError):
                        continue
                    cands.append({"id": tok, "mid": m["id"], "name": (m.get("groupItemTitle") or m["question"]).strip(),
                                  "party": p_party(m, rid), "price": round(p_price(m), 4),
                                  "bid": f(m.get("bestBid")), "ask": f(m.get("bestAsk"))})
                for c in cands:
                    if c["party"] == "O" and c["price"] >= 0.02:
                        log(f"  WARN {rid} poly candidate with unknown party: {c['name']} @ {c['price']}")
                rec["p"] = {"slug": r["p"]["slug"], "url": f"https://polymarket.com/event/{r['p']['slug']}",
                            "cands": cands, "probs": agg(cands), "vol": round(f(e.get("volume")))}
                need_p[(ch, rid)] = cands
            if "k" in rec or "p" in rec:
                races[ch].append(rec)

    # --- rolling hourly history: reuse previous state, backfill what's new
    series = {}
    k_back, p_back = set(), set()
    for (ch, rid), cands in need_k.items():
        key, sig = f"{rid}|k", sorted(c["id"] + ":" + c["party"] for c in cands)
        old = prev_series.get(key)
        if not old or old.get("sig") != sig:
            k_back.update(c["id"] for c in cands)
    for (ch, rid), cands in need_p.items():
        key, sig = f"{rid}|p", sorted(c["id"] + ":" + c["party"] for c in cands)
        old = prev_series.get(key)
        if not old or old.get("sig") != sig:
            p_back.update(c["id"] for c in cands if c["price"] >= 0.003)
    log(f"backfilling history: {len(k_back)} Kalshi markets, {len(p_back)} Polymarket tokens")
    kh = k_history(k_back) if k_back else {}
    ph = p_history(p_back) if p_back else {}

    def update(key, cands, hist):
        sig = sorted(c["id"] + ":" + c["party"] for c in cands)
        old = prev_series.get(key)
        if old and old.get("sig") == sig:
            pts = [p for p in old["pts"] if p[0] >= NOW_H - WEEK]
        else:
            pts = build_series(cands, hist)
        pr = agg(cands)
        cur = [NOW_H, round(pr["D"], 4), round(pr["R"], 4), round(pr["I"] + pr["O"], 4)]
        pts = [p for p in pts if p[0] != NOW_H] + [cur]
        series[key] = {"sig": sig, "pts": pts}
        return pts

    for ch in ("senate", "house"):
        for rec in races[ch]:
            minfav = {}
            for plat, hist in (("k", kh), ("p", ph)):
                if plat not in rec:
                    continue
                pts = update(f"{rec['id']}|{plat}", rec[plat]["cands"], hist)
                rec[plat]["weekLowFav"] = round(min(max(p[1:]) for p in pts), 4)
                rec[plat]["hist"] = [[p[0], p[1], p[2]] for p in pts]
                minfav[plat] = rec[plat]["weekLowFav"]
            rec["include"] = any(v < 0.90 for v in minfav.values())

    # --- chamber-level markets
    def kc(ev):
        d = kd.get(ev)
        return {k_party(m, ev): round(k_price(m), 4) for m in (d or {}).get("markets", [])} if d else None

    def pc(slug):
        e = pd.get(slug)
        if not e:
            return None
        out = {}
        for m in live_markets(e["markets"]):
            g = m.get("groupItemTitle") or ""
            if g.startswith("Democrat"):
                out["D"] = round(p_price(m), 4)
            elif g.startswith("Republican"):
                out["R"] = round(p_price(m), 4)
        return out

    def kb(ev):
        d = kd.get(ev)
        return buckets_from(d["markets"], "yes_sub_title", k_price) if d else []

    def pb(slug):
        e = pd.get(slug)
        return buckets_from(live_markets(e["markets"]), "groupItemTitle", p_price) if e else []

    seats = {
        "senate": {
            "kalshi": refine(kb(K_FIXED["senate_dseats"]), kb(K_FIXED["senate_dseats_hi"])),
            "polymarket": flip(refine(pb(P_FIXED["senate_rseats"]), pb(P_FIXED["senate_rseats_lo"])), 100),
        },
        "house": {
            "kalshi": kb(K_FIXED["house_dseats"]),
            "polymarket": flip(refine(pb(P_FIXED["house_rseats"]), pb(P_FIXED["house_rseats_lo"])), 435),
        },
    }
    control = {
        "senate": {"kalshi": kc(K_FIXED["senate_control"]), "polymarket": pc(P_FIXED["senate_control"]),
                   "kalshi_url": "https://kalshi.com/markets/controls",
                   "polymarket_url": f"https://polymarket.com/event/{P_FIXED['senate_control']}"},
        "house": {"kalshi": kc(K_FIXED["house_control"]), "polymarket": pc(P_FIXED["house_control"]),
                  "kalshi_url": "https://kalshi.com/markets/controlh",
                  "polymarket_url": f"https://polymarket.com/event/{P_FIXED['house_control']}"},
    }

    data = {
        "generated": NOW,
        "senate": {"base": SENATE_BASE, "races": races["senate"]},
        "house": {"races": races["house"]},
        "control": control,
        "seats": seats,
    }
    # Only included races carry their weekly history to the page.
    for ch in ("senate", "house"):
        for rec in data[ch]["races"]:
            if not rec["include"]:
                for plat in ("k", "p"):
                    if plat in rec:
                        rec[plat].pop("hist", None)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "data.json"), "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    with open(os.path.join(OUT, "state.json"), "w") as fh:
        json.dump({"registry": reg, "series": series, "generated": NOW}, fh, separators=(",", ":"))
    ns = sum(r["include"] for r in races["senate"])
    nh = sum(r["include"] for r in races["house"])
    log(f"done in {time.time() - t0:.0f}s: senate {len(races['senate'])} races ({ns} shown), "
        f"house {len(races['house'])} districts ({nh} shown)")


if __name__ == "__main__":
    main()
