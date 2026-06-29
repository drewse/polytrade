"""BTC 5M Micro-Test V3 Phase 1 — on-chain detector tests: OrderFilled decode,
maker/taker watched-wallet detection, token->market mapping, BUY/SELL + outcome
derivation, dedup + gap-fill, cursor persistence, latency/drift measurement,
gate simulation, verdict, and PAPER-ONLY isolation (no LiveExecution, no bankroll
change, no orders)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import btc5m_onchain_source as oc
from app import btc5m_onchain_models as om
from app import live
from app.models import LiveExecution

PRIMARY = "0x4c9497941333332d29f1c235dd23200f3623ffad"
BACKUP = "0xd9013df863c1ba932780857b020dfdeacedf8e14"
OTHER = "0x1111111111111111111111111111111111111111"
EXCH = oc.DEFAULT_EXCHANGES[0]


def _enable(monkeypatch):
    monkeypatch.setenv("BTC5M_ONCHAIN_ENABLED", "true")
    monkeypatch.setenv("BTC5M_ONCHAIN_PAPER_ONLY", "true")
    monkeypatch.setenv("POLYGON_WS_RPC_URL", "https://polygon-rpc.example/x")
    monkeypatch.setenv("BTC5M_MICRO_TEST_PRIMARY_WALLET", PRIMARY)
    monkeypatch.setenv("BTC5M_MICRO_TEST_BACKUP_WALLETS", BACKUP)
    monkeypatch.setenv("BTC5M_MICRO_TEST_MAX_ENTRY_PRICE", "0.60")
    monkeypatch.setenv("BTC5M_MICRO_TEST_MIN_SECONDS_REMAINING", "30")


def _pad(a):
    return "0x" + a[2:].rjust(64, "0")


def _word(n):
    return format(n, "064x")


def _log(*, maker, taker, maker_asset="0", taker_asset="12345", usd=2_500_000, shares=5_000_000,
         tx="0xabc", log_index="0x1", block="0x10", address=EXCH):
    data = "0x" + _word(int(maker_asset)) + _word(int(taker_asset)) + _word(usd) + _word(shares) + _word(0)
    return {"transactionHash": tx, "logIndex": log_index, "blockNumber": block, "address": address,
            "topics": [oc.ORDERFILLED_TOPIC0, "0x" + "0" * 64, _pad(maker), _pad(taker)], "data": data}


GAMMA = [{"conditionId": "0xcond1", "clobTokenIds": '["12345","67890"]', "outcomes": '["Up","Down"]',
          "question": "Bitcoin Up or Down - 5 minute", "slug": "btc-updown-5m-1",
          "endDate": None, "closed": False}]


def _tmap():
    return oc.build_token_map(GAMMA)


# --- decode + classify ------------------------------------------------------
def test_decode_order_filled():
    dec = oc.decode_order_filled(_log(maker=PRIMARY, taker=OTHER))
    assert dec["maker"] == PRIMARY and dec["taker"] == OTHER
    assert dec["maker_asset_id"] == "0" and dec["taker_asset_id"] == "12345"
    assert dec["maker_amount"] == 2_500_000 and dec["tx_hash"] == "0xabc" and dec["log_index"] == 1


def test_detect_maker_buy():
    dec = oc.decode_order_filled(_log(maker=PRIMARY, taker=OTHER))
    cl = oc.classify_fill(dec, {PRIMARY}, set(oc.DEFAULT_EXCHANGES))
    assert cl["role"] == "maker" and cl["side"] == "buy" and cl["watched"] == PRIMARY
    assert cl["token_id"] == "12345" and cl["price"] == 0.5 and cl["shares"] == 5.0 and cl["usd"] == 2.5


def test_detect_taker_buy():
    # taker gave USDC (takerAssetId=0) -> taker BOUGHT makerAssetId
    dec = oc.decode_order_filled(_log(maker=OTHER, taker=BACKUP, maker_asset="67890", taker_asset="0",
                                      usd=5_000_000, shares=2_000_000))
    cl = oc.classify_fill(dec, {BACKUP}, set(oc.DEFAULT_EXCHANGES))
    assert cl["role"] == "taker" and cl["side"] == "buy" and cl["token_id"] == "67890"
    # taker gave takerAmount USDC (2_000_000=$2) for makerAmount shares (5_000_000=5) -> price 0.4
    assert cl["price"] == 0.4


def test_ignore_non_watched():
    dec = oc.decode_order_filled(_log(maker=OTHER, taker="0x2222222222222222222222222222222222222222"))
    assert oc.classify_fill(dec, {PRIMARY}, set(oc.DEFAULT_EXCHANGES)) is None


def test_ignore_exchange_counterparty():
    dec = oc.decode_order_filled(_log(maker=PRIMARY, taker=EXCH))   # other side is the exchange
    assert oc.classify_fill(dec, {PRIMARY}, set(oc.DEFAULT_EXCHANGES)) is None


def test_token_map_btc_only():
    tm = _tmap()
    assert tm["12345"]["outcome"] == "Up" and tm["12345"]["duration_minutes"] == 5
    assert oc.build_token_map([{"conditionId": "x", "clobTokenIds": '["9"]', "outcomes": '["Yes","No"]',
                                "question": "Will it rain?", "slug": "weather"}]) == {}


# --- process_logs: measurement + dedup + gates ------------------------------
def _proc(db, logs, *, now=None, price=0.50, block_ago_s=3):
    cfg = oc._cfg()
    now = now or datetime.utcnow()
    return oc.process_logs(db, logs, cfg=cfg, tmap=_tmap(), now=now,
                           price_fn=lambda t: price,
                           block_ts_fn=lambda b: now - timedelta(seconds=block_ago_s))


def test_process_creates_measured_signal(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    out = _proc(db, [_log(maker=PRIMARY, taker=OTHER)], price=0.55, block_ago_s=3)
    assert out["signals_created"] == 1
    s = db.scalar(select(om.Btc5mOnchainSignal))
    assert s.side == "buy" and s.direction == "YES" and s.price == 0.5
    assert 2900 <= s.detection_latency_ms <= 3200          # ~3s
    assert s.market_price_at_detection == 0.55 and s.price_drift == 0.05
    assert s.would_pass_gates is True and s.ignored_reason is None


def test_dedup_and_gapfill_no_duplicate(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    log = _log(maker=PRIMARY, taker=OTHER)
    assert _proc(db, [log])["signals_created"] == 1
    out2 = _proc(db, [log])                                 # same tx+log_index re-seen (reconnect replay)
    assert out2["signals_created"] == 0 and out2["deduped"] == 1
    assert db.scalar(select(func.count()).select_from(om.Btc5mOnchainSignal)) == 1


def test_gate_sim_price_above_max_ignored(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    # price 0.75 > 0.60 ceiling -> recorded but ignored (not actionable)
    _proc(db, [_log(maker=PRIMARY, taker=OTHER, usd=3_750_000, shares=5_000_000)])
    s = db.scalar(select(om.Btc5mOnchainSignal))
    assert s.price == 0.75 and s.would_pass_gates is False and "max entry" in s.ignored_reason


def test_non_btc_token_ignored(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    _proc(db, [_log(maker=PRIMARY, taker=OTHER, taker_asset="99999")])   # token not in map
    s = db.scalar(select(om.Btc5mOnchainSignal))
    assert s.ignored_reason == "token not in BTC up/down map"


# --- run_once: cursor + isolation -------------------------------------------
def test_run_once_advances_cursor_and_isolated(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    bank0 = live.get_state(db).bankroll
    now = datetime.utcnow()
    out = oc.run_once(db, now=now,
                      latest_block_fn=lambda cfg: 100,
                      fetch_logs_fn=lambda cfg, a, b: [_log(maker=PRIMARY, taker=OTHER)],
                      fetch_all_logs_fn=lambda cfg, a, b: [_log(maker=PRIMARY, taker=OTHER),
                                                           _log(maker=OTHER, taker="0x2222222222222222222222222222222222222222", tx="0xz")],
                      token_fetch_fn=lambda: GAMMA,
                      price_fn=lambda t: 0.5,
                      block_ts_fn=lambda b: now - timedelta(seconds=2))
    assert out["ran"] is True and out["signals_created"] == 1
    assert oc.get_state(db).last_processed_block == 100
    # second cycle, new block, no new logs -> cursor advances, no dup
    oc.run_once(db, now=now, latest_block_fn=lambda cfg: 101,
                fetch_logs_fn=lambda cfg, a, b: [], fetch_all_logs_fn=lambda cfg, a, b: [],
                token_fetch_fn=lambda: GAMMA)
    assert oc.get_state(db).last_processed_block == 101
    # ISOLATION: no production execution / bankroll change ever
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert live.get_state(db).bankroll == bank0


def test_run_once_disabled_noop(in_memory_db, monkeypatch):
    db = in_memory_db
    monkeypatch.setenv("BTC5M_ONCHAIN_ENABLED", "false")
    out = oc.run_once(db, latest_block_fn=lambda cfg: 1, fetch_logs_fn=lambda cfg, a, b: [])
    assert out["ran"] is False and "false" in out["reason"]
    assert db.scalar(select(func.count()).select_from(om.Btc5mOnchainSignal)) == 0


# --- stats / verdict / status ----------------------------------------------
def test_verdict_insufficient_then_status_paper_only(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    _proc(db, [_log(maker=PRIMARY, taker=OTHER)])
    st = oc.status(db)
    assert st["live_execution"] is False and st["paper_only"] is True
    assert st["stats"]["verdict"] == "insufficient_data"   # <20 signals
    assert PRIMARY in st["watched_wallets"]


def test_latency_metrics_and_verdict(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    now = datetime.utcnow()
    # 22 fast signals (~3s each) on actionable BUYs -> viable verdict
    for i in range(22):
        oc.process_logs(db, [_log(maker=PRIMARY, taker=OTHER, tx=f"0x{i:02x}", log_index="0x0")],
                        cfg=oc._cfg(), tmap=_tmap(), now=now,
                        price_fn=lambda t: 0.5, block_ts_fn=lambda b: now - timedelta(seconds=3))
    s = oc.stats(db)
    assert s["signals"] == 22 and s["measured"] == 22
    assert s["median_latency_s"] is not None and s["median_latency_s"] < 5
    assert s["pct_under_10s"] == 100.0
    assert s["verdict"] == "viable"


# --- read-only diagnostics --------------------------------------------------
def test_process_logs_emits_diagnostics(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    logs = [
        _log(maker=PRIMARY, taker=OTHER, tx="0x1"),                       # watched BTC buy (actionable)
        _log(maker=OTHER, taker="0x2222222222222222222222222222222222222222", tx="0x2"),  # not watched
        _log(maker=PRIMARY, taker=OTHER, taker_asset="99999", tx="0x3"),  # watched, token not in map
    ]
    out = _proc(db, logs)
    d = out["diag"]
    assert d["decoded"] == 3 and d["watched"] == 2 and d["btc_matches"] == 1
    assert d["ignored_by_reason"].get("token not in BTC up/down map") == 1
    assert d["last_watched"] and d["last_btc"]


def _run(db, now, *, all_logs, watched_logs=None, token=GAMMA, block=100):
    return oc.run_once(db, now=now, latest_block_fn=lambda cfg: block,
                       fetch_logs_fn=lambda cfg, a, b: watched_logs if watched_logs is not None else [],
                       fetch_all_logs_fn=lambda cfg, a, b: all_logs,
                       token_fetch_fn=lambda: token, price_fn=lambda t: 0.5,
                       block_ts_fn=lambda b: now - timedelta(seconds=3))


def test_diagnosis_no_watched_trade(in_memory_db, monkeypatch):
    """Chain active (OrderFilled seen) but none from watched wallets."""
    db = in_memory_db; _enable(monkeypatch)
    now = datetime.utcnow()
    _run(db, now, all_logs=[_log(maker=OTHER, taker="0x2222222222222222222222222222222222222222", tx="0xa")],
         watched_logs=[])
    diag = oc.status(db)["diagnosis"]
    assert diag["code"] == "no_watched_trade"
    d = oc.status(db)["diagnostics"]
    assert d["logs_scanned"] >= 1 and d["events_matching_watched"] == 0


def test_diagnosis_rpc_log_issue_when_no_orderfilled(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    now = datetime.utcnow()
    _run(db, now, all_logs=[], watched_logs=[])          # blocks scanned but ZERO OrderFilled
    assert oc.status(db)["diagnosis"]["code"] == "rpc_log_issue"


def test_diagnosis_token_map_issue(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    now = datetime.utcnow()
    # watched wallet traded a token that is NOT in the BTC map
    wl = [_log(maker=PRIMARY, taker=OTHER, taker_asset="99999", tx="0xb")]
    _run(db, now, all_logs=wl, watched_logs=wl)
    assert oc.status(db)["diagnosis"]["code"] == "token_map_issue"


def test_diagnosis_all_ignored_by_gates(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    now = datetime.utcnow()
    # watched BTC buy but price 0.75 > 0.60 ceiling -> ignored by gate
    wl = [_log(maker=PRIMARY, taker=OTHER, usd=3_750_000, shares=5_000_000, tx="0xc")]
    _run(db, now, all_logs=wl, watched_logs=wl)
    diag = oc.status(db)["diagnosis"]
    assert diag["code"] == "all_ignored" and "max entry" in diag["message"]


def test_diagnosis_not_started(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    assert oc.status(db)["diagnosis"]["code"] == "not_started"


def test_diagnostics_accumulate_across_cycles(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    now = datetime.utcnow()
    wl = [_log(maker=PRIMARY, taker=OTHER, tx="0xd")]
    _run(db, now, all_logs=wl, watched_logs=wl, block=100)
    _run(db, now, all_logs=[_log(maker=PRIMARY, taker=OTHER, tx="0xe")],
         watched_logs=[_log(maker=PRIMARY, taker=OTHER, tx="0xe")], block=101)
    d = oc.status(db)["diagnostics"]
    assert d["blocks_scanned"] >= 2 and d["events_matching_watched"] == 2 and d["btc_token_map_matches"] == 2


# --- RPC URL resolution + error reporting (400 fix) -------------------------
def test_resolve_prefers_polygon_rpc_url():
    r = oc._resolve_http_rpc("https://polygon-mainnet.g.alchemy.com/v2/KEY", "wss://x/ws")
    assert r["url"].startswith("https://") and r["source"] == "POLYGON_RPC_URL" and r["error"] is None


def test_resolve_rejects_wss_in_rpc_url():
    r = oc._resolve_http_rpc("wss://polygon-mainnet.g.alchemy.com/v2/KEY", "")
    assert r["url"] is None and "HTTPS" in r["error"]


def test_resolve_converts_alchemy_wss():
    r = oc._resolve_http_rpc("", "wss://polygon-mainnet.g.alchemy.com/v2/KEY")
    assert r["url"] == "https://polygon-mainnet.g.alchemy.com/v2/KEY"
    assert r["converted"] is True and r["scheme"] == "https"


def test_resolve_converts_infura_ws_path():
    r = oc._resolve_http_rpc("", "wss://polygon-mainnet.infura.io/ws/v3/KEY")
    assert r["url"] == "https://polygon-mainnet.infura.io/v3/KEY"   # '/ws' stripped


def test_resolve_none_configured():
    r = oc._resolve_http_rpc("", "")
    assert r["url"] is None and "POLYGON_RPC_URL" in r["error"]


def test_rpc_error_message_names_method_and_status():
    e = oc.OnchainRpcError(method="eth_getLogs", scheme="https", host="h", status=400,
                           body="bad range", hint="check endpoint")
    s = str(e)
    assert "eth_getLogs" in s and "400" in s and "https://h" in s and "bad range" in s


def test_config_error_diagnosis_when_rpc_url_is_wss(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    monkeypatch.setenv("POLYGON_RPC_URL", "wss://polygon-mainnet.g.alchemy.com/v2/KEY")  # wrong scheme
    out = oc.run_once(db)
    assert out["ran"] is False
    diag = oc.status(db)["diagnosis"]
    assert diag["code"] == "rpc_config_issue"
    assert oc.status(db)["diagnostics"]["rpc"]["scheme"] in ("wss", "?")


# --- v2 OrderFilled (CTFExchange current; side + single tokenId) ------------
def _v2log(*, maker, taker, side=0, token="12345", maker_amt=2_500_000, taker_amt=5_000_000,
           fee=0, tx="0xv2", log_index="0x1", block="0x10", address=oc.DEFAULT_EXCHANGES[1]):
    data = "0x" + _word(side) + _word(int(token)) + _word(maker_amt) + _word(taker_amt) + _word(fee) + _word(0) + _word(0)
    return {"transactionHash": tx, "logIndex": log_index, "blockNumber": block, "address": address,
            "topics": [oc.ORDERFILLED_V2, "0x" + "0" * 64, _pad(maker), _pad(taker)], "data": data}


# real production receipt values (tx 0xc49436…, CTFExchange 0xe111…):
REAL_V2_TAKER = "0xdf7930e89a2c47560165331863c31deca0733dcd"
REAL_V2_MAKER = "0x87b5f743808f0d7dfaacd303d329b84788b3cfc2"
REAL_V2_TOKEN = "5021063252940354361659831081453334571034164151689405177567614293621153747592"


def test_decode_v2_real_receipt():
    """Decode the exact OrderFilled log from a live CTFExchange v2 trade."""
    log = _v2log(maker=REAL_V2_MAKER, taker=REAL_V2_TAKER, side=0, token=REAL_V2_TOKEN,
                 maker_amt=847500, taker_amt=3390000, fee=0)
    dec = oc.decode_order_filled(log)
    assert dec["version"] == "v2" and dec["side"] == 0
    assert dec["token_id"] == REAL_V2_TOKEN
    assert dec["maker_amount"] == 847500 and dec["taker_amount"] == 3390000 and dec["fee"] == 0
    assert dec["maker"] == REAL_V2_MAKER and dec["taker"] == REAL_V2_TAKER


def test_v2_classify_taker_sell_and_maker_buy():
    log = _v2log(maker=REAL_V2_MAKER, taker=REAL_V2_TAKER, side=0, token=REAL_V2_TOKEN,
                 maker_amt=847500, taker_amt=3390000)
    dec = oc.decode_order_filled(log)
    # taker is the watched wallet: maker BOUGHT -> taker SOLD. price 0.8475/3.39 = 0.25
    ct = oc.classify_fill(dec, {REAL_V2_TAKER}, set(oc.DEFAULT_EXCHANGES))
    assert ct["role"] == "taker" and ct["side"] == "sell" and ct["price"] == 0.25
    assert ct["shares"] == 3.39 and ct["usd"] == 0.8475
    # maker perspective: BUY
    cm = oc.classify_fill(dec, {REAL_V2_MAKER}, set(oc.DEFAULT_EXCHANGES))
    assert cm["role"] == "maker" and cm["side"] == "buy" and cm["price"] == 0.25


def test_v2_watched_buy_emits_btc_signal(in_memory_db, monkeypatch):
    """Watched wallet BUYS a BTC token via v2 -> actionable paper signal."""
    db = in_memory_db; _enable(monkeypatch)
    # maker=PRIMARY, side=BUY(0), token 12345 in the BTC map, $2.50 for 5 shares -> price 0.5
    log = _v2log(maker=PRIMARY, taker=OTHER, side=0, token="12345", maker_amt=2_500_000, taker_amt=5_000_000)
    out = _proc(db, [log], price=0.5)
    assert out["diag"]["by_signature"] == {"v2": 1}
    s = db.scalar(select(om.Btc5mOnchainSignal))
    assert s.side == "buy" and s.direction == "YES" and s.price == 0.5 and s.shares == 5.0
    assert s.would_pass_gates is True and s.ignored_reason is None


def test_both_versions_decoded_and_counted(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    logs = [_log(maker=PRIMARY, taker=OTHER, tx="0xa"),                       # v1 watched BTC buy
            _v2log(maker=PRIMARY, taker=OTHER, token="67890", tx="0xb")]      # v2 watched BTC buy (other outcome)
    out = _proc(db, logs)
    assert out["diag"]["by_signature"].get("v1") == 1 and out["diag"]["by_signature"].get("v2") == 1
    assert out["diag"]["last_decoded_signature"]


def test_unknown_topic0_counted_not_crash(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    bad = _v2log(maker=PRIMARY, taker=OTHER)
    bad["topics"][0] = "0x" + "ab" * 32                                       # unknown event
    out = _proc(db, [bad])
    assert out["diag"]["unknown_topic0"] == 1 and out["diag"]["decoded"] == 0
    assert db.scalar(select(func.count()).select_from(om.Btc5mOnchainSignal)) == 0


def test_status_exposes_decoding_diagnostics(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    wl = [_v2log(maker=PRIMARY, taker=OTHER, token="12345")]
    oc.run_once(db, now=datetime.utcnow(), latest_block_fn=lambda cfg: 100,
                fetch_logs_fn=lambda cfg, a, b: wl, fetch_all_logs_fn=lambda cfg, a, b: wl,
                token_fetch_fn=lambda: GAMMA, price_fn=lambda t: 0.5,
                block_ts_fn=lambda b: datetime.utcnow() - timedelta(seconds=3))
    dec = oc.status(db)["diagnostics"]["decoding"]
    assert dec["abi_source"].startswith("CTFExchange")
    assert {s["version"] for s in dec["known_signatures"]} == {"v1", "v2"}
    assert dec["decoded_by_signature"].get("v2") == 1


def test_v2_paper_only_no_liveexecution(in_memory_db, monkeypatch):
    db = in_memory_db; _enable(monkeypatch)
    bank0 = live.get_state(db).bankroll
    _proc(db, [_v2log(maker=PRIMARY, taker=OTHER, token="12345")])
    assert db.scalar(select(func.count()).select_from(LiveExecution)) == 0
    assert live.get_state(db).bankroll == bank0


def test_block_chunks_respects_span():
    assert list(oc._block_chunks(0, 4, 10)) == [(0, 4)]
    assert list(oc._block_chunks(100, 129, 10)) == [(100, 109), (110, 119), (120, 129)]
    assert list(oc._block_chunks(100, 105, 3)) == [(100, 102), (103, 105)]


def test_get_logs_chunks_to_free_tier_cap(monkeypatch):
    """Wide ranges must be split into <=max_block_span sub-queries (Alchemy free
    tier caps eth_getLogs at 10 blocks)."""
    monkeypatch.setenv("BTC5M_ONCHAIN_ENABLED", "true")
    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.example/x")
    monkeypatch.setenv("BTC5M_MICRO_TEST_PRIMARY_WALLET", PRIMARY)
    cfg = oc._cfg()
    ranges = []

    def fake_rpc(cfg, method, params):
        p = params[0]
        ranges.append(int(p["toBlock"], 16) - int(p["fromBlock"], 16) + 1)
        return []
    monkeypatch.setattr(oc, "_rpc", fake_rpc)
    oc.rpc_get_logs_all(cfg, 1000, 1045)               # 46 blocks
    assert ranges and all(r <= 10 for r in ranges) and sum(ranges) == 46


def test_run_once_jumps_when_cursor_far_behind(in_memory_db, monkeypatch):
    """A stale cursor far behind the tip self-heals: scan only the last span
    blocks (never a 500-block range that would 400 on the free tier)."""
    db = in_memory_db; _enable(monkeypatch)
    st = oc.get_state(db); st.last_processed_block = 1000; db.commit()   # tip will be 1600 -> 600 behind
    seen_range = {}

    def fl(cfg, a, b):
        seen_range["span"] = b - a + 1
        return []
    oc.run_once(db, now=datetime.utcnow(), latest_block_fn=lambda cfg: 1600,
                fetch_logs_fn=fl, fetch_all_logs_fn=lambda cfg, a, b: [], token_fetch_fn=lambda: GAMMA)
    assert seen_range["span"] <= 10                    # bounded, not 600
    assert oc.get_state(db).last_processed_block == 1600


def test_rpc_error_surfaces_before_not_started(in_memory_db, monkeypatch):
    """A failing scan (blocks=0, error) must read as rpc_log_issue, NOT not_started."""
    db = in_memory_db; _enable(monkeypatch)

    def boom(cfg):
        raise oc.OnchainRpcError(method="eth_blockNumber", scheme="https", host="h", status=400,
                                 body="Must be authenticated!", hint="check key")
    out = oc.run_once(db, latest_block_fn=boom, token_fetch_fn=lambda: GAMMA)
    assert out["ran"] is False
    st = oc.get_state(db)
    assert (st.error_count or 0) >= 1 and "eth_blockNumber" in (st.last_error or "")
    assert oc.status(db)["diagnosis"]["code"] == "rpc_log_issue"   # not 'not_started'
