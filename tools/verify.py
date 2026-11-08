"""Score FT8 decodes against PSK Reporter spots captured during the same session.

  python3 tools/verify.py                                 # jt9 baseline (recordings/*/decodes.txt)
  python3 tools/verify.py --decodes ../ft8-nn/data/val --tag nn_v2   # <root>/<station>/decodes_<tag>.txt
  python3 tools/verify.py --tag nn_v2 --decodes ... --list   # also print every unconfirmed decode

Accepts jt9 lines ("<slot> <hhmmss> <snr> <dt> <freq> ~ <text>") and ft8nn tab lines
("<slot>\\t<f0>\\t<dt>\\t<method>\\t<text>[\\t...]").

For each decode the *transmitting* call is extracted (2nd call of a standard message, or the call
after CQ [modifier]). Tiers, strongest first:
  slot    - someone else spotted that call on this band in this exact 15 s slot, within FREQ_TOL Hz
  active  - call spotted on this band within +/- ACTIVE_WIN s (real station, slot/freq not matched)
  repeat  - not spotted, but this decoder heard the same call in >= 2 slots on this receiver
            (random false decodes don't repeat a callsign; usually a station only local reporters hear)
  unconf  - call never spotted on this band during the session, decoded once
  n/a     - no checkable transmitter call (hashed <...> sender, free text, telemetry)
jt9 has a very low false-decode rate, so its 'unconf' share measures PSK Reporter coverage gaps;
a decoder whose 'unconf' share is well above jt9's is producing false decodes.
Writes <station>/verified_<tag>.tsv next to the decodes.
"""
import argparse, json, os, re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FREQ_TOL = 50       # Hz, after removing this receiver's median offset (reporters differ too)
ACTIVE_WIN = 600    # s
CQ_MODS = re.compile(r"^([A-Z]{1,4}|\d{3})$")  # CQ DX / CQ POTA / CQ NA / CQ 123 ...
CALL = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}$")


def base_call(c):
    """W1ABC/P -> W1ABC, EA8/DL1XYZ -> DL1XYZ (the part that looks most like a callsign)."""
    c = c.strip("<>").upper()
    parts = [p for p in c.split("/") if CALL.match(p)]
    return max(parts, key=len) if parts else None


def sender(text):
    w = text.split()
    if not w:
        return None
    if w[0] == "CQ":
        rest = w[1:]
        if len(rest) >= 2 and CQ_MODS.match(rest[0]) and not CALL.match(rest[0]):
            rest = rest[1:]
        return base_call(rest[0]) if rest and "<" not in rest[0] else None
    if ";" in text or len(w) < 2 or "<" in w[1]:
        return None  # DXpedition/hashed sender: not checkable
    return base_call(w[1])


def norm_text(t):
    return re.sub(r"(\s+(\?|a\d))+\s*$", "", t.strip()).strip()


def parse_decodes(path):
    out = []
    for line in open(path):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if "\t" in line:
            p = line.split("\t")
            out.append((p[0], float(p[1]), norm_text(p[4])))
        else:
            m = re.match(r"(\d{6}_\d{6})\s+\d{6}\s+(-?\d+)\s+(-?[\d.]+)\s+(\d+)\s+~\s+(.*)$", line)
            if m:
                out.append((m.group(1), float(m.group(4)), norm_text(m.group(5))))
    return out


