"""
Polymarket Whale Scorer

Standalone tool to evaluate whale candidates BEFORE adding them to
polymarket_bot.py WHALES list. Protects you from survivorship bias and
lucky-streak whales.

Usage:
    1. Edit CANDIDATES below with addresses you want to evaluate
    2. python whale_scorer.py
    3. Review the scorecard — only use whales that score >= MIN_SCORE
    4. Copy passing addresses into polymarket_bot.py WHALES list

How scoring works:
    Each whale gets checked against 7 criteria. Each criterion is pass/fail.
    A whale must pass at least MIN_SCORE (default 5/7) to be recommended.

    Criteria:
      1. ROI   — realized+unrealized PnL / total cost basis >= MIN_ROI
      2. TRADES — at least MIN_TRADES distinct markets touched
      3. WIN%  — win rate on closed positions >= MIN_WIN_RATE
      4. DD    — max observed drawdown from peak PnL <= MAX_DRAWDOWN
      5. DIV   — traded in at least MIN_CATEGORIES different market tags
      6. ACTV  — at least one trade in the last RECENT_DAYS days
      7. SIZE  — typical trade size within SANE_SIZE_RANGE (not a whale whale)

IMPORTANT: This uses best-effort parsing of the public Polymarket data API.
The API is not formally documented; field names may change. If a criterion
reports "N/A", it could not be computed from the response — treat as FAIL
conservatively.

Requires: aiohttp
Install:  pip install aiohttp
"""

import asyncio
import csv
import aiohttp
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
EXPORT_CSV = "whale_scores.csv"
EXPORT_JSON = "whale_scores.json"

# ═══════════════════════════════════════════════════════
# CANDIDATES — edit this list
# ═══════════════════════════════════════════════════════

CANDIDATES = [
    "0xaad03b403c831d0a8b484abe59ac25188b49f61d",  # Fredi9999 (example)
    # add more candidate addresses here
]

# ═══════════════════════════════════════════════════════
# SCORING THRESHOLDS
# ═══════════════════════════════════════════════════════

MIN_ROI          = 0.20      # 20% total ROI
MIN_TRADES       = 50        # at least 50 distinct markets
MIN_WIN_RATE     = 0.55      # 55% win rate on closed positions
MAX_DRAWDOWN     = 0.40      # max drawdown <= 40%
MIN_CATEGORIES   = 5         # at least 5 different market tags
RECENT_DAYS      = 7         # active in last 7 days
SANE_SIZE_MIN    = 1_000     # typical trade >= $1K
SANE_SIZE_MAX    = 200_000   # typical trade <= $200K (avoid mega funds)

MIN_SCORE        = 5         # out of 7 to recommend

# ═══════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════

def _f(d: dict, *keys, default=0.0) -> float:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _s(d: dict, *keys, default="") -> str:
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


# ═══════════════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════════════

async def fetch_positions(session, address: str) -> list[dict]:
    """All positions (open + closed) for a user."""
    data = await fetch_json(session, f"{DATA_API}/positions", {
        "user":  address,
        "limit": 500,
    })
    return data if isinstance(data, list) else []


async def fetch_activity(session, address: str) -> list[dict]:
    """Recent wallet activity for a user."""
    data = await fetch_json(session, f"{DATA_API}/activity", {
        "user":  address,
        "limit": 500,
    })
    return data if isinstance(data, list) else []


async def fetch_value(session, address: str) -> dict:
    """Portfolio value summary (if available)."""
    data = await fetch_json(session, f"{DATA_API}/value", {"user": address})
    return data if isinstance(data, dict) else {}


# ═══════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════

@dataclass
class WhaleScore:
    address:     str
    wallet_type: str              = "trader"
    roi:         Optional[float] = None
    net_cashflow: Optional[float] = None
    total_buys:  Optional[float] = None
    total_sells: Optional[float] = None
    total_redeemed: Optional[float] = None
    total_rebates: Optional[float] = None
    total_trades: Optional[int]  = None
    win_rate:    Optional[float] = None
    max_dd:      Optional[float] = None
    categories:  Optional[int]   = None
    last_trade_age_days: Optional[float] = None
    median_size: Optional[float] = None
    criteria:    dict            = field(default_factory=dict)
    score:       int             = 0
    recommended: bool            = False


