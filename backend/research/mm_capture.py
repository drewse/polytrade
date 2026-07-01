"""Read-only L2 + spot capture for the final market-making probe.

Records, with precise local receive timestamps (wall + monotonic), for BTC/ETH/SOL/XRP 5-minute
markets simultaneously:
  * Polymarket CLOB market channel: `book` (full L2 snapshot), `price_change` (near-touch deltas =
    order adds/cancels/queue changes), `last_trade_price` (trade flow).  -> file poly.jsonl
  * Binance spot trades for the reference asset (btcusdt/ethusdt/solusdt/xrpusdt).  -> file spot.jsonl
Rolls subscriptions as 5-minute windows advance. Token->(asset,window_ts) map -> meta.json.

100% read-only. Subscribes to public feeds; places/sends NO orders. Nothing but data collection.
Usage: python mm_capture.py <out_dir> <duration_seconds>
"""
import asyncio, json, time, sys, urllib.request
import websockets

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
DUR = int(sys.argv[2]) if len(sys.argv) > 2 else 2700
ASSETS = ["btc", "eth", "sol", "xrp"]
BINANCE = {"btc": "btcusdt", "eth": "ethusdt", "sol": "solusdt", "xrp": "xrpusdt"}
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
t0 = time.monotonic()
meta = {}  # token -> {asset, window_ts}
polyf = open(f"{OUT}/poly.jsonl", "a", buffering=1)
spotf = open(f"{OUT}/spot.jsonl", "a", buffering=1)


def _get(u):
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "r"}), timeout=15))
    except Exception:
        return None


def current_tokens():
    """Up-token id per asset for the current + next 5-min window."""
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


def stamp():
    return {"w": time.time(), "m": time.monotonic()}


async def poly_task():
    """Reconnect whenever the live token set changes (window rollover)."""
    cur = set()
    while time.monotonic() - t0 < DUR:
        toks = current_tokens()
        for tk, m in toks.items():
            meta[tk] = m
        json.dump(meta, open(f"{OUT}/meta.json", "w"))
        want = set(toks)
        if not want:
            await asyncio.sleep(5); continue
        cur = want
        try:
            async with websockets.connect(POLY_WS, ping_interval=None, max_size=None) as ws:
                await ws.send(json.dumps({"assets_ids": list(want), "type": "market"}))
                last_ping = time.monotonic(); last_refresh = time.monotonic()
                while time.monotonic() - t0 < DUR:
                    if time.monotonic() - last_ping > 8:
                        try: await ws.send("PING")
                        except Exception: break
                        last_ping = time.monotonic()
                    if time.monotonic() - last_refresh > 45:      # check for rollover
                        if set(current_tokens()) - cur:
                            break                                 # new window -> reconnect
                        last_refresh = time.monotonic()
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
                    if msg == "PONG":
                        continue
                    polyf.write(json.dumps({"r": stamp(), "d": msg}) + "\n")
        except Exception as e:
            polyf.write(json.dumps({"r": stamp(), "err": repr(e)[:100]}) + "\n")
            await asyncio.sleep(2)


async def spot_task():
    streams = "/".join(f"{s}@trade" for s in BINANCE.values())
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
                    spotf.write(json.dumps({"r": stamp(), "d": msg}) + "\n")
        except Exception as e:
            spotf.write(json.dumps({"r": stamp(), "err": repr(e)[:100]}) + "\n")
            await asyncio.sleep(2)


async def main():
    await asyncio.gather(poly_task(), spot_task())
    json.dump(meta, open(f"{OUT}/meta.json", "w"))
    print("capture done; tokens seen:", len(meta))


if __name__ == "__main__":
    asyncio.run(main())
