#!/usr/bin/env python3
"""Wallet watch, straight from the Bittensor chain (no Taostats).
Usage: python3 wallet_watch.py <watched_coldkey> <start_unix> <end_unix> [my_coldkey]
Prints JSON: {"window":..., "events":[...], "my_held":[netuids], "names":{netuid:name}}
Needs: pip install substrate-interface ; network access to *.opentensor.ai
"""
import sys, json, threading, queue, warnings, logging
warnings.filterwarnings("ignore"); logging.disable(logging.WARNING)
from substrateinterface import SubstrateInterface

URL = "wss://archive.chain.opentensor.ai:443"
RAO = 1e9
THREADS = 6
STAKE_EVENTS = {"StakeAdded", "StakeRemoved", "StakeMoved", "StakeTransferred",
                "StakeSwapped", "StakeAndHotkeyTransferred"}

def conn():
    return SubstrateInterface(url=URL)

def block_ts(s, b):
    return s.query("Timestamp", "Now", block_hash=s.get_block_hash(b)).value / 1000

def block_at(s, target, head, head_ts):
    b = head - int((head_ts - target) / 12)
    for _ in range(5):
        d = block_ts(s, b) - target
        if abs(d) < 12:
            break
        b -= int(round(d / 12))
    return max(1, min(b, head))

def acct(x):
    return x if isinstance(x, str) else str(x)

def scan(blocks, ck, out, errs):
    try:
        s = conn()
    except Exception as e:
        errs.append(repr(e)); return
    while True:
        try:
            b = blocks.get_nowait()
        except queue.Empty:
            return
        for attempt in range(3):
            try:
                h = s.get_block_hash(b)
                ts = s.query("Timestamp", "Now", block_hash=h).value / 1000
                evs = s.get_events(h)
                # signed extrinsics by this coldkey (catches failed / other actions)
                blk = s.get_block(block_hash=h)
                mine = {}
                for i, ex in enumerate(blk["extrinsics"]):
                    v = ex.value
                    if v.get("address") and acct(v["address"]) == ck:
                        c = v.get("call", {})
                        mine[i] = f'{c.get("call_module")}.{c.get("call_function")}'
                ok = {}
                for e in evs:
                    v = e.value
                    idx = v.get("extrinsic_idx")
                    if v["module_id"] == "System" and idx in mine:
                        ok[idx] = v["event_id"] == "ExtrinsicSuccess"
                    a = v["attributes"]
                    ev = v["event_id"]
                    rec = None
                    if v["module_id"] == "Balances" and ev == "Transfer" and ck in (a["from"], a["to"]):
                        rec = {"kind": "transfer", "direction": "out" if a["from"] == ck else "in",
                               "from": a["from"], "to": a["to"], "amount_tao": a["amount"] / RAO}
                    elif v["module_id"] == "SubtensorModule" and ev in STAKE_EVENTS:
                        if ev in ("StakeAdded", "StakeRemoved") and a[0] == ck:
                            rec = {"kind": "stake" if ev == "StakeAdded" else "unstake", "hotkey": a[1],
                                   "amount_tao": a[2] / RAO, "alpha": a[3] / RAO, "netuid": a[4]}
                        elif ev == "StakeMoved" and a[0] == ck:
                            rec = {"kind": "move", "from_hotkey": a[1], "from_netuid": a[2],
                                   "hotkey": a[3], "netuid": a[4], "amount_tao": a[5] / RAO}
                        elif ev == "StakeSwapped" and a[0] == ck:
                            rec = {"kind": "swap", "hotkey": a[1], "from_netuid": a[2],
                                   "netuid": a[3], "amount_tao": a[4] / RAO}
                        elif ev == "StakeTransferred" and ck in (a[0], a[1]):
                            rec = {"kind": "stake_transfer", "direction": "out" if a[0] == ck else "in",
                                   "from": a[0], "to": a[1], "hotkey": a[2], "netuid": a[4],
                                   "amount_tao": a[5] / RAO}
                        elif ev == "StakeAndHotkeyTransferred" and ck in (a["origin_coldkey"], a["destination_coldkey"]):
                            rec = {"kind": "stake_transfer", "direction": "out" if a["origin_coldkey"] == ck else "in",
                                   "from": a["origin_coldkey"], "to": a["destination_coldkey"],
                                   "hotkey": a["destination_hotkey"], "netuid": a["destination_netuid"],
                                   "amount_tao": a["amount"] / RAO}
                    if rec:
                        rec.update({"block": b, "timestamp": ts, "extrinsic_idx": idx})
                        out.append(rec)
                for i, call in mine.items():
                    out.append({"kind": "extrinsic", "call": call, "success": ok.get(i, True),
                                "block": b, "timestamp": ts, "extrinsic_idx": i})
                break
            except Exception as e:
                if attempt == 2:
                    errs.append(f"block {b}: {e!r}")
                try:
                    s = conn()
                except Exception:
                    pass

def main():
    ck, start, end = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    me = sys.argv[4] if len(sys.argv) > 4 else None
    s = conn()
    head = s.get_block_number(None)
    head_ts = block_ts(s, head)
    b0 = block_at(s, start, head, head_ts)
    b1 = block_at(s, end, head, head_ts)
    q = queue.Queue()
    for b in range(b0, b1 + 1):
        q.put(b)
    out, errs = [], []
    th = [threading.Thread(target=scan, args=(q, ck, out, errs)) for _ in range(THREADS)]
    [t.start() for t in th]; [t.join() for t in th]
    out.sort(key=lambda r: (r["block"], r.get("extrinsic_idx") or 0))
    # names for subnets and validators involved
    names, vnames = {}, {}
    nets = {r.get("netuid") for r in out} | {r.get("from_netuid") for r in out}
    my_held = []
    if me and out:
        for hk in (s.query("SubtensorModule", "StakingHotkeys", [me]).value or []):
            for k, v in s.query_map("SubtensorModule", "AlphaV2", [hk, me]):
                n = k.value if not isinstance(k.value, (list, tuple)) else k.value[-1]
                m = v.value.get("mantissa") if isinstance(v.value, dict) else v.value
                if n and m:
                    my_held.append(int(n))
        my_held = sorted(set(my_held))
        nets |= set(my_held)
    for n in nets:
        if n is None:
            continue
        idt = s.query("SubtensorModule", "SubnetIdentitiesV3", [n]).value
        names[n] = (idt or {}).get("subnet_name") if idt else None
    for hk in {r.get("hotkey") for r in out if r.get("hotkey")}:
        try:
            owner = s.query("SubtensorModule", "Owner", [hk]).value
            idt = s.query("SubtensorModule", "IdentitiesV2", [owner]).value
            vnames[hk] = (idt or {}).get("name") or hk[:6] + "…"
        except Exception:
            vnames[hk] = hk[:6] + "…"
    for r in out:
        if r.get("hotkey"):
            r["validator"] = vnames.get(r["hotkey"])
    json.dump({"window": {"start_block": b0, "end_block": b1, "blocks": b1 - b0 + 1},
               "events": out, "my_held": my_held, "names": names, "errors": errs},
              sys.stdout, indent=1, default=str)

if __name__ == "__main__":
    main()
