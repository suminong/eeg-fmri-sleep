"""Build subject- and session-level sleep stage count tables from the staging TSVs."""
import argparse
from pathlib import Path

import pandas as pd
import re

EPOCH_SEC = 30
STAGE_MAP = {
    "w": "W",
    "1": "N1",
    "2": "N2",
    "3": "N3",
    "uncertain": "uncertain",
    "unscorable": "unscorable",
}
STAGE_ORDER = ["W", "N1", "N2", "N3", "uncertain", "unscorable", "other"]
SLEEP_STAGES = ["N1", "N2", "N3"]

# Expected epochs per session: rest = 10 min, sleep = 15 min (from README)
EXPECTED_EPOCHS = {"rest": 20, "sleep": 30}

# Draft usability rules; adjust after looking at the distribution
MIN_WAKE_EPOCHS = 10   # 5 min of wake
MIN_SLEEP_EPOCHS = 10  # 5 min of N1-N3
MAX_UNSCORABLE_FRAC = 0.2
 
BASE_STAGE = {"w": "W", "1": "N1", "2": "N2", "3": "N3"}


def parse_stage(raw) -> tuple[str, str]:
    """Split a raw label like '2 (uncertain)' into (base stage, flag).

    Returns flag '' for confident labels. Standalone 'uncertain' or
    'unscorable' get base stage 'none'. Labels like '2 or 3' become 'ambiguous'.
    """
    if not isinstance(raw, str):
        return "other", ""
    text = raw.strip().lower()

    flag = ""
    m = re.search(r"\((uncertain|unscorable)\)", text)
    if m:
        flag = m.group(1)
        text = text[: m.start()].strip()

    if text in ("uncertain", "unscorable"):
        return "none", text
    if " or " in text:
        return "ambiguous", flag
    return BASE_STAGE.get(text, "other"), flag

def load_staging(src: Path) -> pd.DataFrame:
    """Read every staging TSV into one long table with cleaned labels.

    Handles two format issues found in the source files:
    1. Only some files have a 'subject' column, so the ID comes from the file name.
    2. Some rows use a space instead of a tab between time and stage
       (e.g. "810 W"), so those rows are split and repaired.
    """
    files = sorted(src.glob("sub-*-sleep-stage.tsv"))
    if not files:
        raise FileNotFoundError(f"No staging files found in {src}")

    time_col, stage_col = "epoch_start_time_sec", "30-sec_epoch_sleep_stage"
    required = ["session", time_col, stage_col]
    frames, missing_subject_col, repaired = [], [], {}

    for f in files:
        part = pd.read_csv(f, sep="\t", dtype=str)

        absent = [c for c in required if c not in part.columns]
        if absent:
            raise ValueError(f"{f.name} is missing columns: {absent}")
        if "subject" not in part.columns:
            missing_subject_col.append(f.name)

        part = part[required].copy()

        # Repair rows where time and stage were joined by a space
        joined = part[stage_col].isna() & part[time_col].str.contains(r"\s", na=False)
        if joined.any():
            split = part.loc[joined, time_col].str.strip().str.split(r"\s+", n=1, expand=True)
            part.loc[joined, time_col] = split[0]
            part.loc[joined, stage_col] = split[1]
            repaired[f.name] = int(joined.sum())

        # Subject ID from the file name, e.g. sub-01-sleep-stage.tsv -> sub-01
        sub_num = int(re.match(r"sub-(\d+)", f.name).group(1))
        part.insert(0, "subject", f"sub-{sub_num:02d}")
        frames.append(part)

    if missing_subject_col:
        print(f"NOTE: {len(missing_subject_col)} / {len(files)} files have no 'subject' column "
              "(ID taken from file name)")
    if repaired:
        print(f"NOTE: repaired space-separated rows in {len(repaired)} files: {repaired}")

    df = pd.concat(frames, ignore_index=True)
    df.columns = ["subject", "session", "epoch_start_sec", "stage_raw"]

    df["session"] = df["session"].str.strip()
    df["task"] = df["session"].str.extract(r"task-([a-z]+)")[0]

    # Fail loudly, with file context, if any time value is still not a number
    df["epoch_start_sec"] = pd.to_numeric(df["epoch_start_sec"].str.strip(), errors="coerce")
    bad_time = df["epoch_start_sec"].isna()
    if bad_time.any():
        raise ValueError(f"Non-numeric epoch times remain:\n{df[bad_time].head(10)}")
 
    parsed = df["stage_raw"].apply(parse_stage)
    df["stage_base"] = parsed.str[0]
    df["flag"] = parsed.str[1]
    # Flagged epochs are counted under their flag; the base stage is kept separately
    df["stage"] = df["flag"].where(df["flag"] != "", df["stage_base"])

    flagged = df[df["flag"] != ""]
    if not flagged.empty:
        print("Flagged epochs by base stage:")
        print(pd.crosstab(flagged["stage_base"], flagged["flag"]).to_string(), "\n")

    unknown = sorted(df.loc[df["stage"] == "other", "stage_raw"].dropna().unique())
    if unknown:
        print(f"WARNING: unmapped stage labels: {unknown}")
    print(f"Loaded {len(files)} files, {len(df)} epochs\n")
    return df


