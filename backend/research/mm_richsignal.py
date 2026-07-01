"""Richer-signal analysis for the final MM probe.

Reads the L2+spot capture (book.jsonl, trades.jsonl, spot.jsonl, meta.json), fetches resolution,
and answers ONE question: does richer real-time info predict adverse maker fills — and improve EV —
beyond the trade-momentum-only model?

Two parts:
  1) SIGNAL DISCRIMINATION (statistically efficient): for every maker-fill event, compute the
     post-fill toxicity (adverse mark-out) and each PRE-fill signal at (t_fill - tau): trade
     momentum, L2 order-book imbalance, near-touch liquidity depletion, spot-market lead. Report
     AUC (how well each predicts adverse fills) per signal per latency. AUC>0.5 = informative.
  2) EV EMULATOR: rest a maker at the touch; cancel a side when its signal says adverse (reaction
     latency tau); compute net EV per market. Compare trade-momentum-only vs each rich signal vs a
     combined signal; paired test across markets.

Millisecond recv timestamps => sub-second tau is genuinely resolvable here (unlike the 1s tape).
Read-only. Usage: python mm_richsignal.py <capture_dir>
"""
import json, os, sys, time, urllib.request, bisect
import statistics as st

CD = sys.argv[1]
MARKOUT = 10.0                    # seconds, toxicity horizon
TAUS = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
W = 1.5                          # signal lookback window (s)


def load(f):
    p = os.path.join(CD, f)
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def resolve(cid_by_token):
    cache = os.path.join(CD, "res.json")
    R = json.load(open(cache)) if os.path.exists(cache) else {}
    for tok, cid in cid_by_token.items():
        if cid in R:
            continue
        try:
            m = json.load(urllib.request.urlopen(urllib.request.Request(
                f"https://clob.polymarket.com/markets/{cid}", headers={"User-Agent": "r"}), timeout=15))
            up = next((t for t in (m.get("tokens") or []) if str(t.get("outcome", "")).lower() == "up"), None)
            R[cid] = 1 if (up and up.get("winner")) else (0 if (up is not None and any(t.get("winner") for t in m.get("tokens", []))) else None)
        except Exception:
            R[cid] = None
    json.dump(R, open(cache, "w"))
    return R


def series(rows, key="t"):
    rows = sorted(rows, key=lambda r: r[key])
    ts = [r[key] for r in rows]
    return ts, rows


def at(ts, rows, t):
    i = bisect.bisect_right(ts, t) - 1
    return rows[i] if i >= 0 else None


def auc(scores, labels):
    """AUC that score ranks label=1 above label=0. scores oriented so higher => more adverse."""
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return None, len(pos), len(neg)
    allv = sorted(set(scores))
    rank = {v: i + 1 for i, v in enumerate(sorted(scores))}
    # Mann-Whitney via average ranks (approx; ties handled by dict overwrite is fine for AUC est)
    import bisect as bs
    sp = sorted(scores)
    def rankof(x): return bs.bisect_left(sp, x) + (bs.bisect_right(sp, x) - bs.bisect_left(sp, x)) / 2 + 0.5
    R = sum(rankof(s) for s in pos)
    u = R - len(pos) * (len(pos) + 1) / 2
    return round(u / (len(pos) * len(neg)), 4), len(pos), len(neg)


def build():
    meta = json.load(open(os.path.join(CD, "meta.json")))
    books = load("book.jsonl"); trades = load("trades.jsonl"); spot = load("spot.jsonl")
    # need conditionId per token for resolution — meta has asset/window; fetch market by slug->cid
    cid_by_token = {}
    for tok, m in meta.items():
        d = None
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(
                f"https://gamma-api.polymarket.com/markets?slug={m['asset']}-updown-5m-{m['window_ts']}",
                headers={"User-Agent": "r"}), timeout=15))
        except Exception:
            pass
        if isinstance(d, list) and d:
            cid_by_token[tok] = d[0].get("conditionId")
    R = resolve(cid_by_token)
    # index book + spot per token/asset
    bytok = {}
    for b in books:
        bytok.setdefault(b["tok"], []).append(b)
    bytrade = {}
    for tr in trades:
        bytrade.setdefault(tr["tok"], []).append(tr)
    spot_by = {}
    for s in spot:
        spot_by.setdefault(s["asset"], []).append(s)
    spot_ser = {a: series(v) for a, v in spot_by.items()}
    return meta, bytok, bytrade, spot_ser, cid_by_token, R


