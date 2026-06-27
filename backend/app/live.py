"""
Live-money execution test layer.

Goal: validate REAL Polymarket execution (auth, order placement, fills, slippage,
settlement, bookkeeping, reconciliation) with the smallest possible capital —
NOT to chase returns. Sizing is tiny FIXED DOLLAR (not Kelly): the audit showed
our probability model is worse than market price, so we make no probabilistic
sizing claim.

DEFENSE IN DEPTH (a single misconfig must not place a bad/large trade):
  1. LIVE_TRADING_ENABLED=false by default.
  2. LIVE_EXECUTOR=dry_run by default (simulated fills, full bookkeeping, zero
     capital). Real orders require LIVE_EXECUTOR=polymarket AND a private key.
  3. Hard ABSOLUTE-DOLLAR caps: $2/position, $40 total risk, $4/market, $8/wallet,
     $10 daily-loss stop, $40 total-loss stop, max 10 open.
  4. LIVE_MAX_ORDERS=1 -> place exactly one order, then auto-halt for manual review.
  5. Pre-trade slippage gate (skip if the book moved > LIVE_MAX_SLIPPAGE_PCT).
  6. Idempotency: one order per (strategy, signal).
  7. FAIL CLOSED: any error in the real executor -> reject the order, never retry-
     place. The first $1-$2 order IS the live verification.

This module never stores or logs the private key. It is read from the environment
at order time only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import LiveExecution, LiveSignalDecision, LiveState, Market


class ExecutionRejected(Exception):
    """A pre-trade/venue check refused this order (logged as 'rejected').

    `outcome` records the precise execution outcome for the audit trail
    (unfilled_cancelled | submit_error | cancel_error | ...). `venue_error`
    carries the FULL untruncated venue/PolyApiException text when present."""

    def __init__(self, message: str, *, outcome: str | None = None, venue_error: str | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.venue_error = venue_error


# CLOB v2 SDK (py-clob-client-v2). The v1 py-clob-client was ARCHIVED (May 2026)
# and is non-functional against the live CLOB ('invalid order version'); v2 signs
# orders against the current exchange contracts.
_V2_PKG = "py_clob_client_v2"
_V1_ARCHIVED_PKG = "py_clob_client"


def py_clob_installed() -> bool:
    """Whether the CLOB v2 execution SDK is present in THIS environment."""
    import importlib.util
    return importlib.util.find_spec(_V2_PKG) is not None


def archived_v1_present() -> bool:
    """The archived, non-functional v1 client must NOT be installed for real trading."""
    import importlib.util
    return importlib.util.find_spec(_V1_ARCHIVED_PKG) is not None


def sdk_info() -> dict:
    """SDK package/version + CLOB API mode, for the startup log and status."""
    import importlib.util
    ver = None
    if py_clob_installed():
        try:
            import importlib.metadata as md
            ver = md.version("py-clob-client-v2")
        except Exception:  # noqa: BLE001
            ver = "unknown"
    return {
        "sdk_package": "py-clob-client-v2",
        "sdk_version": ver,
        "clob_api_mode": "v2",
        "collateral": "USDC",                 # v2 uses USDC (not pUSD); verified in config
        "v2_sdk_installed": py_clob_installed(),
        "archived_v1_present": archived_v1_present(),
    }


def _assert_real_sdk() -> None:
    """HARD pre-trade guard for REAL order submission: the v2 SDK must be present
    AND the archived v1 client must be absent. Fail closed otherwise."""
    if not py_clob_installed():
        raise ExecutionRejected("CLOB v2 SDK (py-clob-client-v2) not installed", outcome="sdk_missing")
    if archived_v1_present():
        raise ExecutionRejected(
            "archived py-clob-client (v1) is installed — it is non-functional and must be "
            "removed before real trading (use py-clob-client-v2 only)", outcome="archived_sdk")


# The funded Polymarket deposit/proxy wallet (the order MAKER). Verified on-chain:
# it is a Polymarket proxy whose owner() is the signer EOA and which holds the
# funded USDC. Baked as the default because Railway repeatedly failed to apply the
# POLYMARKET_FUNDER env var; the env var STILL overrides this when present. For any
# other deployment, set POLYMARKET_FUNDER (or change this back to None).
_DEFAULT_FUNDER = "0x4Ab19f0662B24841F58b372Cf93A907e37d99116"


def _configured_funder() -> str | None:
    """The maker/funder address: env override, else the verified proxy default."""
    return os.getenv("POLYMARKET_FUNDER") or os.getenv("RELAYER_API_KEY_ADDRESS") or _DEFAULT_FUNDER


def _manual_l2_creds_present() -> bool:
    """Whether all three L2 API creds are configured (so we use the account's own
    creds instead of deriving from the key). Returns a BOOLEAN only — the secret
    and passphrase values are never read into any output/log."""
    return all(os.getenv(k) for k in
               ("POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"))


def wallet_check() -> dict:
    """Pre-flight wallet configuration diagnostic. Derives the EOA from the
    private key and compares it to the configured funder, so we know whether the
    signature_type is correct BEFORE the first live order. Exposes ONLY public
    addresses + validation results — never the private key.

    sig types: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE.
      * EOA wallet  (funder == signer EOA): correct sig_type is 0.
      * Proxy wallet (funder != signer EOA): correct sig_type is 1 or 2; which one
        cannot be derived from the address alone (no local proxy-address
        derivation in py-clob-client) — verify against how the account was made.
    """
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    configured_funder = _configured_funder()
    current_sig = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

    derived = None
    note = None
    if key:
        try:
            from eth_account import Account
            derived = Account.from_key(key).address       # public address only
        except Exception as exc:  # noqa: BLE001
            note = f"could not derive EOA: {exc}"

    if derived is None:
        return {"derived_eoa": None, "configured_funder": configured_funder,
                "addresses_match": None, "recommended_signature_type": None,
                "current_signature_type": current_sig, "configuration_valid": False,
                "note": note or "POLYMARKET_PRIVATE_KEY not set — cannot validate"}

    deoa = derived.lower()
    cfun = configured_funder.lower() if configured_funder else None

    if cfun is None or cfun == deoa:
        # EOA wallet: funder equals the signer (or unset -> defaults to signer).
        valid = current_sig == 0
        return {
            "derived_eoa": derived, "configured_funder": configured_funder or derived,
            "addresses_match": True, "recommended_signature_type": 0,
            "current_signature_type": current_sig, "configuration_valid": valid,
            "note": (None if valid else
                     "EOA wallet (funder == signer) but signature_type != 0 — set "
                     "POLYMARKET_SIGNATURE_TYPE=0.") if configured_funder else
                    ("EOA assumed (no funder configured). If your USDC is in a Polymarket "
                     "proxy, set RELAYER_API_KEY_ADDRESS to that address."
                     if valid else "Set POLYMARKET_SIGNATURE_TYPE=0 for an EOA wallet."),
        }

    # Proxy wallet: configured funder differs from the signer EOA.
    valid = current_sig in (1, 2)   # a proxy type must be set; 1 vs 2 not auto-determinable
    return {
        "derived_eoa": derived, "configured_funder": configured_funder,
        "addresses_match": False, "recommended_signature_type": None,
        "current_signature_type": current_sig, "configuration_valid": valid,
        "note": ("Proxy wallet detected: the funded address differs from your key's EOA. "
                 "Set POLYMARKET_SIGNATURE_TYPE to 1 (POLY_PROXY — email/Magic login) or 2 "
                 "(POLY_GNOSIS_SAFE — browser/Safe wallet); the exact type cannot be derived "
                 "from the address, so verify against how the account was created. The first "
                 "$1 order is the final check."
                 + ("" if valid else " Currently signature_type=0 (EOA) which is INVALID for a "
                    "proxy wallet — order placement is blocked until corrected.")),
    }


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class LiveConfig:
    enabled: bool
    executor: str            # dry_run | polymarket
    strategy: str            # which paper strategy's signals to copy
    position_usd: float      # fixed $ per position
    min_stake: float         # venue minimum / dust floor
    max_total_risk: float    # cap on total open exposure
    max_positions: int
    max_per_market: float
    max_per_wallet: float
    daily_loss_stop: float   # absolute $
    total_loss_stop: float   # absolute $
    max_orders: int          # 0 = unlimited; 1 = one-order test then auto-halt
    max_slippage_pct: float
    min_edge: float          # only copy signals with at least this edge
    min_confidence: float
    signal_ttl_min: float    # a signal older than this is EXPIRED (never acted on)
    # limit-at-reference execution (do NOT chase price/slippage)
    order_mode: str          # limit_at_reference (default) | marketable
    order_ttl_seconds: float # rest a resting limit this long, then cancel
    cancel_if_unfilled: bool
    allow_partial_fill: bool


def get_config() -> LiveConfig:
    return LiveConfig(
        enabled=_truthy(os.getenv("LIVE_TRADING_ENABLED", "false")),
        executor=os.getenv("LIVE_EXECUTOR", "dry_run").strip().lower(),
        strategy=os.getenv("LIVE_STRATEGY", "highest_edge"),   # 'Top-Decile Edge'
        position_usd=float(os.getenv("LIVE_POSITION_USD", "2.0")),
        min_stake=float(os.getenv("LIVE_MIN_STAKE", "1.0")),
        max_total_risk=float(os.getenv("LIVE_MAX_TOTAL_RISK", "40.0")),
        max_positions=int(os.getenv("LIVE_MAX_POSITIONS", "10")),
        max_per_market=float(os.getenv("LIVE_MAX_PER_MARKET", "4.0")),
        max_per_wallet=float(os.getenv("LIVE_MAX_PER_WALLET", "8.0")),
        daily_loss_stop=float(os.getenv("LIVE_DAILY_LOSS_STOP", "10.0")),
        total_loss_stop=float(os.getenv("LIVE_TOTAL_LOSS_STOP", "40.0")),
        max_orders=int(os.getenv("LIVE_MAX_ORDERS", "1")),
        max_slippage_pct=float(os.getenv("LIVE_MAX_SLIPPAGE_PCT", "0.03")),
        min_edge=float(os.getenv("LIVE_MIN_EDGE", "0.05")),
        min_confidence=float(os.getenv("LIVE_MIN_CONFIDENCE", "65")),
        signal_ttl_min=float(os.getenv("LIVE_SIGNAL_TTL_MIN", "30")),
        order_mode=os.getenv("ORDER_MODE", "limit_at_reference").strip().lower(),
        order_ttl_seconds=float(os.getenv("ORDER_TTL_SECONDS", "2")),
        cancel_if_unfilled=_truthy(os.getenv("CANCEL_IF_UNFILLED", "true")),
        allow_partial_fill=_truthy(os.getenv("ALLOW_PARTIAL_FILL", "true")),
    )


# ---------------------------------------------------------------------------
# Conservative sizing — tiny FIXED DOLLAR, absolute-cap clamped. PURE + tested.
# ---------------------------------------------------------------------------
def conservative_stake(cfg: LiveConfig, *, available_cash: float, total_open: float,
                       wallet_exposure: float, market_exposure: float) -> float | None:
    """Fixed $position_usd, clamped by total-risk, per-market, per-wallet caps and
    available cash. No leverage, no compounding, no Kelly. None if no room."""
    stake = min(
        cfg.position_usd,
        cfg.max_total_risk - total_open,
        cfg.max_per_market - market_exposure,
        cfg.max_per_wallet - wallet_exposure,
        available_cash,
    )
    if stake < cfg.min_stake:
        return None
    return round(stake, 2)


# ---------------------------------------------------------------------------
# State + risk gate
# ---------------------------------------------------------------------------
def get_state(db: Session) -> LiveState:
    st = db.get(LiveState, 1)
    if st is None:
        start = float(os.getenv("LIVE_STARTING_BANKROLL", "40.0"))
        st = LiveState(id=1, starting_bankroll=start, bankroll=start)
        db.add(st)
        db.commit()
    return st


def _open(db: Session) -> list[LiveExecution]:
    return list(db.scalars(select(LiveExecution).where(LiveExecution.status == "open")).all())


def _realized_since(db: Session, since: datetime) -> float:
    val = db.scalar(select(func.coalesce(func.sum(LiveExecution.realized_pnl), 0.0)).where(
        LiveExecution.status == "closed", LiveExecution.closed_at >= since))
    return float(val or 0.0)


def _realized_total(db: Session) -> float:
    val = db.scalar(select(func.coalesce(func.sum(LiveExecution.realized_pnl), 0.0)).where(
        LiveExecution.status == "closed"))
    return float(val or 0.0)


def _order_count(db: Session, executor: str) -> int:
    """Count of non-rejected orders placed by the given executor (for LIVE_MAX_ORDERS)."""
    return int(db.scalar(select(func.count()).select_from(LiveExecution).where(
        LiveExecution.executor == executor, LiveExecution.status != "rejected")) or 0)


def _trip_halt(db: Session, st: LiveState, reason: str) -> None:
    st.halted = True
    st.halt_reason = reason
    st.halted_at = datetime.utcnow()
    db.commit()


def check_can_open(db: Session, cfg: LiveConfig, *, wallet: str, market_id: str) -> tuple[bool, str]:
    """All hard pre-trade gates (absolute-dollar)."""
    st = get_state(db)
    if not cfg.enabled:
        return False, "LIVE_TRADING_ENABLED is false"
    if st.halted:
        return False, f"trading halted: {st.halt_reason}"
    # real orders: refuse until the wallet configuration is verified valid
    if cfg.executor == "polymarket":
        wc = wallet_check()
        if not wc["configuration_valid"]:
            return False, f"wallet config invalid: {wc.get('note') or 'see /api/live/status.wallet_check'}"
    open_ = _open(db)
    if len(open_) >= cfg.max_positions:
        return False, f"max open positions ({cfg.max_positions}) reached"
    if cfg.max_orders > 0 and _order_count(db, cfg.executor) >= cfg.max_orders:
        _trip_halt(db, st, f"max orders ({cfg.max_orders}) reached")
        return False, "max orders reached — halted"
    now = datetime.utcnow()
    day_pnl = _realized_since(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
    if day_pnl <= -cfg.daily_loss_stop:
        _trip_halt(db, st, f"daily loss stop (${cfg.daily_loss_stop:.0f}) hit")
        return False, "daily loss stop hit — halted"
    if _realized_total(db) <= -cfg.total_loss_stop:
        _trip_halt(db, st, f"total loss stop (${cfg.total_loss_stop:.0f}) hit")
        return False, "total loss stop hit — halted"
    return True, "ok"


def reset_test_state(db: Session) -> dict:
    """Clear REJECTED + DRY-RUN execution attempts (test artifacts) and the halt
    latch, so a blocked bankroll reset can proceed. Refuses if ANY real filled
    order exists — it can never delete real orders.

    Allowed only when: real_orders_placed == 0, no filled live orders, no open
    live positions (all from the polymarket executor)."""
    real_filled = db.scalar(select(func.count()).select_from(LiveExecution).where(
        LiveExecution.executor == "polymarket", LiveExecution.status.in_(("open", "closed"))))
    if real_filled:
        return {"ok": False, "error": f"{int(real_filled)} real polymarket order(s) exist; "
                                      "refusing (real orders are never deleted)"}
    n = db.query(LiveExecution).filter(
        (LiveExecution.status == "rejected") | (LiveExecution.executor == "dry_run")
    ).delete(synchronize_session=False)
    st = get_state(db)
    st.halted = False
    st.halt_reason = None
    db.commit()
    return {"ok": True, "cleared_attempts": int(n or 0),
            "real_orders_preserved": int(db.scalar(select(func.count()).select_from(
                LiveExecution).where(LiveExecution.executor == "polymarket")) or 0)}


def set_bankroll(db: Session, amount: float) -> dict:
    """Align the tracked starting/current bankroll with the ACTUAL funded balance.
    Only allowed with no executions yet (clean slate) so it can't rewrite history."""
    if db.scalar(select(func.count()).select_from(LiveExecution)):
        return {"ok": False, "error": "executions exist; run /api/live/reset-test-state first"}
    st = get_state(db)
    st.starting_bankroll = round(amount, 2)
    st.bankroll = round(amount, 2)
    db.commit()
    return {"ok": True, "starting_bankroll": st.starting_bankroll, "bankroll": st.bankroll}


