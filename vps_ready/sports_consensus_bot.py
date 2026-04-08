"""
arb3 — Sports Consensus Bot

Watches the top-N sports traders (from smart_wallets.json). When 3+ of
them open positions on the same (market, direction) within 15 minutes,
enters the trade with SL/TP/time-stop exit logic.

Unlike the original polymarket_bot.py, this bot DOES manage exits:
    - Stop loss at -30% of cost
    - Take profit at +50% of cost
    - Time exit after 12 hours

Run (paper):
    python3 sports_consensus_bot.py --paper

Run (live):
    POLY_PRIVATE_KEY=0x... POLY_FUNDER=0x... python3 sports_consensus_bot.py

Dependencies: aiohttp, py-clob-client
"""

import argparse
import asyncio
import aiohttp
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_notify import notify, notify_critical  # noqa: E402

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID_POLYGON = 137

SMART_WALLETS_FILE = os.getenv("SMART_WALLETS_FILE", "smart_wallets.json")
STATE_FILE         = os.getenv("STATE_FILE", "sports_state.json")

# Consensus params
CONSENSUS_MIN      = 3              # 3+ whales agree
CONSENSUS_WINDOW   = 15 * 60        # 15 minutes
SCAN_INTERVAL      = 120            # 2 minutes

# Exit params
STOP_LOSS_PCT      = 0.30           # close if -30% from entry cost
TAKE_PROFIT_PCT    = 0.50           # close if +50%
MAX_HOLD_SECONDS   = 12 * 3600      # 12h hard time stop

# Risk
TRADE_USDC         = 20.0
MAX_DAILY_LOSS     = 10.0
MAX_OPEN_POSITIONS = 5
PRICE_MIN          = 0.10
PRICE_MAX          = 0.90
SKIP_LIVE_MATCHES  = True           # avoid markets tagged "live" or with start_time passed

# ═══════════════════════════════════════════════════════
# HELPERS (same pattern as other bots)
# ═══════════════════════════════════════════════════════

def _f(d, *keys, default=0.0):
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _s(d, *keys, default=""):
    for k in keys:
        if k in d and d[k] is not None:
            return str(d[k])
    return default


def _parse_token_ids(raw) -> tuple[str, str]:
    if not raw:
        return "", ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "", ""
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return "", ""


