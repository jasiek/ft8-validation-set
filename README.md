# FT8 validation set — KiwiSDR audio + PSK Reporter ground truth

Real off-air FT8 recordings, captured while simultaneously logging every PSK Reporter FT8 spot for the
same bands. A decode is "real" if other stations worldwide spotted that transmitter in the same slot.

## Runs (2026-11-04)

| Run | Folder | Receivers | Window (UTC) | Spots |
|---|---|---|---|---|
| 1 | `.` (`recordings/`, `pskr_spots.jsonl`) | 9 Kiwis **also used for training** (`../ft8-training-data`), different bands/time | 22:17:45–22:47:45 | 920k |
| 2 | `run2/` | 9 fresh receiverbook Kiwis, **no training-set site** | 22:28:30–22:58:30 | 853k |

Each receiver folder: `slots/*.wav` (15 s, 12 kHz mono, `YYMMDD_HHMMSS` = slot start), `meta.json`
(url, band, dial kHz, location), `decodes.txt` (jt9 baseline), `verified_<tag>.tsv` (per-decode tier),
`*_30min.wav` (continuous, zero-filled gaps). Run 2 is the honest generalisation test; compare both.

## Score a decoder

```sh
# decoder output: <root>/<station>/decodes_<tag>.txt  (ft8nn tab format or jt9 text format)
python3 tools/verify.py --decodes <root> --tag <tag> --list
python3 tools/verify.py --recordings run2/recordings --spots run2/pskr_spots.jsonl --decodes <root2> --tag <tag>
```

Tiers: `slot` (spotted by others, same slot, ±50 Hz) > `active` (spotted on the band ±10 min) >
`repeat` (unspotted but decoded in ≥2 slots) > `unconf` (unspotted, once) ; `n/a` = hashed sender/free text.
**`unconf` ≈ false decodes.** jt9 baseline: run 1 13 / 27,462 checkable (0.05%), run 2 16 / 23,958
(0.07%) — every one a garbage call. Run 2 Kiwis 00 and 05 are mis-calibrated by −48 / −129 Hz;
`verify.py` estimates and removes each receiver's offset before the ±50 Hz match.
`active` is mostly slots whose spots never got uploaded — reporters only upload some decodes, in batches.

## Reproduce

```sh
python3 tools/val.py probe                                              # re-probe training Kiwis
RECEIVERS_JSON=receivers.json OUTDIR=run2 python3 tools/val.py probe-fresh   # fresh Kiwis
[OUTDIR=run2] python3 tools/val.py record                               # 30 min + 12 min spot tail
[OUTDIR=run2] python3 tools/val.py decode                               # jt9 baseline
```

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
python3 tools/datarepo.py unpack . --manifest MANIFEST.sha256 --manifest run2/MANIFEST.sha256
```

Only part of it: `GIT_LFS_SKIP_SMUDGE=1 git clone ...`, then `git lfs pull --include="run2/**"` (and unpack).
Sources: audio from public KiwiSDR receivers (operators listed in `selection.json` / `run2/selection.json`);
spots from PSK Reporter's public MQTT feed (`mqtt.pskreporter.info`). The tools live on in
https://github.com/jasiek/ft8-nn under `collect/`.
