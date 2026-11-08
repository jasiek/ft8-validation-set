"""Pack a recording folder for a Git LFS data repo, and unpack it back byte for byte.

  python3 collect/datarepo.py pack SRC DST       # SRC = ../ft8-validation-set or ../ft8-training-data
  python3 collect/datarepo.py unpack DIR [--manifest M ...]

pack: slot WAVs -> FLAC (`flac -8`, lossless, ~60% of the size), PSK Reporter *.jsonl -> *.jsonl.xz (`xz -9`, ~6%),
every other file copied as is. The 30-minute WAVs are left out: they are the slot WAVs concatenated with silence
for missing slots, and unpack rebuilds them exactly as collect/val.py / collect/ft8rec.py `decode` wrote them.
unpack: the reverse, in place (WAV and .jsonl next to the packed files), then checks each given SHA-256 manifest
(collect/manifest.py format, relative to its own run folder). Needs the `flac` and `xz` command-line tools.
"""
import argparse, hashlib, os, shutil, subprocess, sys, wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

SLOT_BYTES = 15 * 12000 * 2


def pack(src, dst):
    jobs = []
    for d, dirs, fs in os.walk(src):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
        rel = os.path.relpath(d, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in fs:
            s, t = os.path.join(d, f), os.path.join(dst, rel, f)
            if f.endswith("min.wav") and "_" in f:
                continue  # 30-minute WAV: rebuilt by unpack
            if f.endswith(".wav"):
                jobs.append(["flac", "-8", "-s", "-f", "-o", t[:-4] + ".flac", s])
            elif f.endswith(".jsonl"):
                jobs.append(["sh", "-c", 'xz -9 -T0 -c "$0" > "$1"', s, t + ".xz"])
            else:
                shutil.copy2(s, t)
    with ThreadPoolExecutor(os.cpu_count()) as ex:
        for r in ex.map(lambda c: subprocess.run(c, capture_output=True, text=True), jobs):
            if r.returncode:
                sys.exit(f"failed: {r.args}: {r.stderr}")
    print(f"packed {len(jobs)} files from {src} into {dst}")


def write_wav(path, pcm):
    with wave.open(path, "wb") as w:  # same header as collect/kiwi.py write_wav
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(12000)
        w.writeframes(pcm)


def unflac(path):
    """FLAC -> raw little-endian PCM -> WAV written like the recorder did (byte-identical)."""
    r = subprocess.run(["flac", "-d", "-s", "-c", "--force-raw-format", "--endian=little", "--sign=signed", path],
                       capture_output=True)
    if r.returncode:
        sys.exit(f"flac failed on {path}: {r.stderr.decode()}")
    write_wav(path[:-5] + ".wav", r.stdout)


def rebuild_30min(rx_dir):
    """As collect/val.py cmd_decode: all slots from the first to the last, silence where a slot is missing."""
    sd = os.path.join(rx_dir, "slots")
    slots = sorted(f for f in os.listdir(sd) if f.endswith(".wav"))
    if not slots:
        return
    t = lambda f: datetime.strptime(f[:13], "%y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    t0 = t(slots[0])
    have = {int((t(f) - t0) // 15): os.path.join(sd, f) for f in slots}
    with wave.open(os.path.join(rx_dir, f"{slots[0][:13]}_{os.path.basename(rx_dir)}_30min.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(12000)
        for k in range(int((t(slots[-1]) - t0) // 15) + 1):
            if k in have:
                with wave.open(have[k]) as r:
                    w.writeframes(r.readframes(r.getnframes()))
            else:
                w.writeframes(b"\0" * SLOT_BYTES)


def check(manifest):
    root, bad, n = os.path.dirname(os.path.abspath(manifest)), 0, 0
    for line in open(manifest):
        h, rel = line.rstrip("\n").split("  ", 1)
        n += 1
        p = os.path.join(root, rel)
        if not os.path.exists(p) or hashlib.sha256(open(p, "rb").read()).hexdigest() != h:
            bad += 1
            print("MISMATCH" if os.path.exists(p) else "MISSING ", rel)
    print(f"{manifest}: {n - bad}/{n} files OK")
    return bad == 0


def unpack(d, manifests):
    flacs, xzs, rx = [], [], []
    for dd, dirs, fs in os.walk(d):
        dirs[:] = [x for x in dirs if x != ".git"]
        flacs += [os.path.join(dd, f) for f in fs if f.endswith(".flac")]
        xzs += [os.path.join(dd, f) for f in fs if f.endswith(".jsonl.xz")]
        if os.path.basename(dd) == "slots":
            rx.append(os.path.dirname(dd))
    with ThreadPoolExecutor(os.cpu_count()) as ex:
        list(ex.map(unflac, flacs))
    for x in xzs:
        subprocess.run(["xz", "-d", "-k", "-f", x], check=True)
    for r in rx:
        rebuild_30min(r)
    print(f"unpacked {len(flacs)} WAVs, {len(xzs)} spot files, {len(rx)} 30-minute WAVs in {d}")
    ok = all([check(m) for m in manifests])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pack", "unpack"])
    ap.add_argument("dir")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--manifest", action="append", default=[])
    a = ap.parse_args()
    pack(a.dir, a.dst) if a.cmd == "pack" else unpack(a.dir, a.manifest)