async def fetch_json(session, url, params=None):
    try:
        async with session.get(
            url, params=params or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        print(f"[http] {url}: {e}")
        return None


# ═══════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════

def load_smart_wallets(path: str) -> list[str]:
    try:
        with open(path) as f:
            data = json.load(f)
        wallets = data.get("wallets", [])
        addrs = [w["address"].lower() for w in wallets if w.get("address")]
        print(f"[wallets] loaded {len(addrs)} from {path}")
        return addrs
    except FileNotFoundError:
        print(f"[wallets] {path} not found — run sports_trader_finder.py first")
        return []
    except Exception as e:
        print(f"[wallets] load failed: {e}")
        return []


async def get_wallet_positions(session, addr: str) -> list[dict]:
    data = await fetch_json(session, f"{DATA_API}/positions", {
        "user":  addr,
        "limit": 100,
    })
    return data if isinstance(data, list) else []


async def get_market(session, condition_id: str) -> Optional[dict]:
    data = await fetch_json(session, f"{GAMMA_API}/markets", {
        "condition_ids": condition_id,
        "limit": 1,
    })
    if isinstance(data, list) and data:
        return data[0]
    return None


async def get_token_midpoint(session, token_id: str) -> float:
    data = await fetch_json(session, f"{CLOB_HOST}/midpoint", {"token_id": token_id})
    if not data:
        return 0.0
    return _f(data, "mid", "midpoint", default=0.0)


def is_sports_market(m: dict) -> bool:
    tags = m.get("tags") or m.get("category") or []
    if isinstance(tags, str):
        tags = [tags]
    sports = {"sports", "nba", "nfl", "mlb", "nhl", "soccer", "football",
              "basketball", "baseball", "hockey", "ufc", "boxing", "tennis",
              "golf", "cricket", "esports"}
    return bool({str(t).lower() for t in tags} & sports)


def is_live_match(m: dict) -> bool:
    """Heuristic: market is 'live' if its start time has passed but it hasn't resolved."""
    start = m.get("startDate") or m.get("gameStartTime")
    if not start:
        return False
    try:
        if isinstance(start, (int, float)):
            start_ts = float(start)
        else:
            start_ts = datetime.fromisoformat(
                str(start).replace("Z", "+00:00")
            ).timestamp()
        return time.time() > start_ts
    except Exception:
        return False


# ═══════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════

@dataclass
class OpenPosition:
    market_id: str
    token_id:  str
    direction: str
    shares:    float
    entry:     float
    cost:      float
    opened_at: float
    question:  str = ""


@dataclass
class State:
    day:         str = ""
    trades:      int = 0
    realized:    float = 0.0
    unrealized:  float = 0.0
    positions:   dict = field(default_factory=dict)       # market_id -> OpenPosition
    wallet_activity: dict = field(default_factory=dict)   # (market, direction) -> [(wallet, ts)]

    def maybe_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            print(f"[DAY] {self.day or '-'} -> {today}")
            self.day = today
            self.trades = 0
            self.realized = 0.0

    @property
    def total_pnl(self) -> float:
        return self.realized + self.unrealized

    def save(self, path: str):
        try:
            with open(path, "w") as f:
                json.dump({
                    "day": self.day,
                    "trades": self.trades,
                    "realized": self.realized,
                    "positions": {
                        k: {
                            "market_id": v.market_id,
                            "token_id":  v.token_id,
                            "direction": v.direction,
                            "shares":    v.shares,
                            "entry":     v.entry,
                            "cost":      v.cost,
                            "opened_at": v.opened_at,
                            "question":  v.question,
                        }
                        for k, v in self.positions.items()
                    },
                }, f, indent=2)
        except Exception as e:
            print(f"[state] save failed: {e}")

    @classmethod
    def load(cls, path: str) -> "State":
        s = cls()
        try:
            with open(path) as f:
                data = json.load(f)
            s.day = data.get("day", "")
            s.trades = int(data.get("trades", 0))
            s.realized = float(data.get("realized", 0.0))
            for k, v in data.get("positions", {}).items():
                s.positions[k] = OpenPosition(**v)
            print(f"[state] loaded {len(s.positions)} open positions")
        except FileNotFoundError:
            print("[state] fresh")
        except Exception as e:
            print(f"[state] load failed: {e}")
        return s


# ═══════════════════════════════════════════════════════
# CONSENSUS
# ═══════════════════════════════════════════════════════

async def collect_recent_activity(session, wallets: list[str], state: State):
    """Build a map of (market_id, direction) -> {wallet: latest_ts}."""
    cutoff = time.time() - CONSENSUS_WINDOW
    fresh: dict[tuple, dict] = defaultdict(dict)

    for addr in wallets:
        positions = await get_wallet_positions(session, addr)
        for p in positions:
            ts = _f(p, "lastTradedTimestamp", "lastUpdateTimestamp", "timestamp")
            if ts and ts < cutoff:
                continue
            cid = _s(p, "conditionId", "condition_id")
            outcome = _s(p, "outcome").upper()
            if not cid or outcome not in ("YES", "NO"):
                continue
            fresh[(cid, outcome)][addr] = ts or time.time()
        await asyncio.sleep(0.3)

    state.wallet_activity = fresh


def find_consensus(state: State) -> list[tuple[str, str, int]]:
    """Return [(market_id, direction, count), ...] where count >= CONSENSUS_MIN."""
    out = []
    for (cid, direction), wallets in state.wallet_activity.items():
        if len(wallets) >= CONSENSUS_MIN:
            out.append((cid, direction, len(wallets)))
    return out


# ═══════════════════════════════════════════════════════
# ENTRY
# ═══════════════════════════════════════════════════════

async def place_order(token_id: str, price: float, shares: float) -> Optional[dict]:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    client = ClobClient(
        host     = CLOB_HOST,
        key      = os.getenv("POLY_PRIVATE_KEY", ""),
        chain_id = CHAIN_ID_POLYGON,
        funder   = os.getenv("POLY_FUNDER", ""),
        signature_type = int(os.getenv("POLY_SIG_TYPE", "0")),
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    side = BUY  # entry is always BUY; sells are handled separately
    order = client.create_order(OrderArgs(
        token_id=token_id, price=price, size=shares, side=side,
    ))
    return client.post_order(order, OrderType.GTC)


async def sell_order(token_id: str, price: float, shares: float) -> Optional[dict]:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    client = ClobClient(
        host     = CLOB_HOST,
        key      = os.getenv("POLY_PRIVATE_KEY", ""),
        chain_id = CHAIN_ID_POLYGON,
        funder   = os.getenv("POLY_FUNDER", ""),
        signature_type = int(os.getenv("POLY_SIG_TYPE", "0")),
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    order = client.create_order(OrderArgs(
        token_id=token_id, price=price, size=shares, side=SELL,
    ))
    return client.post_order(order, OrderType.GTC)


async def enter_consensus(session, cid: str, direction: str,
                          wallet_count: int, state: State, paper: bool):
    if cid in state.positions:
        return
    if len(state.positions) >= MAX_OPEN_POSITIONS:
        print(f"[cap] max open positions")
        return
    if state.total_pnl <= -MAX_DAILY_LOSS:
        print(f"[cap] daily loss limit")
        return

    market = await get_market(session, cid)
    if not market:
        return
    if not is_sports_market(market):
        return
    if SKIP_LIVE_MATCHES and is_live_match(market):
        print(f"[skip] live match {_s(market, 'question')[:40]}")
        return

    yes_id, no_id = _parse_token_ids(market.get("clobTokenIds"))
    token_id = yes_id if direction == "YES" else no_id
    if not token_id:
        return

    price = _f(market, "bestAsk", "best_ask", "lastTradePrice")
    if direction == "NO":
        price = 1 - price  # approximate NO ask
    if not (PRICE_MIN <= price <= PRICE_MAX):
        return

    shares = round(TRADE_USDC / price, 2)
    question = _s(market, "question", default=cid)[:60]

    msg = (f"ENTRY {direction} {question}\n"
           f"wallets={wallet_count} price={price:.4f} shares={shares}")
    print(msg)
    notify(msg, tag="arb3")

    if paper:
        state.positions[cid] = OpenPosition(
            market_id=cid, token_id=token_id, direction=direction,
            shares=shares, entry=price, cost=TRADE_USDC,
            opened_at=time.time(), question=question,
        )
        state.trades += 1
        return

    try:
        resp = await place_order(token_id, price, shares)
        if not resp or not resp.get("success", True):
            notify_critical(f"entry rejected: {resp}", tag="arb3")
            return
        state.positions[cid] = OpenPosition(
            market_id=cid, token_id=token_id, direction=direction,
            shares=shares, entry=price, cost=TRADE_USDC,
            opened_at=time.time(), question=question,
        )
        state.trades += 1
        notify(f"ENTRY OK {resp.get('orderID', '?')}", tag="arb3")
    except Exception as e:
        notify_critical(f"entry failed: {e}", tag="arb3")


# ═══════════════════════════════════════════════════════
# EXIT LOGIC
# ═══════════════════════════════════════════════════════

async def manage_positions(session, state: State, paper: bool):
    """Check each open position for SL/TP/time exit."""
    unrealized = 0.0
    to_close: list[tuple[str, str]] = []  # (market_id, reason)

    for cid, pos in list(state.positions.items()):
        cur = await get_token_midpoint(session, pos.token_id)
        if cur <= 0:
            continue

        current_value = cur * pos.shares
        pnl = current_value - pos.cost
        pnl_pct = pnl / pos.cost if pos.cost > 0 else 0.0
        unrealized += pnl

        age = time.time() - pos.opened_at

        if pnl_pct >= TAKE_PROFIT_PCT:
            to_close.append((cid, f"TP +{pnl_pct:.1%}"))
        elif pnl_pct <= -STOP_LOSS_PCT:
            to_close.append((cid, f"SL {pnl_pct:+.1%}"))
        elif age >= MAX_HOLD_SECONDS:
            to_close.append((cid, f"TIME {age/3600:.1f}h"))

    state.unrealized = round(unrealized, 2)

    for cid, reason in to_close:
        pos = state.positions.get(cid)
        if not pos:
            continue
        cur = await get_token_midpoint(session, pos.token_id)
        if cur <= 0:
            print(f"[exit] {cid} skip: no price")
            continue

        realized = (cur - pos.entry) * pos.shares
        msg = (f"EXIT [{reason}] {pos.question}\n"
               f"entry={pos.entry:.4f} exit={cur:.4f} "
               f"pnl=${realized:+.2f}")
        print(msg)
        notify(msg, tag="arb3")

        if paper:
            state.realized += realized
            del state.positions[cid]
            continue

        try:
            # Use a slightly aggressive sell price to ensure fill
            sell_price = max(cur - 0.01, 0.01)
            resp = await sell_order(pos.token_id, sell_price, pos.shares)
            if not resp or not resp.get("success", True):
                notify_critical(f"exit rejected: {resp}", tag="arb3")
                continue
            state.realized += realized
            del state.positions[cid]
            notify(f"EXIT OK {resp.get('orderID', '?')}", tag="arb3")
        except Exception as e:
            notify_critical(f"exit failed: {e}", tag="arb3")


# ═══════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════

async def main_loop(paper: bool):
    print(f"[arb3] mode={'PAPER' if paper else 'LIVE'}")
    notify(f"started ({'PAPER' if paper else 'LIVE'})", tag="arb3")

    wallets = load_smart_wallets(SMART_WALLETS_FILE)
    if not wallets:
        print("[arb3] no wallets to track, exiting")
        return

    state = State.load(STATE_FILE)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                state.maybe_reset()
                t0 = time.time()

                # 1. Exit management first (protect capital)
                if state.positions:
                    await manage_positions(session, state, paper)

                # 2. Scan for new consensus signals
                await collect_recent_activity(session, wallets, state)
                consensus = find_consensus(state)
                print(f"[scan] {len(consensus)} consensus signals")

                for cid, direction, count in consensus:
                    await enter_consensus(session, cid, direction, count, state, paper)
                    await asyncio.sleep(1)

                state.save(STATE_FILE)
                print(f"[cycle] {time.time()-t0:.1f}s "
                      f"open={len(state.positions)} "
                      f"realized=${state.realized:.2f} "
                      f"unrealized=${state.unrealized:.2f}")

            except Exception as e:
                print(f"[loop] error: {e}")
                notify_critical(f"loop error: {e}", tag="arb3")

            await asyncio.sleep(SCAN_INTERVAL)


def preflight(paper: bool) -> bool:
    if not os.path.exists(SMART_WALLETS_FILE):
        print(f"[PREFLIGHT] {SMART_WALLETS_FILE} missing — run sports_trader_finder.py")
        return False
    if paper:
        return True
    if not os.getenv("POLY_PRIVATE_KEY", "").startswith("0x"):
        print("[PREFLIGHT] POLY_PRIVATE_KEY missing")
        return False
    if not os.getenv("POLY_FUNDER", "").startswith("0x"):
        print("[PREFLIGHT] POLY_FUNDER missing")
        return False
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true")
    args = ap.parse_args()

    if not preflight(args.paper):
        sys.exit(1)

    try:
        asyncio.run(main_loop(args.paper))
    except KeyboardInterrupt:
        notify("stopped (SIGINT)", tag="arb3")
        print("\n[arb3] stopped")
