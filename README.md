# FT8 validation set — KiwiSDR audio + PSK Reporter ground truth

Real off-air FT8 recordings, captured while simultaneously logging every PSK Reporter FT8 spot for the
same bands. A decode is "real" if other stations worldwide spotted that transmitter in the same slot.

## Runs

| Run | Folder | Receivers | Window (UTC) | Spots |
|---|---|---|---|---|
| 1 | `.` (`recordings/`, `pskr_spots.jsonl`) | 9 Kiwis **also used for training** (`../ft8-training-data`), different bands/time | Wed 2026-11-04 22:17:45–22:47:45 | 920k |
| 2 | `run2/` | 9 fresh receiverbook Kiwis, **no training-set site** | Wed 2026-11-04 22:28:30–22:58:30 | 853k |
| 3 | `run3/` | 12 fresh receiverbook Kiwis, **no training-set or run-2 site** | Mon 2026-11-09 05:32:00–06:02:00 | 556k |

Each receiver folder: `slots/*.wav` (15 s, 12 kHz mono, `YYMMDD_HHMMSS` = slot start), `meta.json`
(url, band, dial kHz, location), `decodes.txt` (jt9 baseline), `verified_<tag>.tsv` (per-decode tier),
`*_30min.wav` (continuous, zero-filled gaps). Runs 2 and 3 are the honest generalisation tests; compare all.

Run 2 is one evening in Europe/North America (night bands 160–40 m, plus 20–10 m). Run 3 is a different day
and time: Oceania, Asia and the Gulf in daytime (VK6, VK2, Tokyo, Philippines, Nepal, Qatar on 20/15/10 m),
Europe at dawn on 40 m and **60 m** (new band), western USA at night on 80 m. Scoring both separately and pooled
shows whether a change generalises across propagation and band activity, and pools the false-decode counts.

## Score a decoder

```sh
# decoder output: <root>/<station>/decodes_<tag>.txt  (ft8nn tab format or jt9 text format)
python3 tools/verify.py --decodes <root> --tag <tag> --list
python3 tools/verify.py --recordings run2/recordings --spots run2/pskr_spots.jsonl --decodes <root2> --tag <tag>
python3 tools/verify.py --recordings run3/recordings --spots run3/pskr_spots.jsonl --decodes <root3> --tag <tag>
```

Tiers: `slot` (spotted by others, same slot, ±50 Hz) > `active` (spotted on the band ±10 min) >
`repeat` (unspotted but decoded in ≥2 slots) > `unconf` (unspotted, once) ; `n/a` = hashed sender/free text.
**`unconf` ≈ false decodes.** jt9 baseline: run 1 13 / 27,462 checkable (0.05%), run 2 16 / 23,958
(0.07%), run 3 17 / 23,139 (0.07%) — every one a garbage call. Run 2 Kiwis 00 and 05 are mis-calibrated by −48 / −129 Hz, run 3 Kiwi 11 (Doha) by −319 Hz;
`verify.py` estimates and removes each receiver's offset before the ±50 Hz match.
`active` is mostly slots whose spots never got uploaded — reporters only upload some decodes, in batches.

## Reproduce

```sh
python3 tools/val.py probe                                              # re-probe training Kiwis
RECEIVERS_JSON=receivers.json OUTDIR=run2 python3 tools/val.py probe-fresh   # fresh Kiwis
RECEIVERS_JSON=receivers.json OUTDIR=run3 N_RX=16 PER_BAND=6 python3 tools/val.py probe-fresh  # run 3
[OUTDIR=run2] python3 tools/val.py record                               # 30 min + 12 min spot tail
[OUTDIR=run2] python3 tools/val.py decode                               # jt9 baseline
```

`probe-fresh` excludes training sites and every earlier `run*/selection.json` (by URL and by lat/lon
rounded to 0.1°), and keeps receivers with ≥ 5 jt9 decodes in the probe slot, up to 3 per band. Run 3 asked
for 16 and got 12; it reused the 2026-11-04 `receivers.json` snapshot (360 sites still passed the status check).

`receivers.json` = receiverbook.de/map snapshot. `tools/pskr.py` is a stdlib MQTT client for
`mqtt.pskreporter.info:1883`, topic `pskr/filter/v2/<band>/FT8/#`.

jt9 note: without a MyCall jt9 uses placeholder `K1ABC`, and its MyCall-AP pass (`a2`) invents
`K1ABC …` decodes. `val.py` drops them; `../ft8-training-data` labels still contain 5.

## Getting the files (this repository)

The audio and spots are stored compressed in Git LFS: slot WAVs as FLAC (lossless), `pskr_spots.jsonl` as
`.jsonl.xz`; the 30-minute WAVs are left out (they are the slots concatenated). Unpack restores every file
byte for byte and checks it against the SHA-256 manifests. Needs `git-lfs`, `flac` and `xz`.

```sh
git clone https://github.com/jasiek/ft8-validation-set   # next to ft8-nn, so ../ft8-validation-set works there
cd ft8-validation-set
python3 tools/datarepo.py unpack . --manifest MANIFEST.sha256 --manifest run2/MANIFEST.sha256 --manifest run3/MANIFEST.sha256
```

Only part of it: `GIT_LFS_SKIP_SMUDGE=1 git clone ...`, then `git lfs pull --include="run3/**"` (and unpack).
Sources: audio from public KiwiSDR receivers (operators listed in `selection.json` / `run2/selection.json` / `run3/selection.json`);
spots from PSK Reporter's public MQTT feed (`mqtt.pskreporter.info`). The tools live on in
https://github.com/jasiek/ft8-nn under `collect/`.