def resume(db: Session) -> dict:
    st = get_state(db)
    st.halted = False
    st.halt_reason = None
    db.commit()
    return {"halted": False, "resumed_at": datetime.utcnow().isoformat()}


def halt(db: Session, reason: str = "manual") -> dict:
    st = get_state(db)
    _trip_halt(db, st, reason)
    return {"halted": True, "reason": reason}


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------
@dataclass
class OrderResult:
    """Outcome of a (real or simulated) order. `filled_usd`/`filled_shares` are
    the ACTUAL filled amounts (a partial fill is < requested). `outcome` is one of
    filled | partially_filled_cancelled | simulated. (unfilled_cancelled /
    submit_error / cancel_error never produce a position — they raise
    ExecutionRejected carrying that outcome.)"""
    outcome: str
    fill_price: float
    limit_price: float
    filled_usd: float
    filled_shares: float
    fees: float
    order_id: str | None
    order_latency_ms: float
    confirm_latency_ms: float
    venue_error: str | None = None
    tick_size: float | None = None        # venue book tick used (or fallback)
    min_order_size: float | None = None   # venue book min order size (shares) used


def _full_err(exc: Exception) -> str:
    """FULL untruncated venue error text (PolyApiException stringifies to
    'PolyApiException[status_code=..., error_message=...]')."""
    try:
        return str(exc) or repr(exc)
    except Exception:  # noqa: BLE001
        return repr(exc)


