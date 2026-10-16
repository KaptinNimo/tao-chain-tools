#!/usr/bin/env python3
"""Maany's subnet watch: read everything straight from the Bittensor chain.
Usage: python3 subnet_reader.py <coldkey> <netuid,netuid,...> [days=35] > chain.json
Needs: pip install bittensor ; network access to *.opentensor.ai
Output JSON: wallet balances, and per subnet: name, price now, TAO in pool,
24h net TAO flow, completed daily closes (00:00 UTC prices), and the
indicators the watch rules use (7d/30d %, MA10, MA20, MA10 slope, volatility, exit line).
"""
import sys, json, statistics as stat, datetime as dt, warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.WARNING)
import bittensor as bt

RAO = 1e9
BLOCK_S = 12

def main():
    ck = sys.argv[1]
    nets = [int(x) for x in sys.argv[2].split(",") if x]
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 35
    live = bt.Subtensor(network="finney")
    arch = bt.Subtensor(network="archive")
    S = bt.storage.SubtensorModule

    cur = live.block
    now = live.timestamp().replace(tzinfo=dt.timezone.utc) if live.timestamp().tzinfo is None else live.timestamp()

    # ---- wallet ----
    prices_now = live.prices.alpha_prices()
    names = live.subnets.subnet_names()
    free = live.balances.balance(ck).rao / RAO
    pos = {}
    for p in live.staking.positions(ck):
        a = p.stake.rao / RAO
        if a <= 0:
            continue
        d = pos.setdefault(p.netuid, {"alpha": 0.0, "hotkeys": []})
        d["alpha"] += a
        d["hotkeys"].append(p.hotkey)
    positions = []
    root = 0.0
    for n, d in sorted(pos.items()):
        val = d["alpha"] * prices_now[n]
        if n == 0:
            root = val
        positions.append({"netuid": n, "name": names.get(n), "balance_alpha": d["alpha"],
                          "balance_tao": val, "hotkeys": d["hotkeys"]})
    alpha_val = sum(p["balance_tao"] for p in positions if p["netuid"] != 0)
    wallet = {"free_tao": free, "staked_root_tao": root, "staked_alpha_tao": alpha_val,
              "total_tao": free + root + alpha_val, "positions": positions}

    # ---- find the block at each 00:00 UTC (completed daily closes) ----
    def ts(b):
        t = arch.timestamp(block=b)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

    def block_at(target, guess):
        b = guess
        for _ in range(4):
            diff = (ts(b) - target).total_seconds()
            if abs(diff) < BLOCK_S:
                break
            b -= int(round(diff / BLOCK_S))
        return min(b, cur)

    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # close of day D = price at 00:00 UTC of D+1. Today's 00:00 = close of yesterday (last completed).
    targets = [today0 - dt.timedelta(days=k) for k in range(days - 1, -1, -1)]
    closes = {n: [] for n in nets}
    guess = cur - int((now - targets[0]).total_seconds() / BLOCK_S)
    for t in targets:
        b = block_at(t, guess)
        pr = arch.prices.alpha_prices(block=b)
        day = (t - dt.timedelta(days=1)).date().isoformat()
        for n in nets:
            if n in pr and pr[n]:
                closes[n].append([day, pr[n]])
        guess = b + 7200

    # ---- pool depth and 24h net TAO flow (pool change minus emission injected) ----
    b24 = block_at(now - dt.timedelta(hours=24), cur - 7200)
    tao_now = live.query_batch(S.SubnetTAO, [[n] for n in nets])
    tao_24 = arch.query_batch(S.SubnetTAO, [[n] for n in nets], block=b24)
    inj = live.query_batch(S.SubnetTaoInEmission, [[n] for n in nets])

    # ---- chain buying screen (all subnets) ----
    # chain buys/day = TAO the protocol uses to buy alpha (SubnetExcessTao per block x 7200)
    # miner emission/day = alpha paid to miners (41% of alpha out) x price
    alln = sorted(n for n in prices_now if n != 0)
    exc = live.query_batch(S.SubnetExcessTao, [[n] for n in alln])
    aout = live.query_batch(S.SubnetAlphaOutEmission, [[n] for n in alln])
    tin = live.query_batch(S.SubnetTAO, [[n] for n in alln])
    screen = {}
    for i, n in enumerate(alln):
        buys = exc[i] * 7200 / RAO
        miner = aout[i] * 7200 * 0.41 / RAO * prices_now[n]
        liq = 2 * tin[i] / RAO
        screen[n] = {"chain_buys_day": buys, "miner_emission_day": miner,
                     "ratio": buys / miner if miner else None, "liquidity": liq,
                     "daily_impact_pct": (buys - miner) / liq * 100 if liq else None}
    tb = sum(v["chain_buys_day"] for v in screen.values())
    tm = sum(v["miner_emission_day"] for v in screen.values())
    cands = sorted([dict(netuid=n, name=names.get(n), **v) for n, v in screen.items()
                    if v["ratio"] and v["ratio"] >= 1.1 and v["liquidity"] >= 20000],
                   key=lambda r: -r["daily_impact_pct"])
    out = {"as_of_utc": now.isoformat(), "block": cur, "wallet": wallet,
           "network": {"chain_buys_day": tb, "miner_emission_day": tm, "network_ratio": tb / tm},
           "candidates": cands[:10], "subnets": {}}
    for i, n in enumerate(nets):
        c = [x[1] for x in closes[n]]
        r = {"name": names.get(n), "price": prices_now.get(n),
             "tao_in_pool": tao_now[i] / RAO,
             "net_flow_24h_tao": (tao_now[i] - tao_24[i]) / RAO - (cur - b24) * inj[i] / RAO,
             "closes": closes[n], **{k: screen.get(n, {}).get(k) for k in ("ratio", "liquidity", "daily_impact_pct", "chain_buys_day", "miner_emission_day")}}
        if len(c) >= 31:
            ma = lambda k, e: sum(c[e - k:e]) / k
            L = len(c)
            ma10, ma20, ma10_5 = ma(10, L), ma(20, L), ma(10, L - 5)
            ch = [c[j] / c[j - 1] - 1 for j in range(L - 10, L)]
            vol = stat.stdev(ch)
            r.update({"ma10": ma10, "ma20": ma20, "ma10_slope": ma10 / ma10_5 - 1,
                      "volatility": vol, "exit_line": ma10 * (1 - vol),
                      "chg_7d": c[-1] / c[-8] - 1, "chg_30d": c[-1] / c[-31] - 1,
                      "last2_closes": c[-2:]})
        out["subnets"][n] = r
    json.dump(out, sys.stdout, indent=1, default=str)

if __name__ == "__main__":
    main()
