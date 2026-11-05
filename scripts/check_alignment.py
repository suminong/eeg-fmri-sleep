"""Check EEG-fMRI alignment for every run using only the BrainVision marker files.

For each run: count R128 volume triggers, check their spacing against the TR,
look for missing triggers, and compare scan length with the staging epochs.
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

DATA_ROOT = Path.home() / "Desktop/OpenNeuro"
STAGING_TABLE = Path("reports/qc/stage_counts_by_session.csv")
OUT = Path("reports/qc/alignment_by_run.csv")

TR_SEC = 2.1
EPOCH_SEC = 30


def read_sampling_interval_us(vhdr: Path) -> float:
    """Sampling interval in microseconds from the header (200 us = 5000 Hz)."""
    match = re.search(r"SamplingInterval=(\d+)", vhdr.read_text(errors="ignore"))
    if not match:
        raise ValueError(f"No SamplingInterval in {vhdr.name}")
    return float(match.group(1))


def read_markers(vmrk: Path) -> pd.DataFrame:
    """Parse marker lines like 'Mk2=Response,R128,12345,1,0' into a table."""
    rows = []
    for line in vmrk.read_text(errors="ignore").splitlines():
        if not line.startswith("Mk"):
            continue
        fields = line.split("=", 1)[1].split(",")
        rows.append({"type": fields[0], "desc": fields[1].strip(), "sample": int(fields[2])})
    return pd.DataFrame(rows)


def check_run(vhdr: Path) -> dict:
    """Alignment numbers for one run."""
    sf = 1e6 / read_sampling_interval_us(vhdr)
    markers = read_markers(vhdr.with_suffix(".vmrk"))
    r128 = np.sort(markers.loc[markers["desc"] == "R128", "sample"].to_numpy())
    n_s1 = int((markers["desc"] == "S1").sum()) if not markers.empty else 0

    vol_len = TR_SEC * sf
    gaps = np.diff(r128) if len(r128) > 1 else np.array([])
    # A gap close to 2 TRs or more means a trigger (volume) is missing
    n_missing = int(np.round(gaps[gaps > 1.5 * vol_len] / vol_len - 1).sum()) if len(gaps) else 0
    grid = r128[0] + vol_len * np.arange(len(r128)) if len(r128) else r128
    max_dev = int(np.abs(r128 - grid).max()) if len(r128) else -1

    stem = vhdr.stem  # e.g. sub-01_task-sleep_run-1_eeg
    return {
        "subject": stem.split("_")[0],
        "session": stem.split("_", 1)[1].rsplit("_", 1)[0],
        "sfreq_hz": sf,
        "n_R128": len(r128),
        "median_tr_sec": round(float(np.median(gaps) / sf), 4) if len(gaps) else np.nan,
        "max_grid_dev_samples": max_dev,
        "n_missing_volumes": n_missing,
        "scan_sec": round((len(r128) * vol_len) / sf, 1),
        "n_S1_presses": n_s1,
    }


def main() -> None:
    runs = [check_run(v) for v in sorted(DATA_ROOT.glob("sub-*/eeg/*.vhdr"))]
    table = pd.DataFrame(runs)

    # Compare scan length with the number of scored 30-s epochs
    staging = pd.read_csv(STAGING_TABLE)[["subject", "session", "n_epochs"]]
    table = table.merge(staging, on=["subject", "session"], how="outer", indicator=True)
    table["epochs_from_scan"] = (table["scan_sec"] / EPOCH_SEC).round(1)
    table["length_match"] = (table["epochs_from_scan"] - table["n_epochs"]).abs() < 0.5

    OUT.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT, index=False)

    print(f"Runs: {len(table)}")
    print(f"Runs only in EEG or only in staging: {(table['_merge'] != 'both').sum()}")
    print(f"Runs with missing volumes: {(table['n_missing_volumes'] > 0).sum()}")
    print(f"Runs with TR not 2.1 s: {(table['median_tr_sec'].round(2) != TR_SEC).sum()}")
    print(f"Runs where scan length != staging length: {(~table['length_match']).sum()}")
    print(f"Max trigger deviation from TR grid (samples): {table['max_grid_dev_samples'].max()}")
    print(f"\nSaved {OUT}")


if __name__ == "__main__":
    main()