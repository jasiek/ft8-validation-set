"""FT8 validation set: KiwiSDR recordings + simultaneous PSK Reporter spots as ground truth.

  python3 tools/val.py probe     -> re-probe known-good Kiwis (from ../ft8-training-data) on bands
                                    suited to their local time; writes selection.json
  RECEIVERS_JSON=rx.json OUTDIR=run2 python3 tools/val.py probe-fresh
                                 -> random receiverbook Kiwis, excluding training sites and earlier runs
                                    (N_RX receivers, default 10; PER_BAND probes per band, default 4)
  python3 tools/val.py record    -> 30 min of slot-aligned WAVs from every selected receiver, while
                                    capturing PSK Reporter MQTT spots for the same bands (+ upload tail)
  python3 tools/val.py decode    -> jt9 baseline decodes.txt + 30-min WAV per receiver
"""
import concurrent.futures as cf
import json, os, random, re, subprocess, sys, tempfile, threading, time, urllib.request, wave
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import kiwi, pskr

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE = os.path.abspath(os.environ.get("OUTDIR", ROOT))  # one folder per recording run
OUT = os.path.join(BASE, "recordings")
RECEIVERS_JSON = os.environ.get("RECEIVERS_JSON")  # flattened receiverbook.de/map list
SOURCE_SEL = os.path.join(ROOT, "..", "ft8-training-data", "selection.json")
MINUTES = int(os.environ.get("MINUTES", 30))
PSKR_TAIL_S = 12 * 60  # reporters batch uploads (up to ~5 min); keep listening after the last slot

BANDS = {"160m": 1840.0, "80m": 3573.0, "60m": 5357.0, "40m": 7074.0, "30m": 10136.0,
         "20m": 14074.0, "17m": 18100.0, "15m": 21074.0, "12m": 24915.0, "10m": 28074.0}

lock = threading.Lock()


def log(*a):
    with lock:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


def local_hour(lon):
    now = datetime.now(timezone.utc)
    return (now.hour + now.minute / 60 + lon / 15) % 24


def candidate_bands(lon, wide=False):
    h = local_hour(lon)
    if 8 <= h < 16:
        return ["20m", "17m", "15m", "10m"] + (["12m"] if wide else [])
    if 6 <= h < 8 or 16 <= h < 19:
        return ["20m", "30m", "40m", "17m"] + (["60m"] if wide else [])
    return ["40m", "80m", "30m", "20m"] + (["160m"] if wide else [])


def jt9_decode(wav_path):
    wav_path = os.path.abspath(wav_path)
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["jt9", "-8", "-d", "3", "-a", d, "-t", d, wav_path],
                           capture_output=True, text=True, cwd=d, timeout=120)
    # jt9 without a MyCall uses placeholder K1ABC; its MyCall-AP pass ("a2") then invents "K1ABC ..." decodes
    return [l.rstrip() for l in r.stdout.splitlines()
            if l.strip() and not l.startswith("<Decode") and "~  K1ABC " not in l]


def probe_rx(rx):
    """Probe candidate bands one after another (one Kiwi channel at a time). Returns [(band, n, sample)]."""
    res = []
    for band in candidate_bands(rx["lon"]):
        d = tempfile.mkdtemp(prefix="probe_")
        try:
            files = kiwi.record_slots(rx["url"], BANDS[band], d, time.time() + 35, max_slots=1,
                                      log=lambda *a: None)
            dec = jt9_decode(os.path.join(d, files[0])) if files else []
        except Exception as e:
            log(f"  {rx['url']} {band}: {e!r}")
            if isinstance(e, kiwi.KiwiError):
                return res  # busy/down: don't keep hammering it
            dec = []
        log(f"  {len(dec):3d} decodes  {band:4s} {rx['url']}  (local {local_hour(rx['lon']):.1f}h)")
        res.append((band, len(dec), dec[:5]))
    return res