def slot_t(slot):
    return datetime.strptime(slot, "%y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()


def load_spots(path, bands):
    """index[(band, call)] -> sorted list of (t_tx, f_hz, reporter)"""
    idx = defaultdict(list)
    reporters = defaultdict(set)
    for line in open(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        b = d.get("b")
        if b not in bands:
            continue
        c = base_call(d.get("sc", ""))
        if not c:
            continue
        ttx = d.get("t_tx") or (d["t"] - 13) // 15 * 15
        idx[(b, c)].append((int(ttx), d["f"], d.get("rc", "")))
        reporters[b].add(d.get("rc", ""))
    for v in idx.values():
        v.sort()
    return idx, reporters


T_TOL = 2           # s; some reporters' clocks are a little off


def same_slot(idx, band, call, t):
    lst = idx.get((band, call), ())
    return lst[bisect_left(lst, (t - T_TOL,)):bisect_right(lst, (t + T_TOL, float("inf")))]


def freq_offset(idx, band, decs, dial):
    """Median (spot freq - our freq) over same-slot matches: absorbs this Kiwi's calibration error."""
    deltas = []
    for slot, f0, text in decs:
        call = sender(text)
        if call:
            near = [ff - (dial + f0) for _, ff, _ in same_slot(idx, band, call, slot_t(slot)) if abs(ff - dial - f0) < 400]
            if near:
                deltas.append(min(near, key=abs))
    deltas.sort()
    return deltas[len(deltas) // 2] if len(deltas) >= 5 else 0.0


def classify(idx, band, call, t, f_hz):
    lst = idx.get((band, call))
    if not lst:
        return "unconf", 0
    lo, hi = bisect_left(lst, (t - T_TOL,)), bisect_right(lst, (t + T_TOL, float("inf")))
    hits = {r for tt, ff, r in lst[lo:hi] if abs(ff - f_hz) <= FREQ_TOL}
    if hits:
        return "slot", len(hits)
    lo, hi = bisect_left(lst, (t - ACTIVE_WIN,)), bisect_right(lst, (t + ACTIVE_WIN, float("inf")))
    return ("active", hi - lo) if hi > lo else ("unconf", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recordings", default=os.path.join(ROOT, "recordings"))
    ap.add_argument("--spots", default=os.path.join(ROOT, "pskr_spots.jsonl"))
    ap.add_argument("--decodes", help="root holding <station>/decodes_<tag>.txt (default: recordings, jt9)")
    ap.add_argument("--tag", default="jt9")
    ap.add_argument("--list", action="store_true", help="print unconfirmed decodes")
    a = ap.parse_args()

    stations = sorted(d for d in os.listdir(a.recordings) if os.path.isdir(os.path.join(a.recordings, d)))
    metas = {s: json.load(open(os.path.join(a.recordings, s, "meta.json"))) for s in stations}
    idx, reporters = load_spots(a.spots, {m["band"] for m in metas.values()})
    print(f"spots indexed: {sum(map(len, idx.values()))} for {len(idx)} (band, call) pairs; "
          f"reporters per band: " + ", ".join(f"{b}={len(r)}" for b, r in sorted(reporters.items())))

    tiers = ["slot", "active", "repeat", "unconf", "n/a"]
    print(f"\n{'station':42s} {'band':>4s} {'dec':>5s} " + " ".join(f"{t:>7s}" for t in tiers) + "  conf%")
    grand, unconf_list, offsets = Counter(), [], {}
    for s in stations:
        m = metas[s]
        droot = a.decodes or a.recordings
        path = os.path.join(droot, s, "decodes.txt" if a.tag == "jt9" and not a.decodes else f"decodes_{a.tag}.txt")
        if not os.path.exists(path):
            print(f"{s:42s} (no {os.path.relpath(path, ROOT)})")
            continue
        decs = parse_decodes(path)
        dial = m["freq_khz"] * 1000
        off = freq_offset(idx, m["band"], decs, dial)
        dial += off
        offsets[s] = off
        cnt, rows, seen = Counter(), [], set()
        slots_by_call = defaultdict(set)
        for slot, f0, text in decs:
            c = sender(text)
            if c:
                slots_by_call[c].add(slot)
        for slot, f0, text in decs:
            if (slot, text) in seen:  # some decoders emit the same message twice per slot
                continue
            seen.add((slot, text))
            call = sender(text)
            tier, n = ("n/a", 0) if not call else classify(idx, m["band"], call, slot_t(slot), dial + f0)
            if tier == "unconf" and len(slots_by_call[call]) >= 2:
                tier, n = "repeat", len(slots_by_call[call])
            cnt[tier] += 1
            rows.append(f"{slot}\t{f0:.1f}\t{text}\t{call or ''}\t{tier}\t{n}")
            if tier == "unconf":
                unconf_list.append((s, slot, f0, text))
        with open(os.path.join(os.path.dirname(path), f"verified_{a.tag}.tsv"), "w") as fh:
            fh.write("slot\tfreq\ttext\tsender\ttier\tn_reporters_or_spots\n" + "\n".join(rows) + "\n")
        tot = sum(cnt.values())
        chk = tot - cnt["n/a"]
        conf = (chk - cnt["unconf"]) / chk if chk else 0
        print(f"{s:42s} {m['band']:>4s} {tot:5d} " + " ".join(f"{cnt[t]:7d}" for t in tiers) + f"  {conf:5.1%}")
        grand.update(cnt)
    tot = sum(grand.values())
    chk = tot - grand["n/a"]
    print(f"{'TOTAL':42s} {'':4s} {tot:5d} " + " ".join(f"{grand[t]:7d}" for t in tiers)
          + f"  {(chk - grand['unconf']) / max(chk, 1):5.1%}")
    print(f"\nof checkable decodes: slot-confirmed {grand['slot'] / max(chk, 1):.1%}, "
          f"active-only {grand['active'] / max(chk, 1):.1%}, repeat-only {grand['repeat'] / max(chk, 1):.1%}, unconfirmed {grand['unconf'] / max(chk, 1):.1%}")
    print("per-receiver freq offset vs spots (Hz): " + ", ".join(f"{k[:2]}={v:+.0f}" for k, v in offsets.items()))
    if a.list:
        print("\nunconfirmed:")
        for s, slot, f0, text in unconf_list:
            print(f"  {s[:30]:30s} {slot} {f0:7.1f}  {text}")


if __name__ == "__main__":
    main()
