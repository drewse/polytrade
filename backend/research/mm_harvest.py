"""Harvest a historical sample of RESOLVED 5-minute candle markets (BTC/ETH/SOL/XRP) with
their full trade tapes + resolution, for the two-sided maker simulator (Phase 0a).

Read-only public Polymarket APIs. Enumeration is hard (Gamma drops closed candles), so we
crawl prolific traders' activity to collect condition_ids, then pull each market's tape +
on-chain resolution. Caches to mm_dataset_<asset>.json so the simulator can re-run free.
"""
import json, urllib.request, re, time, sys
from collections import defaultdict, Counter

SP = sys.argv[1] if len(sys.argv) > 1 else "."
TARGET_PER_ASSET = 120
WALLET_BUDGET = 60          # max wallet-history pulls
ASSETS = ["btc", "eth", "sol", "xrp"]


def get(url):
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polytrade-research"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"; time.sleep(0.6)
    return {"_err": err}


def win_ts(slug):
    m = re.search(r"-(\d{10})$", slug or ""); return int(m.group(1)) if m else None


def candle(slug):
    m = re.match(r"(btc|eth|sol|xrp)-updown-5m-(\d+)", slug or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


# ---- 1) collect condition_ids per asset by crawling wallets -------------------
cids = defaultdict(dict)        # asset -> {cid: window_ts}
seen_wallets = set()
queue = []

# seed BTC from the two known market makers (already pulled earlier)
for fn in ("w1_04b6", "w2_568b"):
    try:
        d = json.load(open(f"{SP}/{fn}.json"))
        for t in d.get("trades", []):
            a, ts = candle(t.get("slug"))
            if a: cids[a][t["conditionId"]] = ts
    except Exception:
        pass
print("seed from w1/w2:", {a: len(cids[a]) for a in ASSETS}, flush=True)

# seed wallets from each asset's recent market tapes
now = int(time.time()); base = (now // 300) * 300
for a in ASSETS:
    for ts in (base, base - 300, base + 300):
        d = get(f"https://gamma-api.polymarket.com/markets?slug={a}-updown-5m-{ts}")
        if isinstance(d, list) and d:
            cid = d[0].get("conditionId")
            if cid:
                cids[a][cid] = ts
                tape = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500")
                if isinstance(tape, list):
                    for w, _ in Counter(t["proxyWallet"] for t in tape).most_common(8):
                        if w not in seen_wallets:
                            queue.append(w)

# BFS over wallets: pull each wallet's history, harvest candle cids, enqueue co-traders
calls = 0
while queue and calls < WALLET_BUDGET and any(len(cids[a]) < TARGET_PER_ASSET for a in ASSETS):
    w = queue.pop(0)
    if w in seen_wallets: continue
    seen_wallets.add(w); calls += 1
    tr = get(f"https://data-api.polymarket.com/trades?user={w}&limit=500")
    if not isinstance(tr, list): continue
    new_alt = []
    for t in tr:
        a, ts = candle(t.get("slug"))
        if a and t["conditionId"] not in cids[a]:
            cids[a][t["conditionId"]] = ts
            if a in ("eth", "sol", "xrp"): new_alt.append(t["conditionId"])
    # find more alt-active wallets from a couple freshly-found alt markets
    for cid in new_alt[:2]:
        if len([x for x in queue if x not in seen_wallets]) > 20: break
        tape = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=300")
        if isinstance(tape, list):
            for ww, _ in Counter(t["proxyWallet"] for t in tape).most_common(5):
                if ww not in seen_wallets: queue.append(ww)
print("after crawl:", {a: len(cids[a]) for a in ASSETS}, "wallets pulled:", calls, flush=True)

# ---- 2) pull tape + resolution per market, build dataset ----------------------
def yes_px(tr):
    p = float(tr.get("price", 0) or 0)
    return p if tr.get("outcome") == "Up" else round(1 - p, 4)

for a in ASSETS:
    items = sorted(cids[a].items(), key=lambda kv: -(kv[1] or 0))[:TARGET_PER_ASSET]  # most recent first
    out = []
    for cid, ts in items:
        mk = get(f"https://clob.polymarket.com/markets/{cid}")
        if not isinstance(mk, dict): continue
        toks = mk.get("tokens") or []
        up = next((t for t in toks if str(t.get("outcome", "")).lower() == "up"), None)
        if up is None or not any(t.get("winner") for t in toks):
            continue                                # not resolved
        won_up = bool(up.get("winner"))
        tape = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500")
        if not isinstance(tape, list) or len(tape) < 3:
            continue
        prints = []
        for t in tape:
            to = (t.get("timestamp", 0) - ts) if ts else None
            if to is None or to < 0 or to > 360: continue
            prints.append([to, yes_px(t), float(t.get("size", 0) or 0), t.get("side", "")])
        if len(prints) < 3: continue
        prints.sort()
        out.append({"cid": cid, "window_ts": ts, "won_up": won_up,
                    "n_trades": len(prints), "prints": prints})
    json.dump(out, open(f"{SP}/mm_dataset_{a}.json", "w"))
    print(f"{a}: {len(out)} resolved markets cached (median trades/mkt="
          f"{sorted(m['n_trades'] for m in out)[len(out)//2] if out else 0})", flush=True)
print("HARVEST DONE", flush=True)