def cmd_probe():
    pool = json.load(open(SOURCE_SEL))["chosen"]
    log(f"probing {len(pool)} receivers from {SOURCE_SEL}")
    with cf.ThreadPoolExecutor(len(pool)) as ex:
        results = list(ex.map(probe_rx, pool))
    # greedy: best (rx, band) pairs first, one band per receiver, at most 2 receivers per band
    pairs = sorted(((n, i, band, smp) for i, res in enumerate(results) for band, n, smp in res), reverse=True)
    chosen, used_rx, per_band = [], set(), {}
    for n, i, band, smp in pairs:
        if n < 3 or i in used_rx or per_band.get(band, 0) >= 2:
            continue
        used_rx.add(i)
        per_band[band] = per_band.get(band, 0) + 1
        rx = pool[i]
        chosen.append(dict(url=rx["url"], band=band, freq_khz=BANDS[band], station=rx["station"],
                           lat=rx["lat"], lon=rx["lon"], probe_decodes=n, probe_sample=smp))
    # receivers left over because of the per-band cap: give them their best band anyway
    for n, i, band, smp in pairs:
        if n >= 3 and i not in used_rx:
            used_rx.add(i)
            rx = pool[i]
            chosen.append(dict(url=rx["url"], band=band, freq_khz=BANDS[band], station=rx["station"],
                               lat=rx["lat"], lon=rx["lon"], probe_decodes=n, probe_sample=smp))
    json.dump(dict(probed_utc=datetime.now(timezone.utc).isoformat(), chosen=chosen),
              open(os.path.join(BASE, "selection.json"), "w"), indent=1)
    for c in chosen:
        log(f"chosen {c['band']:4s} {c['probe_decodes']:3d}  {c['url']}  {c['station'][:45]}")


def kiwi_status(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=8) as r:
            txt = r.read().decode("utf-8", "replace")
        return dict(l.split("=", 1) for l in txt.splitlines() if "=" in l)
    except Exception:
        return None


def status_ok(st):
    if not st or st.get("offline") != "no" or st.get("status") != "active":
        return False
    try:
        users, umax = int(st.get("users", 99)), int(st.get("users_max", 0))
        lo, hi = map(int, st.get("bands", "0-0").split("-"))
    except ValueError:
        return False
    return users < umax - 1 and hi >= 29_000_000 and lo <= 1_800_000  # leave a channel for humans


def probe_once(rx, band):
    d = tempfile.mkdtemp(prefix="probe_")
    try:
        info = {}
        files = kiwi.record_slots(rx["url"], BANDS[band], d, time.time() + 35, max_slots=1,
                                  log=lambda *a: None, info=info)
        for key in ("inactivity_timeout_mins", "ip_limit_mins"):
            v = info.get(key, 0) or 0
            if 0 < v < MINUTES + 10:
                return -1, [f"{key}={v}"]
        dec = jt9_decode(os.path.join(d, files[0])) if files else []
        return len(dec), dec[:5]
    except Exception as e:
        return 0, [repr(e)]


def cmd_probe_fresh(seed=None):
    """Random KiwiSDRs from receiverbook, excluding every site used in the training set or an earlier run."""
    n_wanted, per_band = int(os.environ.get("N_RX", 10)), int(os.environ.get("PER_BAND", 4))
    rng = random.Random(seed)
    used = json.load(open(SOURCE_SEL))["chosen"]
    for d in sorted(os.listdir(ROOT)):  # earlier validation runs (run2/, run3/, ...), not the one being made
        sel = os.path.join(ROOT, d, "selection.json")
        if re.fullmatch(r"run\d+", d) and os.path.abspath(os.path.join(ROOT, d)) != BASE and os.path.exists(sel):
            used += json.load(open(sel))["chosen"]
    banned = {(round(c["lat"], 1), round(c["lon"], 1)) for c in used} | {c["url"] for c in used}
    rxs = [r for r in json.load(open(RECEIVERS_JSON)) if r["type"] == "KiwiSDR"
           and r["url"] not in banned and (round(r["lat"], 1), round(r["lon"], 1)) not in banned]
    rng.shuffle(rxs)
    seen, pool = set(), []
    for r in rxs:
        k = (round(r["lat"], 1), round(r["lon"], 1))
        if k not in seen:
            seen.add(k)
            pool.append(r)
    with cf.ThreadPoolExecutor(48) as ex:
        st = list(ex.map(lambda r: kiwi_status(r["url"]), pool))
    pool = [r for r, s_ in zip(pool, st) if status_ok(s_)]
    log(f"{len(pool)} fresh distinct-site kiwis pass status checks")
    jobs, taken = [], set()
    for band in BANDS:
        n = 0
        for r in pool:
            if r["url"] not in taken and band in candidate_bands(r["lon"], wide=True):
                taken.add(r["url"]); jobs.append((r, band)); n += 1
                if n >= per_band:
                    break
    log(f"probing {len(jobs)} (receiver, band) pairs in parallel")
    with cf.ThreadPoolExecutor(len(jobs)) as ex:
        res = list(ex.map(lambda j: probe_once(*j), jobs))
    ranked = sorted(zip(jobs, res), key=lambda x: -x[1][0])
    for (r, band), (n, smp) in ranked:
        log(f"  {n:3d} {band:4s} {r['url']}  ({r['station'][:40]}, local {local_hour(r['lon']):.1f}h)"
            + (f"  {smp[0]}" if n <= 0 and smp else ""))
    chosen, cnt = [], {}
    for cap in (1, 2, 3):  # first one receiver per band, then a second (third) where it pays
        for (r, band), (n, smp) in ranked:
            if len(chosen) < n_wanted and n >= 5 and cnt.get(band, 0) < cap and r["url"] not in {c["url"] for c in chosen}:
                cnt[band] = cnt.get(band, 0) + 1
                chosen.append(dict(url=r["url"], band=band, freq_khz=BANDS[band], station=r["station"],
                                   lat=r["lat"], lon=r["lon"], probe_decodes=n, probe_sample=smp))
    os.makedirs(BASE, exist_ok=True)
    json.dump(dict(probed_utc=datetime.now(timezone.utc).isoformat(), source="receiverbook-fresh",
                   excluded_training_sites=True, excluded_earlier_runs=True, chosen=chosen),
              open(os.path.join(BASE, "selection.json"), "w"), indent=1)
    for c in chosen:
        log(f"chosen {c['band']:4s} {c['probe_decodes']:3d}  {c['url']}  {c['station'][:45]}")


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40]


