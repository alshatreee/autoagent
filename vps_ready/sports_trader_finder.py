"""
sports_trader_finder.py

Scans Polymarket leaderboard/positions to identify top sports traders,
then writes smart_wallets.json for sports_consensus_bot.py to consume.

Strategy:
    1. Pull recent resolved sports markets
    2. For each, fetch winners (positions with realized profit)
    3. Rank addresses by: sports_roi, sports_trades, win_rate
    4. Keep top N
    5. Persist as smart_wallets.json

Run:
    python3 sports_trader_finder.py

Output: smart_wallets.json in the current directory

This is best run weekly (via cron) to keep the wallet list fresh.
"""

import asyncio
import aiohttp
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

TOP_N            = 10
MIN_SPORTS_TRADES = 10
MIN_ROI          = 0.15
MAX_MARKETS_SCAN = 200
SPORTS_TAGS      = {"sports", "nba", "nfl", "mlb", "nhl", "soccer",
                    "football", "basketball", "baseball", "hockey",
                    "ufc", "boxing", "tennis", "golf", "cricket",
                    "esports", "olympics", "champions-league", "premier-league"}

OUTPUT_FILE = os.getenv("SMART_WALLETS_FILE", "smart_wallets.json")


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


async def fetch_json(session, url, params=None):
    try:
        async with session.get(
            url, params=params or {},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            print(f"[WARN] HTTP {resp.status} {url}")
            return None
    except Exception as e:
        print(f"[ERROR] {url}: {e}")
        return None


def is_sports_market(m: dict) -> bool:
    tags = m.get("tags") or m.get("category") or m.get("categories") or []
    if isinstance(tags, str):
        tags = [tags]
    tag_set = {str(t).lower() for t in tags}
    return bool(tag_set & SPORTS_TAGS)


async def fetch_recent_sports_markets(session) -> list[dict]:
    """Recently resolved sports markets (for picking winners)."""
    data = await fetch_json(session, f"{GAMMA_API}/markets", {
        "closed": "true",
        "limit":  MAX_MARKETS_SCAN,
        "order":  "endDate",
        "ascending": "false",
    })
    if not isinstance(data, list):
        return []
    return [m for m in data if is_sports_market(m)]


async def fetch_market_positions(session, condition_id: str) -> list[dict]:
    """All positions for a given market (winners + losers)."""
    data = await fetch_json(session, f"{DATA_API}/positions", {
        "market": condition_id,
        "limit":  500,
    })
    return data if isinstance(data, list) else []


@dataclass
class WalletStats:
    address: str = ""
    trades:  int = 0
    wins:    int = 0
    total_pnl: float = 0.0
    total_cost: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def roi(self) -> float:
        return self.total_pnl / self.total_cost if self.total_cost > 0 else 0.0


async def build_wallet_stats(session) -> list[WalletStats]:
    print(f"[scan] fetching recent sports markets (max {MAX_MARKETS_SCAN})...")
    markets = await fetch_recent_sports_markets(session)
    print(f"[scan] {len(markets)} sports markets found")

    stats: dict[str, WalletStats] = defaultdict(lambda: WalletStats())

    for idx, m in enumerate(markets):
        cid = _s(m, "conditionId", "condition_id")
        if not cid:
            continue
        positions = await fetch_market_positions(session, cid)
        for p in positions:
            addr = _s(p, "user", "address", "wallet").lower()
            if not addr:
                continue
            cost = _f(p, "initialValue", "cost", "costBasis")
            pnl  = _f(p, "realizedPnl", "cashPnl", "realized")
            if cost <= 0:
                continue
            ws = stats[addr]
            ws.address = addr
            ws.trades  += 1
            ws.total_pnl += pnl
            ws.total_cost += cost
            if pnl > 0:
                ws.wins += 1

        if (idx + 1) % 10 == 0:
            print(f"[scan] processed {idx+1}/{len(markets)} markets")
        await asyncio.sleep(0.5)  # gentle on API

    return list(stats.values())


def rank_and_filter(stats: list[WalletStats]) -> list[WalletStats]:
    qualified = [
        s for s in stats
        if s.trades >= MIN_SPORTS_TRADES and s.roi >= MIN_ROI
    ]
    qualified.sort(key=lambda s: (s.roi, s.win_rate, s.trades), reverse=True)
    return qualified[:TOP_N]


def save(wallets: list[WalletStats], path: str):
    payload = {
        "generated_at": int(time.time()),
        "count":        len(wallets),
        "criteria": {
            "min_sports_trades": MIN_SPORTS_TRADES,
            "min_roi":            MIN_ROI,
            "top_n":              TOP_N,
        },
        "wallets": [
            {
                "address":  w.address,
                "trades":   w.trades,
                "wins":     w.wins,
                "win_rate": round(w.win_rate, 4),
                "roi":      round(w.roi, 4),
                "total_pnl": round(w.total_pnl, 2),
            }
            for w in wallets
        ],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[save] wrote {len(wallets)} wallets to {path}")


async def main():
    async with aiohttp.ClientSession() as session:
        raw = await build_wallet_stats(session)
        print(f"[rank] {len(raw)} unique wallets seen")
        top = rank_and_filter(raw)
        print(f"[rank] {len(top)} qualified (>= {MIN_SPORTS_TRADES} trades, "
              f">= {MIN_ROI:.0%} ROI)")

        if not top:
            print("[rank] no wallets qualified — loosen thresholds or scan more markets")
            return

        print("\nTop sports traders:")
        for i, w in enumerate(top, 1):
            print(f"  {i:2d}. {w.address}  trades={w.trades:3d}  "
                  f"wins={w.wins:3d}  win_rate={w.win_rate:.1%}  "
                  f"roi={w.roi:+.1%}  pnl=${w.total_pnl:+,.0f}")

        save(top, OUTPUT_FILE)


if __name__ == "__main__":
    asyncio.run(main())