def session_table(df: pd.DataFrame) -> pd.DataFrame:
    """Stage counts and timing checks for each subject x session."""
    keys = ["subject", "session", "task"]
    g = df.groupby(keys)

    out = pd.crosstab([df[k] for k in keys], df["stage"]).reindex(columns=STAGE_ORDER, fill_value=0)
    out["n_epochs"] = g.size()
    out["first_start_sec"] = g["epoch_start_sec"].min()
    out["last_start_sec"] = g["epoch_start_sec"].max()

    # Epochs should be contiguous, one every 30 s, with no duplicates
    out["timing_issue"] = g["epoch_start_sec"].apply(
        lambda s: not (s.sort_values().diff().dropna() == EPOCH_SEC).all()
    )
    expected = out.index.get_level_values("task").map(EXPECTED_EPOCHS)
    out["expected_epochs"] = expected
    out["length_mismatch"] = out["n_epochs"] != out["expected_epochs"]
    return out.reset_index()


def subject_table(df: pd.DataFrame) -> pd.DataFrame:
    """Stage counts per subject plus a draft usability flag."""
    out = pd.crosstab(df["subject"], df["stage"]).reindex(columns=STAGE_ORDER, fill_value=0)

    out["n_sessions"] = df.groupby("subject")["session"].nunique()
    out["n_sleep_sessions"] = (
        df[df["task"] == "sleep"].groupby("subject")["session"].nunique()
        .reindex(out.index, fill_value=0)
    )
    total = out[STAGE_ORDER].sum(axis=1)
    out["total_min"] = total * EPOCH_SEC / 60
    out["sleep_epochs"] = out[SLEEP_STAGES].sum(axis=1)
    out["unscorable_frac"] = (out["unscorable"] / total).round(3)

    out["eval_eligible"] = (
        (out["W"] >= MIN_WAKE_EPOCHS)
        & (out["sleep_epochs"] >= MIN_SLEEP_EPOCHS)
        & (out["unscorable_frac"] <= MAX_UNSCORABLE_FRAC)
    )
    return out.reset_index()


def print_summary(df: pd.DataFrame, subj: pd.DataFrame, sess: pd.DataFrame) -> None:
    """Print the numbers needed for the feasibility note."""
    print("Epochs per stage (whole dataset)")
    counts = df["stage"].value_counts().reindex(STAGE_ORDER, fill_value=0)
    print(pd.DataFrame({"epochs": counts, "minutes": counts * EPOCH_SEC / 60}).to_string(), "\n")

    print("Stage counts by task (rest vs sleep)")
    print(pd.crosstab(df["task"], df["stage"]).reindex(columns=STAGE_ORDER, fill_value=0).to_string(), "\n")

    print(f"Subjects with any N3: {(subj['N3'] > 0).sum()} / {len(subj)}")
    print(f"Usable subjects (draft rule): {subj['eval_eligible'].sum()} / {len(subj)}")
    print(f"Sessions with timing issues: {sess['timing_issue'].sum()}")
    print(f"Sessions with unexpected length: {sess['length_mismatch'].sum()}\n")

    cols = ["subject", "W", "N1", "N2", "N3", "uncertain", "unscorable",
            "n_sleep_sessions", "total_min", "unscorable_frac", "usable"]
    print(subj[cols].to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=Path.home() / "Desktop/OpenNeuro/sourcedata")
    parser.add_argument("--out", type=Path, default=Path("reports/qc"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    df = load_staging(args.src)
    sess = session_table(df)
    subj = subject_table(df)

    sess.to_csv(args.out / "stage_counts_by_session.csv", index=False)
    subj.to_csv(args.out / "stage_counts_by_subject.csv", index=False)
    print_summary(df, subj, sess)


if __name__ == "__main__":
    main()