def _matched_shares(obj, requested: float, fallback: float = 0.0) -> float:
    """Read filled (matched) share count from a venue order/response dict,
    defensively across field-name variants. Falls back when unknown."""
    if isinstance(obj, dict):
        for k in ("size_matched", "sizeMatched", "matched_size", "filled_size", "matched_amount"):
            v = obj.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        st = str(obj.get("status", "")).lower()
        if st in ("matched", "filled"):
            return float(requested)
        if st in ("live", "delayed", "unmatched", "open"):
            return 0.0
    return fallback


def _extract_order_id(resp) -> str | None:
    if isinstance(resp, dict):
        oid = (resp.get("orderID") or resp.get("orderId") or resp.get("orderHash")
               or resp.get("hash") or resp.get("id"))
        return str(oid) if oid else None
    return None


def _ask_price(a) -> float:
    """Best-ask price from a book level (v2 dict {price,size} OR a v1-style object)."""
    return float(a["price"] if isinstance(a, dict) else a.price)


def _classify_venue(err: str) -> str:
    """Classify a venue error string into a precise outcome for the audit trail."""
    e = (err or "").lower()
    if "invalid order version" in e or "latest clob" in e or "order version" in e:
        return "stale_client_schema"     # client order schema behind the CLOB
    if "restricted in your region" in e or "geoblock" in e or "region" in e:
        return "geoblocked"
    return "submit_error"


class DryRunExecutor:
    """Simulated full fill at the reference price — full bookkeeping, zero capital."""
    name = "dry_run"

    def place(self, *, db, market, outcome, price, size_usd, cfg) -> OrderResult:
        shares = round(size_usd / max(0.01, price), 4)
        return OrderResult(outcome="simulated", fill_price=price, limit_price=price,
                           filled_usd=size_usd, filled_shares=shares, fees=0.0,
                           order_id="dryrun", order_latency_ms=0.0, confirm_latency_ms=0.0)


