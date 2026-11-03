"""Inspect EEG headers, amplitude, and fMRI volume triggers for downloaded subjects."""
from pathlib import Path
import sys

import mne
import numpy as np
import pandas as pd

ROOT = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Desktop/OpenNeuro"
OUT = Path("reports/inspect")
OUT.mkdir(parents=True, exist_ok=True)

TR_SEC = 2.1  # from README
AMP_LIMIT_UV = 16383.0  # int16 max (32767) x 0.5 uV resolution


def trigger_stats(raw: mne.io.BaseRaw) -> dict:
    """Count R128 volume triggers and check that their spacing matches the TR."""
    onsets = np.array(
        [a["onset"] for a in raw.annotations if "R128" in a["description"]]
    )
    if len(onsets) < 2:
        return {"n_R128": len(onsets), "median_tr_sec": np.nan, "tr_irregular": np.nan}

    gaps = np.diff(onsets)
    return {
        "n_R128": len(onsets),
        "median_tr_sec": round(float(np.median(gaps)), 4),
        # Gaps more than 1% away from the TR suggest missing or extra triggers
        "tr_irregular": int((np.abs(gaps - TR_SEC) > 0.01 * TR_SEC).sum()),
        "first_R128_sec": round(float(onsets[0]), 2),
        "scan_end_sec": round(float(onsets[-1] + TR_SEC), 2),
    }


def summarize_eeg(path: Path, tmin: float = 360.0, tmax: float = 600.0) -> dict:
    """Header info, clipping, flat channels, and trigger checks for one recording."""
    raw = mne.io.read_raw_brainvision(path, preload=False, verbose="error")
    # BrainVision files load every channel as EEG; fix the auxiliary ones
    raw.set_channel_types({"EOG": "eog", "ECG": "ecg"})

    sf = raw.info["sfreq"]
    duration = raw.times[-1]
    if duration < tmax:
        tmin, tmax = duration * 0.25, duration * 0.75

    eeg_names = [raw.ch_names[i] for i in mne.pick_types(raw.info, eeg=True)]
    eeg = raw.get_data(picks="eeg", tmin=tmin, tmax=tmax) * 1e6  # volts to microvolts
    ecg = raw.get_data(picks="ecg", tmin=tmin, tmax=tmax) * 1e6

    ch_std = eeg.std(axis=1)
    clip_pct = (np.abs(eeg) >= AMP_LIMIT_UV).mean(axis=1) * 100

    # Only list channels that actually clip, with the percent of samples affected
    clipped = {n: round(float(p), 3) for n, p in zip(eeg_names, clip_pct) if p > 0}
    flat = [n for n, s in zip(eeg_names, ch_std) if s < 0.5]

    return {
        "file": path.stem,
        "sfreq_hz": sf,
        "duration_min": round(duration / 60, 1),
        "median_eeg_std_uv": round(float(np.median(ch_std)), 1),
        "eeg_max_abs_uv": round(float(np.abs(eeg).max()), 1),
        "n_clipped_eeg_ch": len(clipped),
        "ecg_max_abs_uv": round(float(np.abs(ecg).max()), 1),
        "ecg_clip_pct": round(float((np.abs(ecg) >= AMP_LIMIT_UV).mean() * 100), 3),
        "flat_channels": ", ".join(flat),
        **trigger_stats(raw),
        "clipped_channels_pct": clipped,
    }


def main() -> None:
    vhdr_files = sorted(ROOT.glob("sub-*/eeg/*.vhdr"))
    print(f"{len(vhdr_files)} EEG recordings found\n")

    rows = []
    for p in vhdr_files:
        try:
            rows.append(summarize_eeg(p))
        except Exception as err:  # keep going if one file fails
            rows.append({"file": p.stem, "error": str(err)})

    table = pd.DataFrame(rows)
    table.to_csv(OUT / "eeg_header_summary.csv", index=False)

    show = ["file", "duration_min", "median_eeg_std_uv", "eeg_max_abs_uv",
            "n_clipped_eeg_ch", "ecg_max_abs_uv", "ecg_clip_pct", "flat_channels",
            "first_R128_sec", "scan_end_sec"]
    print(table[[c for c in show if c in table.columns]].to_string(index=False))

    print("\nClipped EEG channels (% of samples in the 6-10 min segment):")
    for _, row in table.iterrows():
        if row.get("clipped_channels_pct"):
            print(f"  {row['file']}: {row['clipped_channels_pct']}")


if __name__ == "__main__":
    main()