def analyze():
    meta, bytok, bytrade, spot_ser, cid_by_token, R = build()
    print("tokens captured:", len(meta), "| with book:", len(bytok), "| with trades:", len(bytrade))
    # per-asset pooled fill events with signals + toxicity
    from collections import defaultdict
    events = defaultdict(list)      # asset -> list of {tau: {signals}, adverse}
    fills_by_asset = defaultdict(int)
    for tok, m in meta.items():
        a = m["asset"]; cid = cid_by_token.get(tok)
        if tok not in bytok or tok not in bytrade:
            continue
        bt, brows = series(bytok[tok])
        tt, trows = series(bytrade[tok])
        sp_ts, sp_rows = spot_ser.get(a, ([], []))
        for tr in trows:
            tf = tr["t"]; bk = at(bt, brows, tf)
            if not bk:
                continue
            # maker fill: SELL taker hits our Up bid (@bb); BUY taker lifts our Up ask (@ba)
            side = tr["side"]
            if side == "SELL" and tr["p"] <= bk["bb"] + 1e-9:
                fillpx = bk["bb"]; up_side = True     # we bought Up
            elif side == "BUY" and tr["p"] >= bk["ba"] - 1e-9:
                fillpx = bk["ba"]; up_side = False    # we bought Down (sold Up)
            else:
                continue
            bk2 = at(bt, brows, tf + MARKOUT)
            if not bk2:
                continue
            mid2 = (bk2["bb"] + bk2["ba"]) / 2
            # adverse if, for an Up buy, mid fell below fill; for a Down buy (sold up), mid rose
            adverse = 1 if ((mid2 < fillpx) if up_side else (mid2 > fillpx)) else 0
            fills_by_asset[a] += 1
            rec = {"adverse": adverse, "sig": {}, "tok": tok, "tf": tf, "fillpx": fillpx,
                   "up_side": up_side, "cid": cid}
            for tau in TAUS:
                bkL = at(bt, brows, tf - tau); bkW = at(bt, brows, tf - tau - W)
                if not bkL or not bkW:
                    continue
                midL = (bkL["bb"] + bkL["ba"]) / 2; midW = (bkW["bb"] + bkW["ba"]) / 2
                mom = midL - midW                                   # yes-price momentum
                imb = bkL["imb"]                                    # L2 imbalance at lag
                # depletion: bid touch size change (falling support) for up buy
                depl = (bkL["bbsz"] - bkW["bbsz"]) / (bkW["bbsz"] + 1e-9)
                # spot lead momentum
                sL = at(sp_ts, sp_rows, tf - tau); sW = at(sp_ts, sp_rows, tf - tau - W)
                spm = ((sL["p"] - sW["p"]) / sW["p"]) if (sL and sW and sW["p"]) else 0.0
                # orient so HIGHER => more adverse (predict the loss direction)
                o = 1.0 if up_side else -1.0
                rec["sig"][tau] = {"mom": -o * mom, "imb": -o * imb, "depl": -o * depl, "spot": -o * spm}
            events[a].append(rec)
    print("\nfill events per asset:", dict(fills_by_asset))

    print("\n================  SIGNAL DISCRIMINATION — AUC (>0.5 = predicts adverse fills)  ================")
    print("  (pooled fills; adverse-fill base rate shown; AUC per signal at tau=0.25s and 1.0s)")
    sigs = ["mom", "imb", "depl", "spot"]
    for a in ["btc", "eth", "sol", "xrp"]:
        evs = [e for e in events[a] if e["sig"]]
        if len(evs) < 20:
            print(f"  {a.upper()}: only {len(evs)} usable fills — insufficient"); continue
        base = st.mean([e["adverse"] for e in evs])
        print(f"\n  {a.upper()} (n={len(evs)} fills, adverse rate {base:.2f}):")
        for tau in [0.25, 1.0]:
            row = []
            for s in sigs:
                sc = [e["sig"][tau][s] for e in evs if tau in e["sig"]]
                lb = [e["adverse"] for e in evs if tau in e["sig"]]
                a_, np_, nn_ = auc(sc, lb)
                row.append(f"{s}={a_}")
            # combined: sum of z-scored signals
            comb_sc = []; comb_lb = []
            for e in evs:
                if tau not in e["sig"]: continue
                comb_sc.append(sum(e["sig"][tau][s] for s in sigs)); comb_lb.append(e["adverse"])
            ca, _, _ = auc(comb_sc, comb_lb)
            print(f"    tau={tau}s: " + " ".join(row) + f"  COMBINED={ca}")

    # summary verdict on discrimination
    print("\n================  VERDICT INPUT: best single-signal AUC vs trade-momentum  ================")
    for a in ["btc", "eth", "sol", "xrp"]:
        evs = [e for e in events[a] if e["sig"]]
        if len(evs) < 20: continue
        best = {}
        for tau in TAUS:
            for s in sigs:
                sc = [e["sig"][tau][s] for e in evs if tau in e["sig"]]
                lb = [e["adverse"] for e in evs if tau in e["sig"]]
                au, _, _ = auc(sc, lb)
                if au is not None:
                    best.setdefault(s, []).append(au)
        line = ", ".join(f"{s}:max_auc={max(v):.3f}" for s, v in best.items())
        print(f"  {a.upper()}: {line}")
    ev_emulator(events, R)
    json.dump({a: [e for e in events[a]] for a in events}, open(os.path.join(CD, "events.json"), "w"))