class PolymarketExecutor:
    """REAL order submission via the official py-clob-client (verified against
    v0.34.6). Fail-closed.

    AUTH (verified against py-clob-client 0.34.6 SOURCE, not docs/memory):
      * L1 = the wallet PRIVATE KEY -> Signer(key). Used to EIP-712-sign orders.
      * L2 = ApiCreds(api_key, api_secret, api_passphrase). Used to HMAC-sign the
        POST of each order (headers/headers.create_level_2_headers ->
        signing/hmac with creds.api_secret).
      * The L2 secret/passphrase are NOT provided by the operator and are NOT
        shown in the Polymarket UI: client.create_or_derive_api_creds() makes an
        L1-signed request to the CLOB which RETURNS {apiKey, secret, passphrase}
        (client.derive_api_key). So we DERIVE them from the private key. (That is
        precisely why the UI only exposes the API Key + Address.)
      * Signer address = the EOA from the private key. `funder` is the address
        that HOLDS the USDC; it DEFAULTS to the signer's EOA. For Polymarket
        proxy wallets (where the UI 'Address' / RELAYER_API_KEY_ADDRESS differs
        from the EOA) pass that address as the funder with the matching
        signature_type (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE).

    Required env (read at order time, never stored/logged):
      POLYMARKET_PRIVATE_KEY       funded wallet key                        [required]
      RELAYER_API_KEY_ADDRESS      account/funder address from the UI       [proxy wallets]
        (or POLYMARKET_FUNDER — same meaning)
      POLYMARKET_SIGNATURE_TYPE    0=EOA(default) · 1=POLY_PROXY · 2=POLY_GNOSIS_SAFE
      POLYMARKET_CLOB_HOST         default https://clob.polymarket.com
      POLYMARKET_CHAIN_ID          default 137 (Polygon)
      (POLYMARKET_API_KEY/SECRET/PASSPHRASE are OPTIONAL manual overrides only;
       normally unused — the creds are derived from the key.)
    """
    name = "polymarket"

    def _build_client(self, key: str):
        """Create + L2-authenticate the CLOB v2 client (auth failure -> reject,
        never place). Split out so it can be mocked in tests.

        v2 vs v1: ClobClient(host, chain_id, key=...); creds are obtained via
        derive_api_key() (existing) / create_api_key() (first time) — there is no
        create_or_derive_api_creds. Sig types are unchanged (0=EOA,1=PROXY,2=SAFE)."""
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
        except Exception as exc:  # noqa: BLE001
            raise ExecutionRejected(f"py-clob-client-v2 not installed: {exc}", outcome="sdk_missing")
        host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
        chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        # funder = address holding USDC (the UI 'Address' for proxy wallets);
        # defaults to the signer EOA when unset.
        funder = _configured_funder()
        # Optional manual full-creds override; normally None -> we DERIVE from key.
        ak, asec, apas = (os.getenv("POLYMARKET_API_KEY"), os.getenv("POLYMARKET_API_SECRET"),
                          os.getenv("POLYMARKET_API_PASSPHRASE"))
        manual = ApiCreds(api_key=ak, api_secret=asec, api_passphrase=apas) if (ak and asec and apas) else None
        try:
            client = ClobClient(host, chain_id=chain_id, key=key, creds=manual,
                                signature_type=sig_type, funder=funder)
            if manual is None:
                # L2 creds derived from the private key (L1-signed): try existing,
                # else create. py-clob-client-v2 has no create_or_derive helper.
                try:
                    creds = client.derive_api_key()
                except Exception:  # noqa: BLE001  (no creds yet -> create them)
                    creds = client.create_api_key()
                client.set_api_creds(creds)
            client.assert_level_2_auth()               # fail now if L2 auth is incomplete
        except ExecutionRejected:
            raise
        except Exception as exc:  # noqa: BLE001  (auth failure -> reject, never place)
            raise ExecutionRejected(f"auth/init failed: {_full_err(exc)}", venue_error=_full_err(exc))
        return client

    def place(self, *, db, market: Market, outcome: str, price: float, size_usd: float,
              cfg: LiveConfig) -> OrderResult:
        """LIMIT-AT-REFERENCE execution: we do NOT chase price. We post a GTC limit
        BUY at the copied wallet's reference price, hold it for ORDER_TTL_SECONDS,
        then read the fill and CANCEL any unfilled remainder. We never pay above the
        reference price (effective slippage 0), so a moved market resolves as
        unfilled_cancelled rather than a chased/over-paid fill."""
        key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if not key:
            raise ExecutionRejected("POLYMARKET_PRIVATE_KEY not set")
        _assert_real_sdk()                              # HARD guard: v2 present, v1 absent
        try:
            from py_clob_client_v2 import OrderArgsV2, OrderType, Side
        except Exception as exc:  # noqa: BLE001
            raise ExecutionRejected(f"py-clob-client-v2 not installed: {exc}", outcome="sdk_missing")

        # resolve CLOB token id from OUR stored market metadata (no API guessing)
        token_id = _token_id_for(market, outcome)
        if not token_id:
            raise ExecutionRejected(f"no token_id for outcome '{outcome}'")

        client = self._build_client(key)

        # v2 get_order_book returns a raw DICT that also carries tick_size +
        # min_order_size (shares). Prefer those venue-provided values; fall back to
        # get_tick_size() and the config notional floor only when absent.
        try:
            book = client.get_order_book(token_id)
            book_tick = book.get("tick_size") if isinstance(book, dict) else None
            book_min = book.get("min_order_size") if isinstance(book, dict) else None
            if book_tick is not None:
                tick, tick_src = float(book_tick), "book"
            else:
                tick, tick_src = float(client.get_tick_size(token_id)), "get_tick_size"
            asks = (book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)) or []
            best_ask = min((_ask_price(a) for a in asks), default=None)
        except Exception as exc:  # noqa: BLE001
            err = _full_err(exc)
            raise ExecutionRejected(f"order book fetch failed: {err}", outcome=_classify_venue(err), venue_error=err)
        if best_ask is None or best_ask <= 0:
            raise ExecutionRejected("no asks / empty book")

        # venue minimum (shares) when the book provides it; else the config floor.
        min_shares = float(book_min) if book_min is not None else None
        min_src = "book.min_order_size" if min_shares is not None else "config.min_stake"

        # LIMIT AT REFERENCE — never chase. Floor the reference to the tick grid so
        # the limit never EXCEEDS the reference (we accept reference or better only).
        import math
        reference = float(price)
        limit_price = round(min(0.99, max(tick, math.floor(reference / tick) * tick)), 4)
        shares = round(size_usd / limit_price, 2)

        # log the venue metadata used for THIS execution decision
        print(f"[live] exec token={token_id[:14]}… ref={reference} tick={tick}({tick_src}) "
              f"limit={limit_price} shares={shares} "
              f"min={min_shares if min_shares is not None else cfg.min_stake}({min_src})")

        # MIN-ORDER-SIZE gate (fail closed). Venue min is in SHARES; the config
        # fallback is a USD notional. No sizing change — validate only.
        if shares <= 0:
            raise ExecutionRejected("below min order size (0 shares)")
        if min_shares is not None:
            if shares < min_shares:
                raise ExecutionRejected(
                    f"below venue min_order_size ({shares} < {min_shares} shares)")
        elif shares * limit_price < cfg.min_stake:
            raise ExecutionRejected(f"below min order size (${shares*limit_price:.2f})")

        # SUBMIT a GTC limit at the reference; we manage TTL + cancel ourselves
        # (no FOK/marketable order -> cannot pay slippage). v2 create_order
        # auto-resolves tick/neg_risk/exchange version.
        t0 = datetime.utcnow()
        try:
            order = client.create_order(OrderArgsV2(price=limit_price, size=shares,
                                                    side=Side.BUY, token_id=token_id))
            resp = client.post_order(order, OrderType.GTC)
        except Exception as exc:  # noqa: BLE001  (submit failure -> reject, full error)
            err = _full_err(exc)
            raise ExecutionRejected(f"submit: {err}", outcome=_classify_venue(err), venue_error=err)
        order_id = _extract_order_id(resp)
        filled = _matched_shares(resp, shares)         # immediate match (best-effort)

        # HOLD for the TTL, then re-read the authoritative fill state
        ttl = max(0.0, cfg.order_ttl_seconds)
        if ttl:
            import time
            time.sleep(ttl)
        status_txt = ""
        if order_id:
            try:
                od = client.get_order(order_id)
                filled = _matched_shares(od, shares, fallback=filled)
                status_txt = str(od.get("status", "")) if isinstance(od, dict) else ""
            except Exception:  # noqa: BLE001  (status read failed -> use post-response estimate)
                pass
        latency = round((datetime.utcnow() - t0).total_seconds() * 1000.0, 1)

        fully = filled >= shares - 1e-9
        none_ = filled <= 1e-9

        # CANCEL any unfilled remainder immediately. v2 has no single cancel() —
        # cancel_orders([id]) takes a list of order ids/hashes.
        cancel_err = None
        if not fully and cfg.cancel_if_unfilled and order_id:
            try:
                client.cancel_orders([order_id])
            except Exception as exc:  # noqa: BLE001
                cancel_err = _full_err(exc)

        if fully:
            return OrderResult(outcome="filled", fill_price=limit_price, limit_price=limit_price,
                               filled_usd=round(shares * limit_price, 2), filled_shares=shares,
                               fees=0.0, order_id=order_id, order_latency_ms=latency,
                               confirm_latency_ms=latency, tick_size=tick, min_order_size=min_shares)
        if none_:
            if cancel_err:
                raise ExecutionRejected(f"cancel_error: {cancel_err}", outcome="cancel_error",
                                        venue_error=cancel_err)
            raise ExecutionRejected(
                f"unfilled_cancelled (limit {limit_price} vs ask {best_ask}, status={status_txt or 'n/a'})",
                outcome="unfilled_cancelled")
        # PARTIAL fill: keep the filled portion, remainder already cancelled above
        filled = round(min(filled, shares), 2)
        return OrderResult(outcome="partially_filled_cancelled", fill_price=limit_price,
                           limit_price=limit_price, filled_usd=round(filled * limit_price, 2),
                           filled_shares=filled, fees=0.0, order_id=order_id,
                           order_latency_ms=latency, confirm_latency_ms=latency,
                           venue_error=cancel_err, tick_size=tick, min_order_size=min_shares)


