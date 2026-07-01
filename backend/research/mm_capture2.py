"""Read-only L2 + spot capture (feature version) for the final MM probe.

Maintains the full Polymarket L2 book per tracked token (from `book` snapshots + `price_change`
deltas) and emits COMPACT, timestamp-aligned rows instead of raw:
  * book.jsonl : per book update (throttled ~150ms/token) — best_bid/ask, touch sizes, +-5c depth,
                 order-book imbalance. (queue changes / liquidity depletion derivable across rows)
  * trades.jsonl: every last_trade_price (trade flow) — price, size, side, venue+recv ts.
  * spot.jsonl : Binance reference trades per asset, downsampled ~200ms — price + recv ts.
  * meta.json  : token -> {asset, window_ts}.

100% read-only (public feeds; sends no orders). Usage: python mm_capture2.py <out_dir> <seconds>
"""
import asyncio, json, time, sys, urllib.request
import websockets

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
DUR = int(sys.argv[2]) if len(sys.argv) > 2 else 2400
ASSETS = ["btc", "eth", "sol", "xrp"]
BINANCE = {"btc": "btcusdt", "eth": "ethusdt", "sol": "solusdt", "xrp": "xrpusdt"}
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
t0 = time.monotonic()
meta = {}
books = {}                                   # token -> {"b":{px:sz}, "a":{px:sz}}
last_book_emit = {}                          # token -> monotonic
last_spot_emit = {}                          # asset -> monotonic
bf = open(f"{OUT}/book.jsonl", "a", buffering=1)
tf = open(f"{OUT}/trades.jsonl", "a", buffering=1)
sf = open(f"{OUT}/spot.jsonl", "a", buffering=1)


def _get(u):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "r"}), timeout=15))
    except Exception:
        return None


def current_tokens():
    now = int(time.time()); base = (now // 300) * 300
    toks = {}
    for a in ASSETS:
        for ts in (base, base + 300):
            d = _get(f"https://gamma-api.polymarket.com/markets?slug={a}-updown-5m-{ts}")
            if isinstance(d, list) and d and not d[0].get("closed"):
                tk = d[0].get("clobTokenIds"); tk = json.loads(tk) if isinstance(tk, str) else tk
                if tk:
                    toks[tk[0]] = {"asset": a, "window_ts": ts}
    return toks


def emit_book(tok):
    b = books.get(tok)
    if not b or not b["b"] or not b["a"]:
        return
    bb = max(b["b"]); ba = min(b["a"])
    if bb >= ba:
        return
    bbsz = b["b"][bb]; basz = b["a"][ba]
    bd = sum(sz for px, sz in b["b"].items() if px >= bb - 0.05)
    ad = sum(sz for px, sz in b["a"].items() if px <= ba + 0.05)
    imb = (bd - ad) / (bd + ad) if (bd + ad) else 0.0
    bf.write(json.dumps({"t": round(time.time(), 3), "tok": tok, "bb": bb, "ba": ba,
                         "bbsz": round(bbsz, 1), "basz": round(basz, 1),
                         "bd5": round(bd, 1), "ad5": round(ad, 1), "imb": round(imb, 4)}) + "\n")


def on_poly(msg):
    try:
        x = json.loads(msg)
    except Exception:
        return
    for m in (x if isinstance(x, list) else [x]):
        et = m.get("event_type")
        if et == "book":
            tok = m.get("asset_id")
            if tok not in meta:
                continue
            books[tok] = {"b": {float(o["price"]): float(o["size"]) for o in m.get("bids", []) if float(o["size"]) > 0},
                          "a": {float(o["price"]): float(o["size"]) for o in m.get("asks", []) if float(o["size"]) > 0}}
            emit_book(tok); last_book_emit[tok] = time.monotonic()
        elif et == "price_change":
            touched = set()
            for pc in m.get("price_changes", []):
                tok = pc.get("asset_id")
                if tok not in meta or tok not in books:
                    continue
                side = "b" if pc.get("side") == "BUY" else "a"
                px = float(pc["price"]); sz = float(pc["size"])
                if sz <= 0:
                    books[tok][side].pop(px, None)
                else:
                    books[tok][side][px] = sz
                touched.add(tok)
            for tok in touched:
                if time.monotonic() - last_book_emit.get(tok, 0) > 0.15:
                    emit_book(tok); last_book_emit[tok] = time.monotonic()
        elif et == "last_trade_price":
            tok = m.get("asset_id")
            if tok not in meta:
                continue
            tf.write(json.dumps({"t": round(time.time(), 3), "tvenue": m.get("timestamp"),
                                 "tok": tok, "p": float(m["price"]), "sz": float(m["size"]),
                                 "side": m.get("side")}) + "\n")


async def poly_task():
    while time.monotonic() - t0 < DUR:
        toks = current_tokens()
        for tk, mt in toks.items():
            meta[tk] = mt
        json.dump(meta, open(f"{OUT}/meta.json", "w"))
        want = set(toks)
        if not want:
            await asyncio.sleep(5); continue
        try:
            async with websockets.connect(POLY_WS, ping_interval=None, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": list(want), "type": "market"}))
                lp = tr = time.monotonic()
                while time.monotonic() - t0 < DUR:
                    if time.monotonic() - lp > 8:
                        try: await ws.send("PING")
                        except Exception: break
                        lp = time.monotonic()
                    if time.monotonic() - tr > 45:
                        if set(current_tokens()) - want:
                            break
                        tr = time.monotonic()
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
                    if msg != "PONG":
                        on_poly(msg)
        except Exception:
            await asyncio.sleep(2)


async def spot_task():
    streams = "/".join(f"{s}@trade" for s in BINANCE.values())
    rev = {v: k for k, v in BINANCE.items()}
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    while time.monotonic() - t0 < DUR:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                while time.monotonic() - t0 < DUR:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
                    d = json.loads(msg); st = d.get("stream", ""); dat = d.get("data", {})
                    a = rev.get(st.split("@")[0])
                    if a and time.monotonic() - last_spot_emit.get(a, 0) > 0.2:
                        sf.write(json.dumps({"t": round(time.time(), 3), "asset": a, "p": float(dat["p"])}) + "\n")
                        last_spot_emit[a] = time.monotonic()
        except Exception:
            await asyncio.sleep(2)


async def main():
    await asyncio.gather(poly_task(), spot_task())
    print("capture done; tokens:", len(meta))


if __name__ == "__main__":
    asyncio.run(main())