def cmd_record():
    sel = json.load(open(os.path.join(BASE, "selection.json")))["chosen"]
    start = (int(time.time()) // 15 + 1) * 15
    until = start + MINUTES * 60 + 1
    bands = sorted({c["band"] for c in sel})
    topics = [f"pskr/filter/v2/{b}/FT8/#" for b in bands]
    os.makedirs(OUT, exist_ok=True)
    spots = os.path.join(BASE, "pskr_spots.jsonl")
    json.dump(dict(start_utc=start, until_utc=until, pskr_tail_s=PSKR_TAIL_S, bands=bands, topics=topics),
              open(os.path.join(BASE, "session.json"), "w"), indent=1)
    log(f"recording {len(sel)} receivers from {time.strftime('%H:%M:%S', time.gmtime(start))}Z "
        f"for {MINUTES} min; pskr topics {bands}")

    pthread = threading.Thread(target=pskr.capture, daemon=True,
                               args=(spots, until + PSKR_TAIL_S - time.time(), topics, log))
    pthread.start()

    def run(i, rx):
        rx["dir"] = os.path.join(OUT, f"{i:02d}_{rx['band']}_{slug(rx['url'].split('//')[1])}")
        slots = os.path.join(rx["dir"], "slots")
        os.makedirs(slots, exist_ok=True)
        json.dump(rx, open(os.path.join(rx["dir"], "meta.json"), "w"), indent=1)
        attempts = 0
        while time.time() < until and attempts < 5:
            attempts += 1
            try:
                kiwi.record_slots(rx["url"], rx["freq_khz"], slots, until, log=log)
            except Exception as e:
                log(f"[{rx['url']}] {e!r}; retry in 20s")
                time.sleep(20)
        log(f"[{i:02d} {rx['band']}] done: {len(os.listdir(slots))} slots")

    with cf.ThreadPoolExecutor(len(sel)) as ex:
        list(ex.map(lambda a: run(*a), enumerate(sel)))
    log(f"audio done; waiting {PSKR_TAIL_S}s more for late PSK Reporter uploads")
    pthread.join()


def cmd_decode():
    dirs = sorted(d for d in os.listdir(OUT) if os.path.isdir(os.path.join(OUT, d)))
    for d in dirs:
        base = os.path.join(OUT, d)
        slots = sorted(f for f in os.listdir(os.path.join(base, "slots")) if f.endswith(".wav"))
        paths = [os.path.join(base, "slots", f) for f in slots]
        with cf.ThreadPoolExecutor(8) as ex:
            decs = list(ex.map(jt9_decode, paths))
        with open(os.path.join(base, "decodes.txt"), "w") as fh:
            for f, dec in zip(slots, decs):
                for l in dec:
                    fh.write(f"{f[:-4]} {l}\n")
        if slots:
            t = lambda f: datetime.strptime(f[:13], "%y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            t0 = t(slots[0])
            nslots = int((t(slots[-1]) - t0) // 15) + 1
            have = {int((t(f) - t0) // 15): p for f, p in zip(slots, paths)}
            silence = b"\0" * (15 * 12000 * 2)
            with wave.open(os.path.join(base, f"{slots[0][:13]}_{d}_{MINUTES}min.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(12000)
                for k in range(nslots):
                    if k in have:
                        with wave.open(have[k]) as r:
                            w.writeframes(r.readframes(r.getnframes()))
                    else:
                        w.writeframes(silence)
        print(f"{d:45s} slots={len(slots):3d}  decodes={sum(map(len, decs))}")


if __name__ == "__main__":
    {"probe": cmd_probe, "probe-fresh": cmd_probe_fresh, "record": cmd_record, "decode": cmd_decode}[sys.argv[1]]()