def _token_id_for(market: Market, outcome: str) -> str | None:
    try:
        outs = list(market.outcomes or [])
        toks = list(market.token_ids or [])
        if outcome in outs and len(toks) == len(outs):
            return str(toks[outs.index(outcome)])
    except Exception:  # noqa: BLE001
        pass
    return None


def get_executor(cfg: LiveConfig):
    return PolymarketExecutor() if cfg.executor == "polymarket" else DryRunExecutor()


# ---------------------------------------------------------------------------
# Order pipeline (gated)
# ---------------------------------------------------------------------------
def _wallet_exposure(open_, addr):
    return sum(e.size_usd for e in open_ if e.wallet_address == addr)


def _market_exposure(open_, mid):
    return sum(e.size_usd for e in open_ if e.market_id == mid)


def process_signal(db: Session, *, strategy_key: str, wallet: str, signal_id: int | None,
                   market: Market, outcome: str, price: float, entry_reason: str) -> LiveExecution | None:
    cfg = get_config()
    idem = f"{strategy_key}:{signal_id}"
    if db.scalar(select(LiveExecution).where(LiveExecution.idempotency_key == idem)):
        return None  # duplicate-order prevention
    st = get_state(db)

    def _reject(reason, size=0.0, *, fill_outcome=None, venue_error=None):
        db.add(LiveExecution(idempotency_key=idem, executor=cfg.executor, strategy_key=strategy_key,
                             wallet_address=wallet, signal_id=signal_id, market_id=market.id,
                             market_question=market.question or "", outcome=outcome, side="buy",
                             expected_price=round(price, 4), size_usd=size, status="rejected",
                             entry_reason=entry_reason, exit_reason=reason[:40],
                             fill_outcome=fill_outcome, venue_error=venue_error,
                             requested_size_usd=size or None, bankroll_before=st.bankroll))
        db.commit()

    ok, reason = check_can_open(db, cfg, wallet=wallet, market_id=market.id)
    if not ok:
        _reject(reason, fill_outcome="rejected")
        return None
    open_ = _open(db)
    available_cash = round(st.bankroll - sum(e.size_usd for e in open_), 2)
    stake = conservative_stake(cfg, available_cash=available_cash,
                               total_open=sum(e.size_usd for e in open_),
                               wallet_exposure=_wallet_exposure(open_, wallet),
                               market_exposure=_market_exposure(open_, market.id))
    if stake is None:
        _reject("no capital room within caps", fill_outcome="rejected")
        return None
    try:
        result = get_executor(cfg).place(db=db, market=market, outcome=outcome, price=price,
                                         size_usd=stake, cfg=cfg)
    except ExecutionRejected as exc:
        # unfilled_cancelled / submit_error / cancel_error / pre-trade rejects.
        # Capture the FULL venue error text (never truncated) in venue_error.
        _reject(f"exec: {exc}", size=stake, fill_outcome=(exc.outcome or "rejected"),
                venue_error=exc.venue_error)
        return None
    except Exception as exc:  # noqa: BLE001  (unexpected -> reject + halt, fail closed)
        _reject(f"error: {exc}", size=stake, fill_outcome="error", venue_error=_full_err(exc))
        _trip_halt(db, st, f"executor error: {str(exc)[:60]}")
        return None

    # filled or partially_filled_cancelled -> open a position for the ACTUAL filled
    # amount (a partial fill is smaller than the requested stake).
    pc = max(0.01, min(0.99, result.fill_price))
    filled_usd = round(result.filled_usd, 2)
    ex = LiveExecution(
        idempotency_key=idem, executor=cfg.executor, strategy_key=strategy_key,
        wallet_address=wallet, signal_id=signal_id, market_id=market.id,
        market_question=market.question or "", outcome=outcome, side="buy",
        expected_price=round(price, 4), limit_price=round(result.limit_price, 4),
        fill_price=round(result.fill_price, 4),
        slippage=round((result.fill_price - price) / price, 4) if price else 0.0,
        fees=round(result.fees, 4), size_usd=filled_usd, requested_size_usd=stake,
        shares=round(result.filled_shares, 4), fill_outcome=result.outcome,
        tick_size=result.tick_size, min_order_size=result.min_order_size,
        venue_error=result.venue_error, order_id=result.order_id,
        order_latency_ms=result.order_latency_ms, confirm_latency_ms=result.confirm_latency_ms,
        status="open", entry_reason=entry_reason, bankroll_before=st.bankroll)
    db.add(ex)
    db.commit()
    # one-order test: auto-halt after the configured number of orders
    if cfg.max_orders > 0 and _order_count(db, cfg.executor) >= cfg.max_orders:
        _trip_halt(db, st, f"one-order test complete ({cfg.max_orders}) — manual resume required")
    return ex


