# Data Card: OpenNeuro ds003768

## Source
- **Dataset:** Simultaneous EEG and fMRI signals during sleep from humans (OpenNeuro ds003768, v1.0.13)
- **Authors:** Gu, Han, Sainburg, Schade, Liu (Pennsylvania State University)
- **Citation:** Gu Y, Sainburg LE, Han F, Liu X. Simultaneous EEG and functional MRI data during rest and sleep from humans. *Data in Brief* 48, 109059 (2023).
- **License:** [check `dataset_description.json`]
- **Not redistributed here.** Download from OpenNeuro.

## Contents
| Item | Detail |
|---|---|
| Participants | 33 healthy adults (sub-01 to sub-33) |
| Sessions | 2 x 10-min resting state + 0 to 8 x 15-min sleep sessions per subject |
| EEG | 32-channel Brain Products, 10-20 system, EOG and ECG, 5000 Hz, raw (no gradient or BCG correction) |
| fMRI | 3T Siemens Prisma, EPI, TR = 2.1 s, 35 slices |
| Markers | `R128` = fMRI volume trigger; `S1` = wakefulness button press during sleep sessions |
| Sleep staging | 30-s epochs scored by a Registered Polysomnographic Technologist, in `sourcedata/` |
| Size | 86.56 GB, 1346 files |

## Sleep stage summary
Counts from `scripts/count_stages.py` (full tables in `reports/qc/`).

| Stage | Epochs | Minutes |
|---|---|---|
| W | 3344 | 1672 |
| N1 | 2148 | 1074 |
| N2 | 1325 | 662.5 |
| N3 | 42 | 21 |
| Flagged uncertain | 98 | 49 |
| Flagged unscorable | 33 | 16.5 |
| **Total** | **6990** | **3495** |

No REM sleep was scored. N3 appears in only 4 of 33 subjects, and 29 of its 42 epochs come from sub-23.

## Known issues in the source files
All are handled automatically in `scripts/count_stages.py`.

1. **Inconsistent columns.** Only `sub-01` has a `subject` column. Subject IDs are taken from file names.
2. **Malformed row.** One row in `sub-32` uses a space instead of a tab between time and stage. It is split and repaired, and the repair is logged.
3. **Compound labels.** The README lists `uncertain` and `unscorable` as labels, but most appear as suffixes (e.g. `2 (uncertain)`, `2 or 3 (unscorable)`). These are parsed into a base stage plus a flag.

Timing checks passed: every session has contiguous 30-s epochs and the expected length (20 epochs for rest, 30 for sleep).

## Labeling decisions
- **Labels come from staging, not task name.** About 44% of scorable resting-state epochs were scored as sleep (559 of 1267), so "rest" does not mean "awake".
- **Flagged epochs are excluded** from training and evaluation (131 epochs, 1.9%) but kept in counts.
- **Primary target:** Wake vs Sleep (N1 + N2 + N3). Confident epochs are close to balanced (3344 vs 3515).
- **Secondary target:** W / N1 / N2+N3. N3 is too rare and too concentrated in one subject to be its own class.

## Inclusion
- **Training:** all subjects with confident epochs.
- **Per-subject evaluation:** subjects with at least 10 W epochs and 10 sleep epochs, and at most 20% unscorable. 29 of 33 qualify. Excluded from per-subject evaluation: sub-03, sub-07, sub-16 (too little wake) and sub-08 (too little sleep).

## Limitations
- EEG is raw and requires gradient and BCG artifact correction before analysis.
- No REM sleep; results apply to wake and NREM light sleep only.
- N1 has the lowest inter-rater agreement of any stage in general, so N1 labels are expected to be noisier.
- sub-02 and sub-21 have no sleep sessions (rest only, 20 min each).
- Flagged epochs cluster in a few subjects (sub-23, sub-11, sub-07, sub-32).


## Known issues in the EEG files
Checked on sub-01 and sub-23 with `scripts/inspect_dataset.py`.

- **Raw signal.** Median channel SD is 680 to 1080 uV, far above normal EEG, confirming that gradient artifacts are present.
- **Triggers are clean.** Every run has the expected number of `R128` triggers (286 rest, 429 sleep), all spaced exactly 2.1 s. The TR is an integer number of samples (10500), and the recording is synchronized to the scanner clock (`SyncStatus/Sync On`), which suits average artifact subtraction.
- **Amplifier ceiling.** Values are stored as 16-bit integers at 0.5 uV resolution, so the maximum is 16383.5 uV. Samples at this value are clipped.
- **ECG clips in 2.5 to 3.6% of samples** in every run. Acceptable, since ECG is only used to time R-peaks.
- **Bad channels.** sub-01 F8 clips in 2.9% of samples in sleep run-3 only. sub-23 T8 degrades over the session (0% in rest run-1, 24% in rest run-2, 95 to 100% in all sleep runs) and is treated as bad from rest run-2 onward.
- **Recording starts before the scan.** The first `R128` occurs 8 to 166 s into the recording. Staging epochs match scan length exactly, so staging time 0 is taken as the first `R128`.