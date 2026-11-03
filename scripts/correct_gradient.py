"""Remove MRI gradient artifacts from raw EEG with average artifact subtraction (AAS).

Steps for one recording:
1. Keep only the scan window (first R128 to last R128 + TR).
2. For each channel, subtract a sliding average template of nearby volumes.
3. High-pass filter and downsample to 250 Hz.
4. Save the corrected data and a before/after QC figure.
"""
import argparse
import pandas as pd
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # save figures without opening windows
import matplotlib.pyplot as plt
import mne
import numpy as np
from mne.time_frequency import psd_array_welch

DATA_ROOT = Path.home() / "Desktop/OpenNeuro"
OUT_ROOT = DATA_ROOT / "derivatives/gradient_corrected"
FIG_DIR = Path("reports/qc/gradient")

TR_SEC = 2.1
N_SLICES = 35
SLICE_HZ = N_SLICES / TR_SEC  # expected main gradient frequency (~16.7 Hz)
TEMPLATE_WINDOW = 21          # number of volumes averaged per template
HIGHPASS_HZ = 0.5
LOWPASS_HZ = 35.0  # standard sleep EEG band; also removes residual scanner peaks above 35 Hz
TARGET_SFREQ = 250.0
QC_CHANNEL = "Oz"


MAX_TRIGGER_JITTER = 2  # samples; larger deviations mean scanner sync was lost


def find_volume_onsets(raw: mne.io.BaseRaw) -> tuple[np.ndarray, int, int]:
    """Return volume onsets on an exact TR grid, samples per volume, and trigger jitter.

    With scanner clock sync, the gradient artifact repeats exactly every TR,
    while R128 markers can be off by a sample. Onsets are therefore placed on
    a grid starting at the first R128, and the markers are only used to check
    that the grid holds.
    """
    sf = raw.info["sfreq"]
    onsets = np.array([a["onset"] for a in raw.annotations if "R128" in a["description"]])
    samples = np.round(onsets * sf).astype(int)
    vol_len = int(round(TR_SEC * sf))

    grid = samples[0] + vol_len * np.arange(len(samples))
    jitter = int(np.abs(samples - grid).max())
    if jitter > MAX_TRIGGER_JITTER:
        raise ValueError(f"R128 markers drift {jitter} samples from the TR grid; sync may be lost")
    return grid, vol_len, jitter


def aas_channel(signal: np.ndarray, onsets: np.ndarray, vol_len: int, window: int) -> np.ndarray:
    """Subtract a sliding average artifact template from one channel."""
    segs = signal[onsets[:, None] + np.arange(vol_len)]  # (n_volumes, vol_len)
    n_vol = len(onsets)

    # Remove each segment's offset before averaging so slow drift does not leak into the template
    segs_dm = segs - segs.mean(axis=1, keepdims=True)
    cumsum = np.vstack([np.zeros((1, vol_len)), np.cumsum(segs_dm, axis=0)])

    corrected = np.empty_like(segs)
    half = window // 2
    for v in range(n_vol):
        # Centered window, shifted inward at the edges so it always holds `window` volumes
        lo = min(max(0, v - half), max(0, n_vol - window))
        hi = min(n_vol, lo + window)
        template = (cumsum[hi] - cumsum[lo]) / (hi - lo)
        corrected[v] = segs[v] - template
    return corrected.reshape(-1)


def peak_ratio(psd: np.ndarray, freqs: np.ndarray, f0: float) -> float:
    """Power at f0 relative to the median of nearby bins (1 = no peak)."""
    peak = psd[np.argmin(np.abs(freqs - f0))]
    near = (np.abs(freqs - f0) > 0.5) & (np.abs(freqs - f0) < 2.0)
    return float(peak / np.median(psd[near]))