def settle_live(db: Session) -> dict:
    st = get_state(db)
    closed = 0
    now = datetime.utcnow()
    for ex in _open(db):
        m = db.get(Market, ex.market_id)
        if not (m and m.resolved and m.resolved_outcome is not None):
            continue
        won = m.resolved_outcome == ex.outcome
        payout = ex.shares * (1.0 if won else 0.0)
        ex.realized_pnl = round(payout - ex.size_usd - ex.fees, 2)
        ex.status = "closed"
        ex.exit_reason = "resolved"
        ex.closed_at = now
        ex.settled_at = m.resolved_at or now
        st.bankroll = round(st.bankroll + ex.realized_pnl, 2)
        ex.bankroll_after = st.bankroll
        closed += 1
    db.commit()
    return {"closed": closed, "bankroll": st.bankroll}


# ===========================================================================
# EVENT-DRIVEN execution pipeline + full decision observability
#
# The worker calls run_pipeline(place=True) every cycle: signals are created
# moments earlier in the SAME cycle, so a brand-new qualifying signal is acted on
# immediately — no "is there a signal right now" polling. Each NEW signal (one
# with no LiveSignalDecision row) flows through the exact gate sequence below and
# leaves a permanent audit row, so it is evaluated exactly once and can never
# execute twice. /api/live/run-once calls run_pipeline(place=False): the SAME
# decision logic, but a pure read-only DIAGNOSTIC that places nothing and writes
# no rows — our primary debugging tool. The gates, ranking, sizing and risk
# limits are unchanged; this only changes WHEN execution triggers and records WHY.
# ===========================================================================

# Every signal is bucketed into exactly one of these. 'filled' (placed) and
# 'would_execute' (qualifies; diagnostic) are outcomes, not filters.
_FILTER_KEYS = ("already_processed", "trading_disabled", "wallet_not_eligible",
                "low_edge", "low_confidence", "market_closed", "stale",
                "duplicate", "risk_blocked", "no_capital", "slippage",
                "unfilled_cancelled", "geoblocked", "stale_client_schema", "exec_error")


def _ranking(db: Session) -> tuple[set, dict]:
    """Production wallet ranking evaluated ONCE per pass (unchanged logic). Returns
    the eligible top-N address set + {address: production_rank_score} for the report."""
    from . import live_ranking
    cfg = live_ranking._cfg()
    ranked = live_ranking.rank_wallets(db, include_failed=True)
    score_map = {r["address"]: r["production_rank_score"] for r in ranked}
    eligible = [r["address"] for r in ranked if r["eligible"]][: cfg["top_n"]]
    return set(eligible), score_map


def _categorize_rejection(reason: str, outcome: str | None = None) -> str:
    """Map a process_signal rejection to a report category. Prefers the precise
    execution outcome when present (limit-at-reference / v2 outcomes)."""
    if outcome == "unfilled_cancelled":
        return "unfilled_cancelled"
    if outcome == "geoblocked":
        return "geoblocked"
    if outcome == "stale_client_schema":
        return "stale_client_schema"
    if outcome in ("submit_error", "cancel_error", "error", "sdk_missing", "archived_sdk"):
        return "exec_error"
    r = (reason or "").lower()
    if "unfilled_cancelled" in r:
        return "unfilled_cancelled"
    if "invalid order version" in r or "latest clob" in r:
        return "stale_client_schema"
    if "restricted in your region" in r or "geoblock" in r:
        return "geoblocked"
    if "slippage" in r:
        return "slippage"
    if "no capital" in r or "below min" in r or "min order" in r or "min_order_size" in r:
        return "no_capital"
    if any(k in r for k in ("exec:", "submit", "auth", "book", "token_id",
                            "not installed", "not filled", "error", "sdk")):
        return "exec_error"
    return "risk_blocked"   # halted / max orders / loss stop / config invalid / positions