def _activity_type(a: dict) -> str:
    return _s(a, "type", "eventType", "activityType").upper()


def _activity_side(a: dict) -> str:
    return _s(a, "side", "orderSide", "direction").upper()


def classify_wallet(activity: list[dict]) -> str:
    total = len(activity)
    if not total:
        return "trader"

    redeem_count = sum(1 for a in activity if _activity_type(a) == "REDEEM")
    rebate_count = sum(1 for a in activity if _activity_type(a) == "MAKER_REBATE")
    if redeem_count + rebate_count >= max(1, int(total * 0.2)) or rebate_count >= 3:
        return "market_maker"
    return "trader"


def score_whale(address: str, positions: list[dict],
                activity: list[dict], value: dict) -> WhaleScore:
    w = WhaleScore(address=address)
    w.wallet_type = classify_wallet(activity)

    # ── 1. ROI ──
    total_cost, total_value, realized = 0.0, 0.0, 0.0
    for p in positions:
        cost   = _f(p, "initialValue", "cost", "costBasis")
        curval = _f(p, "currentValue", "value", "currentVal")
        rpnl   = _f(p, "realizedPnl", "cashPnl", "realized")
        total_cost  += cost
        total_value += curval
        realized    += rpnl

    if total_cost > 0:
        # ROI = (current value + realized pnl - initial cost) / initial cost
        w.roi = (total_value + realized - total_cost) / total_cost

    # Activity-level cashflow tracking for market maker wallets
    total_buys, total_sells, total_redeemed, total_rebates = 0.0, 0.0, 0.0, 0.0
    for a in activity:
        amount = _f(a, "usdcSize", "size", "amount", "value")
        if amount <= 0:
            continue
        kind = _activity_type(a)
        side = _activity_side(a)
        if kind == "TRADE":
            if side in ("SELL", "ASK"):
                total_sells += amount
            else:
                total_buys += amount
        elif kind == "REDEEM":
            total_redeemed += amount
        elif kind == "MAKER_REBATE":
            total_rebates += amount

    w.total_buys = total_buys if total_buys > 0 else None
    w.total_sells = total_sells if total_sells > 0 else None
    w.total_redeemed = total_redeemed if total_redeemed > 0 else None
    w.total_rebates = total_rebates if total_rebates > 0 else None

    if w.wallet_type == "market_maker":
        # For MM wallets, ROI is cash-flow based, not position-value based.
        net_cashflow = total_redeemed + total_rebates + total_sells - total_buys
        w.net_cashflow = net_cashflow
        if total_buys > 0:
            w.roi = net_cashflow / total_buys
    else:
        w.net_cashflow = (total_value + realized - total_cost) if total_cost > 0 else None

    # ── 2. total trades (distinct markets touched) ──
    markets_seen = {_s(p, "conditionId", "condition_id") for p in positions}
    markets_seen.discard("")
    # activity-based count is more accurate
    activity_markets = {_s(a, "conditionId", "condition_id", "market")
                        for a in activity}
    activity_markets.discard("")
    w.total_trades = max(len(markets_seen), len(activity_markets))

    # ── 3. Win rate on closed/resolved positions ──
    closed = [p for p in positions if _f(p, "currentValue", "value") == 0
              or _s(p, "status").lower() in ("closed", "resolved")]
    if not closed:
        # fall back: any position with realizedPnl != 0
        closed = [p for p in positions
                  if _f(p, "realizedPnl", "cashPnl", "realized") != 0]
    if closed:
        wins = sum(1 for p in closed
                   if _f(p, "realizedPnl", "cashPnl", "realized") > 0)
        w.win_rate = wins / len(closed)

    # ── 4. Max drawdown from activity timeline ──
    # Approximate: walk through activity chronologically, tracking cumulative
    # realized PnL, and compute peak-to-trough drop.
    timeline = []
    for a in activity:
        ts  = _f(a, "timestamp", "time", "createdAt")
        pnl = _f(a, "realizedPnl", "pnl")
        if ts > 0:
            timeline.append((ts, pnl))
    if timeline:
        timeline.sort()
        cum, peak, max_dd = 0.0, 0.0, 0.0
        for _, pnl in timeline:
            cum += pnl
            if cum > peak:
                peak = cum
            drop = peak - cum
            if peak > 0 and (drop / peak) > max_dd:
                max_dd = drop / peak
        w.max_dd = max_dd

    # ── 5. Category diversity ──
    cats = set()
    for p in positions:
        tags = p.get("tags") or p.get("category") or p.get("categories")
        if isinstance(tags, list):
            cats.update(str(t).lower() for t in tags)
        elif isinstance(tags, str):
            cats.add(tags.lower())
    w.categories = len(cats) if cats else None

    # ── 6. Last activity ──
    timestamps = [_f(a, "timestamp", "time", "createdAt") for a in activity]
    timestamps = [t for t in timestamps if t > 0]
    if timestamps:
        last = max(timestamps)
        w.last_trade_age_days = (time.time() - last) / 86400

    # ── 7. Typical size (median) ──
    sizes = sorted([_f(a, "size", "amount", "usdcSize")
                    for a in activity if _f(a, "size", "amount", "usdcSize") > 0])
    if sizes:
        w.median_size = sizes[len(sizes) // 2]

    # ── Evaluate each criterion ──
    def check(name: str, condition: Optional[bool]) -> None:
        w.criteria[name] = condition  # None = N/A (fail conservatively)
        if condition is True:
            w.score += 1

    check("ROI",    w.roi is not None and w.roi >= MIN_ROI)
    check("TRADES", w.total_trades is not None and w.total_trades >= MIN_TRADES)
    check("WIN%",   w.win_rate is not None and w.win_rate >= MIN_WIN_RATE)
    check("DD",     w.max_dd is not None and w.max_dd <= MAX_DRAWDOWN)
    check("DIV",    w.categories is not None and w.categories >= MIN_CATEGORIES)
    check("ACTV",   w.last_trade_age_days is not None
                    and w.last_trade_age_days <= RECENT_DAYS)
    check("SIZE",   w.median_size is not None
                    and SANE_SIZE_MIN <= w.median_size <= SANE_SIZE_MAX)

    w.recommended = w.score >= MIN_SCORE
    return w


# ═══════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════

def fmt(v, pattern, na="N/A"):
    return pattern.format(v) if v is not None else na


def short_addr(addr: str) -> str:
    if len(addr) <= 12:
        return addr
    return f"{addr[:8]}…{addr[-4:]}"


def export_scores(scores: list[WhaleScore]) -> None:
    rows = []
    for s in scores:
        row = asdict(s)
        row["criteria"] = json.dumps(s.criteria, ensure_ascii=False, sort_keys=True)
        rows.append(row)

    json_path = Path(EXPORT_JSON)
    csv_path = Path(EXPORT_CSV)

    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def print_summary_table(scores: list[WhaleScore]) -> None:
    if not scores:
        return

    columns = [
        ("Address", lambda s: short_addr(s.address)),
        ("Type", lambda s: "MM" if s.wallet_type == "market_maker" else "TR"),
        ("Score", lambda s: f"{s.score}/7"),
        ("ROI", lambda s: fmt(s.roi, "{:+.1%}")),
        ("Net Cash", lambda s: fmt(s.net_cashflow, "${:+,.0f}")),
        ("Redeemed", lambda s: fmt(s.total_redeemed, "${:,.0f}")),
        ("Rebates", lambda s: fmt(s.total_rebates, "${:,.0f}")),
        ("Trades", lambda s: fmt(s.total_trades, "{}")),
        ("Win%", lambda s: fmt(s.win_rate, "{:.1%}")),
        ("DD", lambda s: fmt(s.max_dd, "{:.1%}")),
        ("Cats", lambda s: fmt(s.categories, "{}")),
        ("Recent", lambda s: fmt(s.last_trade_age_days, "{:.1f}d")),
        ("Size", lambda s: fmt(s.median_size, "${:,.0f}")),
        ("Rec", lambda s: "YES" if s.recommended else "NO"),
    ]

    rows = [[getter(s) for _, getter in columns] for s in scores]
    widths = []
    for idx, (label, _) in enumerate(columns):
        widths.append(max(len(label), max(len(row[idx]) for row in rows)))

    def line() -> None:
        print("  " + "  ".join("-" * w for w in widths))

    print("\n  SCORE TABLE")
    print("  " + "  ".join(label.ljust(widths[i]) for i, (label, _) in enumerate(columns)))
    line()
    for row in rows:
        print("  " + "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))


def print_scorecard(w: WhaleScore):
    mark = "RECOMMEND" if w.recommended else "REJECT   "
    print(f"\n{'='*60}")
    print(f"  {mark}  {w.address}")
    print(f"  mode:      {'MM' if w.wallet_type == 'market_maker' else 'TR'}")
    print(f"  score: {w.score}/7")
    print(f"{'-'*60}")
    print(f"  ROI:         {fmt(w.roi, '{:+.1%}'):>10}  (need >= {MIN_ROI:.0%})")
    print(f"  Net cash:    {fmt(w.net_cashflow, '${:+,.0f}'):>10}")
    print(f"  Redeemed:    {fmt(w.total_redeemed, '${:,.0f}'):>10}")
    print(f"  Rebates:     {fmt(w.total_rebates, '${:,.0f}'):>10}")
    print(f"  Trades:      {fmt(w.total_trades, '{}'):>10}  (need >= {MIN_TRADES})")
    print(f"  Win rate:    {fmt(w.win_rate, '{:.1%}'):>10}  (need >= {MIN_WIN_RATE:.0%})")
    print(f"  Max DD:      {fmt(w.max_dd, '{:.1%}'):>10}  (need <= {MAX_DRAWDOWN:.0%})")
    print(f"  Categories:  {fmt(w.categories, '{}'):>10}  (need >= {MIN_CATEGORIES})")
    print(f"  Last trade:  {fmt(w.last_trade_age_days, '{:.1f}d'):>10}  (need <= {RECENT_DAYS}d)")
    print(f"  Median size: {fmt(w.median_size, '${:,.0f}'):>10}  "
          f"(range ${SANE_SIZE_MIN:,}-${SANE_SIZE_MAX:,})")
    print(f"{'-'*60}")
    checks = "  ".join(
        f"{'PASS' if v is True else 'FAIL' if v is False else ' NA '}:{k}"
        for k, v in w.criteria.items()
    )
    print(f"  {checks}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

async def evaluate(session, addr: str) -> WhaleScore:
    positions, activity, value = await asyncio.gather(
        fetch_positions(session, addr),
        fetch_activity(session, addr),
        fetch_value(session, addr),
    )
    print(f"[FETCH] {addr}: {len(positions)} positions, {len(activity)} activities")
    return score_whale(addr, positions, activity, value)


async def main():
    if not CANDIDATES:
        print("Add addresses to CANDIDATES list and re-run.")
        return

    print(f"\nEvaluating {len(CANDIDATES)} whale candidate(s)...\n")
    async with aiohttp.ClientSession() as session:
        scores = []
        for addr in CANDIDATES:  # serial to be gentle on API
            try:
                s = await evaluate(session, addr)
                scores.append(s)
                print_scorecard(s)
            except Exception as e:
                print(f"[ERROR] {addr}: {e}")
            await asyncio.sleep(1)

    scores.sort(key=lambda s: s.score, reverse=True)
    export_scores(scores)

    print("\n" + "#" * 60)
    print("  SUMMARY")
    print("#" * 60)
    recommended = [s for s in scores if s.recommended]
    print(f"  {len(recommended)} / {len(scores)} candidates recommended "
          f"(score >= {MIN_SCORE}/7)\n")
    if recommended:
        print("  Copy these into polymarket_bot.py WHALES list:\n")
        for s in recommended:
            print(f'      "{s.address}",  # {s.wallet_type[:2].upper()} score {s.score}/7')
    else:
        print("  No candidates passed. Find better whales before going live.")
    print()

    print_summary_table(scores)
    print(f"\n  Exported: {EXPORT_JSON}, {EXPORT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