def _policy_ev(evs_by_tok, R, signal, tau, theta, *, S=5.0, inv_cap=10.0, h_est=0.005):
    """Replay fills per token; cancel a fill if signal(t-tau) > theta (adverse). Settle at
    resolution. signal='none' => never cancel (baseline worst)."""
    per_mkt = []
    for tok, evs in evs_by_tok.items():
        cid = evs[0]["cid"]; won = R.get(cid)
        if won is None:
            continue
        up = dn = cost = reb = 0.0
        for e in sorted(evs, key=lambda x: x["tf"]):
            if tau not in e["sig"]:
                continue
            if signal != "none" and e["sig"][tau][signal] > theta:
                continue                                    # cancelled in time
            px = e["fillpx"]
            if e["up_side"] and (up - dn) < inv_cap:
                up += S; cost += S * px; reb += 0.2 * 0.07 * S * px * (1 - px)
            elif (not e["up_side"]) and (dn - up) < inv_cap:
                dn += S; cost += S * (1 - px); reb += 0.2 * 0.07 * S * (1 - px) * px
        payout = up * won + dn * (1 - won)
        per_mkt.append(payout - cost + reb)
    return per_mkt


def ev_emulator(events, R):
    print("\n================  EV EMULATOR — settled net EV/market by cancellation signal  ================")
    from collections import defaultdict
    for a in ["btc", "eth", "sol", "xrp"]:
        by_tok = defaultdict(list)
        for e in events[a]:
            if e["sig"]:
                by_tok[e["tok"]].append(e)
        # resolved markets only
        resolved = {t: evs for t, evs in by_tok.items() if R.get(evs[0]["cid"]) is not None}
        if len(resolved) < 4:
            print(f"  {a.upper()}: only {len(resolved)} resolved markets captured — insufficient for EV"); continue
        print(f"\n  {a.upper()} ({len(resolved)} resolved markets):")
        base = None
        for signal in ["none", "mom", "imb", "spot", "combined"]:
            # for combined, wrap signal access
            best_mean = None; best_cfg = None
            for tau in [0.25, 0.5, 1.0]:
                for theta in ([0.0] if signal == "none" else [0.001, 0.003, 0.006]):
                    if signal == "combined":
                        # inline combined replay
                        pm = _policy_ev_combined(resolved, R, tau, theta)
                    else:
                        pm = _policy_ev(resolved, R, signal, tau, theta)
                    if len(pm) < 4:
                        continue
                    m = st.mean(pm)
                    if best_mean is None or m > best_mean:
                        best_mean = m; best_cfg = (tau, theta, pm)
            if best_mean is None:
                continue
            tau, theta, pm = best_cfg
            ci = _boot(pm)
            tag = "(baseline)" if signal == "none" else ""
            print(f"    {signal:9}: net EV/mkt=${best_mean:+.3f} ci95={ci} (tau={tau},th={theta},n={len(pm)}) {tag}")
            if signal == "mom":
                base = pm
            if signal in ("imb", "combined") and base is not None:
                # paired improvement vs trade-momentum baseline
                d = [x - y for x, y in zip(pm, base)]
                if len(d) >= 4:
                    print(f"                -> vs trade-momentum: mean Δ=${st.mean(d):+.3f}/mkt, "
                          f"paired ci95={_boot(d)}, wins {sum(1 for x in d if x>0)}/{len(d)}")


def _policy_ev_combined(evs_by_tok, R, tau, theta, *, S=5.0, inv_cap=10.0):
    per = []
    for tok, evs in evs_by_tok.items():
        cid = evs[0]["cid"]; won = R.get(cid)
        if won is None: continue
        up = dn = cost = reb = 0.0
        for e in sorted(evs, key=lambda x: x["tf"]):
            if tau not in e["sig"]: continue
            comb = sum(e["sig"][tau][s] for s in ("mom", "imb", "spot"))
            if comb > theta: continue
            px = e["fillpx"]
            if e["up_side"] and (up - dn) < inv_cap:
                up += S; cost += S * px; reb += 0.2 * 0.07 * S * px * (1 - px)
            elif (not e["up_side"]) and (dn - up) < inv_cap:
                dn += S; cost += S * (1 - px); reb += 0.2 * 0.07 * S * (1 - px) * px
        per.append(up * won + dn * (1 - won) - cost + reb)
    return per


def _boot(xs, iters=3000, seed=5):
    n = len(xs)
    if n < 4: return None
    s = seed
    def r():
        nonlocal s; s = (1103515245 * s + 12345) & 0x7FFFFFFF; return s / 0x7FFFFFFF
    ms = sorted(sum(xs[int(r() * n) % n] for _ in range(n)) / n for _ in range(iters))
    return [round(ms[int(.025 * iters)], 3), round(ms[int(.975 * iters)], 3)]


if __name__ == "__main__":
    analyze()