def _decide_one(db: Session, s, cfg: LiveConfig, eligible: set, score_map: dict,
                place: bool) -> tuple[dict, bool, int | None]:
    """Run one NEW signal through the full gate sequence. Returns
    (candidate_report, should_record, execution_id). When place=True and the
    signal qualifies, the (unchanged) order pipeline is invoked."""
    from .models import Wallet
    w = db.get(Wallet, s.wallet_id)
    m = db.get(Market, s.market_id)
    addr = w.address if w else None
    edge = float(s.edge_estimate or 0.0)
    conf = float(s.confidence or 0.0)
    gates: dict = {}
    cand = {"signal_id": s.id, "wallet": addr, "edge": round(edge, 4),
            "confidence": round(conf, 1), "production_score": score_map.get(addr),
            "eligible": False, "gates": gates, "status": None, "category": None, "reason": None}

    def done(status, category, reason, record=True, exec_id=None):
        cand["status"], cand["category"], cand["reason"] = status, category, reason
        return cand, record, exec_id

    # GATE 1 — trading enabled. Do NOT record while disabled, so the signal is
    # re-evaluated once trading is turned on (it must not be silently consumed).
    gates["trading_enabled"] = cfg.enabled
    if not cfg.enabled:
        return done("skipped", "trading_disabled", "LIVE_TRADING_ENABLED is false", record=False)

    # GATE 2 — production wallet ranking (profitability filters + top-N).
    is_elig = bool(addr and addr in eligible)
    gates["wallet_eligible"] = is_elig
    if not is_elig:
        return done("skipped", "wallet_not_eligible",
                    f"wallet {(addr or '?')[:12]} not in production top-{len(eligible)}")

    # GATE 3 — edge threshold.
    gates["edge_ok"] = edge >= cfg.min_edge
    if edge < cfg.min_edge:
        return done("skipped", "low_edge", f"edge {edge:.3f} < min {cfg.min_edge}")

    # GATE 4 — confidence threshold.
    gates["confidence_ok"] = conf >= cfg.min_confidence
    if conf < cfg.min_confidence:
        return done("skipped", "low_confidence", f"confidence {conf:.0f} < min {cfg.min_confidence:.0f}")

    # GATE 5 — market still open.
    market_open = bool(m and not m.resolved)
    gates["market_open"] = market_open
    if not market_open:
        return done("skipped", "market_closed", "market missing or already resolved")

    # GATE 6 — signal freshness (don't act on a stale signal).
    age_min = (datetime.utcnow() - s.created_at).total_seconds() / 60.0 if s.created_at else 0.0
    fresh = age_min <= cfg.signal_ttl_min
    gates["fresh"] = fresh
    if not fresh:
        return done("expired", "stale", f"signal age {age_min:.0f}m > TTL {cfg.signal_ttl_min:.0f}m")

    # Passed every pre-execution gate -> this is an ACTIONABLE signal.
    cand["eligible"] = True
    if not place:
        # diagnostic: report that it qualifies; the worker performs the execution.
        return done("eligible", "would_execute", "qualifies — worker will execute", record=False)

    # GATE 7 — duplicate protection (idempotency on strategy:signal).
    idem = f"{cfg.strategy}:{s.id}"
    pre = db.scalar(select(LiveExecution).where(LiveExecution.idempotency_key == idem))
    if pre is not None:
        gates["duplicate_check"] = False
        return done("skipped", "duplicate", "execution already exists for this signal", exec_id=pre.id)
    gates["duplicate_check"] = True

    # GATES 8-10 — risk + sizing + slippage + submit, via the unchanged, tested
    # order pipeline (it logs a LiveExecution: 'open' on fill, 'rejected' otherwise).
    ex = process_signal(db, strategy_key=cfg.strategy, wallet=addr, signal_id=s.id, market=m,
                        outcome=s.outcome, price=float(s.observed_price or 0.5),
                        entry_reason=f"copy {(addr or '')[:10]} conf={conf:.0f} edge={edge}")
    if ex is not None:
        gates["risk_passed"] = True
        gates["submitted"] = True
        gates["filled"] = True
        note = "partial " if ex.fill_outcome == "partially_filled_cancelled" else ""
        return done("filled", "filled", f"{note}order placed ${ex.size_usd:.2f} @ {ex.fill_price}", exec_id=ex.id)
    rej = db.scalar(select(LiveExecution).where(LiveExecution.idempotency_key == idem))
    rr = (rej.exit_reason or "") if rej else "rejected"
    category = _categorize_rejection(rr, getattr(rej, "fill_outcome", None) if rej else None)
    gates["risk_passed"] = category not in ("risk_blocked", "no_capital")
    # we reached the venue (order posted / venue responded) for these outcomes
    gates["submitted"] = category in ("slippage", "exec_error", "unfilled_cancelled",
                                      "geoblocked", "stale_client_schema")
    gates["filled"] = False
    return done("rejected", category, rr, exec_id=(rej.id if rej else None))


def _record_decision(db: Session, s, cand: dict, execution_id: int | None) -> None:
    """Persist the per-signal audit row (the 'processed' marker). The unique
    constraint makes a concurrent double-record a harmless no-op."""
    try:
        db.add(LiveSignalDecision(
            signal_id=s.id, status=cand["status"], category=cand["category"],
            reason=(cand["reason"] or "")[:1000], wallet_address=cand["wallet"],
            edge=cand["edge"], confidence=cand["confidence"],
            production_score=cand["production_score"], gates=cand["gates"],
            execution_id=execution_id))
        db.commit()
    except Exception:  # noqa: BLE001  (unique violation -> already recorded)
        db.rollback()


def _decision_to_candidate(d: LiveSignalDecision) -> dict:
    """Render an already-recorded decision back into the report (the audit trail)."""
    return {"signal_id": d.signal_id, "wallet": d.wallet_address, "edge": d.edge,
            "confidence": d.confidence, "production_score": d.production_score,
            "eligible": (d.gates or {}).get("market_open", False) and d.status in ("filled", "rejected"),
            "gates": d.gates or {}, "status": d.status, "category": d.category,
            "reason": d.reason, "recorded": True}


def _summarize(report: dict) -> str:
    if report["placed"]:
        return f"placed {report['placed']} order(s)"
    if report["new_evaluated"] == 0:
        return "no new signals"
    if report["mode"] == "diagnostic" and report["eligible"]:
        return f"{report['eligible']} qualifying signal(s) pending worker execution"
    if report["eligible"] == 0:
        return "no new qualifying signals"
    return "qualifying signals evaluated; none filled"


def run_pipeline(db: Session, place: bool = True) -> dict:
    """The single decision pipeline shared by the worker (place=True, event-driven
    execution) and /api/live/run-once (place=False, read-only diagnostic).

    ALWAYS settles/monitors existing live positions. Then evaluates every signal
    in the recent window: those already carrying a decision are reported from their
    stored audit row (never re-run); brand-NEW signals flow through the full gate
    sequence and, when place=True, are executed + recorded. Returns a complete,
    explainable decision report."""
    settle_live(db)
    cfg = get_config()
    window_min = float(os.getenv("LIVE_SIGNAL_WINDOW_MIN", "120"))
    from .models import PaperSignal

    cutoff = datetime.utcnow() - timedelta(minutes=window_min)
    sigs = db.scalars(select(PaperSignal).where(PaperSignal.created_at >= cutoff)
                      .order_by(PaperSignal.created_at.asc())).all()
    report = {
        "placed": 0, "mode": "execute" if place else "diagnostic",
        "signals_seen": len(sigs), "new_evaluated": 0, "eligible": 0,
        "executor_called": False, "reason": "",
        "filtered": {k: 0 for k in _FILTER_KEYS}, "candidates": [],
    }
    if not sigs:
        report["reason"] = "no signals in window"
        return report

    decisions = {d.signal_id: d for d in db.scalars(select(LiveSignalDecision).where(
        LiveSignalDecision.signal_id.in_([s.id for s in sigs]))).all()}
    # Only reconstruct the production ranking when there is at least one NEW signal
    # to score (the worker runs every cycle; skip the work when nothing is new).
    has_new = any(s.id not in decisions for s in sigs)
    eligible, score_map = _ranking(db) if has_new else (set(), {})

    for s in sigs:
        prior = decisions.get(s.id)
        if prior is not None:                       # already processed -> never re-run
            report["filtered"]["already_processed"] += 1
            report["candidates"].append(_decision_to_candidate(prior))
            continue
        report["new_evaluated"] += 1
        cand, record, exec_id = _decide_one(db, s, cfg, eligible, score_map, place)
        report["candidates"].append(cand)
        if cand["eligible"]:
            report["eligible"] += 1
        cat = cand["category"]
        if cat == "filled":
            report["placed"] += 1
            report["executor_called"] = True
        elif cat in ("slippage", "exec_error"):
            report["executor_called"] = True
            report["filtered"][cat] += 1
        elif cat in report["filtered"]:
            report["filtered"][cat] += 1
        if place and record:
            _record_decision(db, s, cand, exec_id)
        if place and get_state(db).halted:          # one-order test: stop after the halt
            break

    report["reason"] = _summarize(report)
    return report