def correct_file(vhdr: Path) -> dict:
    """Run AAS on one recording, save the result, and return QC numbers."""
    raw = mne.io.read_raw_brainvision(vhdr, preload=False, verbose="error")
    raw.set_channel_types({"EOG": "eog", "ECG": "ecg"})
    sf = raw.info["sfreq"]

    onsets, vol_len, jitter = find_volume_onsets(raw)
    start, stop = onsets[0], onsets[-1] + vol_len
    data = raw.get_data(start=start, stop=stop)  # scan window only, in volts
    rel_onsets = onsets - start

    eeg_idx = mne.pick_types(raw.info, eeg=True)
    qc_idx = raw.ch_names.index(QC_CHANNEL)
    mid = (stop - start) / sf / 2 - 30  # 60-s QC window in the middle of the scan

    # QC numbers before correction
    before = data[qc_idx, int(mid * sf): int((mid + 60) * sf)] * 1e6
    psd_b, f_b = psd_array_welch(before, sf, fmin=0.5, fmax=100, n_fft=int(4 * sf), verbose="error")
    trace_b = before[: int(5 * sf)]
    std_b = float(np.median(data[eeg_idx].std(axis=1)) * 1e6)

    # AAS, written back in place to save memory
    for i in range(data.shape[0]):
        data[i] = aas_channel(data[i], rel_onsets, vol_len, TEMPLATE_WINDOW)

    raw_c = mne.io.RawArray(data, raw.info.copy(), verbose="error")

    # Keep markers inside the scan window, with time 0 = first R128
    ann = raw.annotations
    keep = (ann.onset >= start / sf) & (ann.onset < stop / sf)
    raw_c.set_annotations(
        mne.Annotations(ann.onset[keep] - start / sf, ann.duration[keep], ann.description[keep])
    )
    raw_c.filter(l_freq=HIGHPASS_HZ, h_freq=LOWPASS_HZ, picks="all", verbose="error")
    raw_c.resample(TARGET_SFREQ, verbose="error")  # includes anti-alias low-pass

    out_dir = OUT_ROOT / vhdr.parent.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_c.save(out_dir / f"{vhdr.stem}_desc-gac_raw.fif", overwrite=True, verbose="error")

    # QC numbers after correction
    after = raw_c.get_data(picks=[QC_CHANNEL], tmin=mid, tmax=mid + 60)[0] * 1e6
    psd_a, f_a = psd_array_welch(after, TARGET_SFREQ, fmin=0.5, fmax=100,
                                 n_fft=int(4 * TARGET_SFREQ), verbose="error")
    trace_a = after[: int(5 * TARGET_SFREQ)]
    std_a = float(np.median(raw_c.get_data(picks="eeg").std(axis=1)) * 1e6)

    save_qc_figure(vhdr.stem, f_b, psd_b, f_a, psd_a, trace_b, sf, trace_a)
 
    return {
        "file": vhdr.stem,
        "n_volumes": len(onsets),
        "trigger_jitter_samples": jitter,
        "median_std_before_uv": round(std_b, 1),
        "median_std_after_uv": round(std_a, 1),
        "slice_peak_before": round(peak_ratio(psd_b, f_b, SLICE_HZ), 1),
        "slice_peak_after": round(peak_ratio(psd_a, f_a, SLICE_HZ), 1),
    }


def save_qc_figure(stem, f_b, psd_b, f_a, psd_a, trace_b, sf_b, trace_a) -> None:
    """Before/after PSD and a 5-s trace for the QC channel."""
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(3, 1, figsize=(9, 9))

    axes[0].semilogy(f_b, psd_b, label="before (raw)", alpha=0.7)
    axes[0].semilogy(f_a, psd_a, label="after AAS")
    for k in range(1, int(100 // SLICE_HZ) + 1):
        axes[0].axvline(k * SLICE_HZ, color="grey", ls=":", lw=0.8)
    axes[0].set(xlabel="Frequency (Hz)", ylabel="PSD (uV^2/Hz)",
                title=f"{stem}  {QC_CHANNEL}  (dotted = slice frequency harmonics)")
    axes[0].legend()

    axes[1].plot(np.arange(len(trace_b)) / sf_b, trace_b, lw=0.5)
    axes[1].set(title="Before: 5 s raw", xlabel="Time (s)", ylabel="uV")

    axes[2].plot(np.arange(len(trace_a)) / TARGET_SFREQ, trace_a, lw=0.7)
    axes[2].set(title="After: 5 s corrected", xlabel="Time (s)", ylabel="uV")

    fig.tight_layout()
    fig.savefig(FIG_DIR / f"{stem}_gradient_qc.png", dpi=120)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="recording stem, e.g. sub-01_task-sleep_run-1_eeg")
    parser.add_argument("--all", action="store_true", help="process every downloaded recording")
    args = parser.parse_args()

    if args.all:
        files = sorted(DATA_ROOT.glob("sub-*/eeg/*.vhdr"))
    elif args.file:
        files = sorted(DATA_ROOT.glob(f"sub-*/eeg/{args.file}.vhdr"))
    else:
        parser.error("use --file STEM or --all")
    if not files:
        raise FileNotFoundError("No matching .vhdr files")

    rows = []
    for f in files:
        result = correct_file(f)
        print(result)
        rows.append(result)

    summary = pd.DataFrame(rows)
    summary_path = Path("reports/qc/gradient_summary.csv")
    # Append to an existing summary so single-file runs are not lost
    if summary_path.exists():
        old = pd.read_csv(summary_path)
        summary = pd.concat([old[~old["file"].isin(summary["file"])], summary])
    summary.sort_values("file").to_csv(summary_path, index=False)
    print(f"\nSaved {summary_path}")


if __name__ == "__main__":
    main()