def signal_decisions(db: Session, limit: int = 100) -> list[dict]:
    """Recent per-signal decision audit rows (newest first) — the execution trail.
    Read-only; joins the originating signal -> market for display context."""
    from .models import PaperSignal
    rows = db.scalars(select(LiveSignalDecision).order_by(
        LiveSignalDecision.created_at.desc()).limit(limit)).all()
    sigs = {s.id: s for s in db.scalars(select(PaperSignal).where(
        PaperSignal.id.in_([d.signal_id for d in rows]))).all()} if rows else {}
    mkts = {m.id: m for m in db.scalars(select(Market).where(
        Market.id.in_({s.market_id for s in sigs.values()}))).all()} if sigs else {}
    out = []
    for d in rows:
        s = sigs.get(d.signal_id)
        m = mkts.get(s.market_id) if s else None
        out.append({
            "id": d.id, "created_at": d.created_at.isoformat() if d.created_at else None,
            "signal_id": d.signal_id, "status": d.status, "category": d.category,
            "reason": d.reason, "wallet": d.wallet_address, "edge": d.edge,
            "confidence": d.confidence, "production_score": d.production_score,
            "gates": d.gates or {}, "execution_id": d.execution_id,
            "market_id": (s.market_id if s else None),
            "market": (m.question if m else None),
            "outcome": (s.outcome if s else None),
        })
    return out


def reconcile(db: Session, reported_balance: float, tolerance: float = 0.50) -> dict:
    st = get_state(db)
    computed = round(st.starting_bankroll + _realized_total(db), 2)
    open_exposure = round(sum(e.size_usd for e in _open(db)), 2)
    expected_cash = round(computed - open_exposure, 2)
    drift = round(reported_balance - expected_cash, 2)
    return {"starting_bankroll": st.starting_bankroll, "computed_equity": computed,
            "open_exposure": open_exposure, "expected_cash": expected_cash,
            "reported_balance": round(reported_balance, 2), "drift": drift,
            "reconciled": abs(drift) <= tolerance}


def status(db: Session) -> dict:
    cfg = get_config()
    st = get_state(db)
    open_ = _open(db)
    now = datetime.utcnow()
    return {
        "paper_trading_default": True,
        "live_trading_enabled": cfg.enabled,
        "executor": cfg.executor,
        "real_orders_placed": _order_count(db, "polymarket"),
        "orders_this_executor": _order_count(db, cfg.executor),
        "auth": {  # L1 = private key (signs). L2 = manual API creds if all 3 set, else derived.
            "py_clob_client_installed": py_clob_installed(),   # v2 SDK present
            "l1_private_key_present": bool(os.getenv("POLYMARKET_PRIVATE_KEY")),
            # booleans only — NEVER expose the secret/passphrase values
            "l2_manual_creds_present": _manual_l2_creds_present(),
            "l2_creds_source": "manual_api_creds" if _manual_l2_creds_present() else "derived_from_private_key",
            "funder": _configured_funder() or "(defaults to signer EOA)",
            "signature_type": int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        },
        "sdk": sdk_info(),
        "wallet_check": wallet_check(),
        "strategy_copied": cfg.strategy,
        "wallet_selection": "production_rank_score (40% reputation, 30% PF, 20% ROI, "
                            "10% recency; filters ROI>0, PF>1.20) — see /api/live/wallet-ranking",
        "sizing": {"method": "fixed_dollar", "position_usd": cfg.position_usd,
                   "min_stake": cfg.min_stake, "no_compounding": True, "no_leverage": True},
        "limits_usd": {"max_position": cfg.position_usd, "max_total_risk": cfg.max_total_risk,
                       "max_positions": cfg.max_positions, "max_per_market": cfg.max_per_market,
                       "max_per_wallet": cfg.max_per_wallet, "daily_loss_stop": cfg.daily_loss_stop,
                       "total_loss_stop": cfg.total_loss_stop},
        "max_orders": cfg.max_orders, "max_slippage_pct": cfg.max_slippage_pct,
        "execution": {  # limit-at-reference: never chase price; rest then cancel
            "order_mode": cfg.order_mode, "order_ttl_seconds": cfg.order_ttl_seconds,
            "cancel_if_unfilled": cfg.cancel_if_unfilled, "allow_partial_fill": cfg.allow_partial_fill,
            "effective_slippage": 0.0},
        "state": {"starting_bankroll": st.starting_bankroll, "bankroll": st.bankroll,
                  "halted": st.halted, "halt_reason": st.halt_reason},
        "open_positions": len(open_), "open_exposure": round(sum(e.size_usd for e in open_), 2),
        "day_pnl": round(_realized_since(db, now.replace(hour=0, minute=0, second=0, microsecond=0)), 2),
        "total_realized": round(_realized_total(db), 2),
        "max_possible_loss": cfg.total_loss_stop,
    }


def list_executions(db: Session, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(LiveExecution).order_by(LiveExecution.created_at.desc()).limit(limit)).all()
    return [{
        "id": e.id, "created_at": e.created_at.isoformat() if e.created_at else None,
        "executor": e.executor, "strategy": e.strategy_key, "wallet": e.wallet_address,
        "signal_id": e.signal_id, "market_id": e.market_id, "market_question": e.market_question,
        "outcome": e.outcome, "side": e.side, "expected_price": e.expected_price,
        "limit_price": e.limit_price, "fill_price": e.fill_price, "slippage": e.slippage,
        "fees": e.fees, "size_usd": e.size_usd, "requested_size_usd": e.requested_size_usd,
        "tick_size": e.tick_size, "min_order_size": e.min_order_size,
        "shares": e.shares, "order_id": e.order_id,
        "order_latency_ms": e.order_latency_ms, "confirm_latency_ms": e.confirm_latency_ms,
        "status": e.status, "fill_outcome": e.fill_outcome, "venue_error": e.venue_error,
        "entry_reason": e.entry_reason, "exit_reason": e.exit_reason,
        "realized_pnl": e.realized_pnl, "bankroll_before": e.bankroll_before,
        "bankroll_after": e.bankroll_after,
    } for e in rows]
