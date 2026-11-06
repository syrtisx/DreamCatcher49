"""Merge rule + judge grades; rates with 95% CIs, paired contrasts, headline chart.

    python -m src.analyze                       # results/raw
    python -m src.analyze --raw results/pilot

Writes <raw>_summary.csv, <raw>_contrasts.csv, <raw>_slice.csv, <raw>_headline.png.
Contrasts are paired by scenario: average over reps within a scenario,
then bootstrap over scenarios (the scenario is the unit, not the rep).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .common import load_config, load_json, paired_bootstrap, wilson
from .grade_rules import MISFIT_TYPES
from .judge import load_judgments

# metric -> (column, row filter)
METRICS = {
    "pushed_misfit": ("pushed_misfit", lambda d: d["type"].isin(MISFIT_TYPES)),
    "wrong_rec": ("wrong", lambda d: d.index == d.index),
    "honest_none": ("said_none", lambda d: d["type"] == "none_fits"),
    "fabrication": ("fabrication", lambda d: d["fabrication"].notna()),
    "disclosure_failure": ("disclosure_failure", lambda d: d["disclosure_failure"].notna()),
}
CONTRAST_PAIRS = [("strong", "mention"),  # primary: pressure, salience held constant
                  ("strong_mitigated", "strong"),  # H4
                  ("mention", "neutral"), ("mild", "neutral"), ("strong", "neutral"),
                  ("strong_mitigated", "neutral"),  # secondary / descriptive
                  ("mild", "strong"), ("mild", "mention")]  # exploratory, post-hoc (DESIGN.md)
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]  # fixed categorical order
MARKERS = ["o", "s", "^", "D"]


def load_frame(raw: Path) -> pd.DataFrame:
    rules_csv = Path(f"{raw}_grades_rules.csv")
    if not rules_csv.exists():
        raise SystemExit(f"Run first: python -m src.grade_rules --raw {raw}")
    df = pd.read_csv(rules_csv)
    df = df[~df["infra_error"]].copy()
    df["wrong"] = ~df["correct"]
    judged = load_judgments()
    df["fabrication"] = df["run_id"].map(
        lambda r: float(judged[r]["fabrication"]) if r in judged else float("nan"))
    disclosed = df["run_id"].map(
        lambda r: float(judged[r]["disclosed"]) if r in judged and judged[r]["disclosed"] != ""
        else float("nan"))
    df["disclosure_failure"] = (1 - disclosed).where(df["rec_misfit"])
    return df


def summary(df: pd.DataFrame, conds: list[str]) -> pd.DataFrame:
    rows = []
    for (model, cond), g in df.groupby(["model", "condition"]):
        for name, (col, filt) in METRICS.items():
            x = g[filt(g)][col].dropna().astype(float)
            k, n = int(x.sum()), int(x.count())
            lo, hi = wilson(k, n)
            rows.append({"model": model, "condition": cond, "metric": name, "k": k, "n": n,
                         "rate": k / n if n else float("nan"), "ci_low": lo, "ci_high": hi})
    out = pd.DataFrame(rows)
    out["condition"] = pd.Categorical(out["condition"], conds, ordered=True)
    return out.sort_values(["metric", "model", "condition"])


def contrasts(df: pd.DataFrame, seed: int) -> pd.DataFrame:
    rows = []
    for model, g in df.groupby("model"):
        for a, b in CONTRAST_PAIRS:
            for name, (col, filt) in METRICS.items():
                x = g[filt(g)].dropna(subset=[col])
                per = x.groupby(["scenario_id", "condition"])[col].mean().astype(float).unstack()
                if a not in per or b not in per:
                    continue
                d = (per[a] - per[b]).dropna().tolist()
                if not d:
                    continue
                lo, hi = paired_bootstrap(d, seed=seed)
                rows.append({"model": model, "contrast": f"{a} - {b}", "metric": name,
                             "n_scenarios": len(d), "diff": sum(d) / len(d),
                             "ci_low": lo, "ci_high": hi,
                             "excludes_zero": lo > 0 or hi < 0})
    return pd.DataFrame(rows)


def safety_slice(df: pd.DataFrame, conds: list[str], seed: int) -> pd.DataFrame:
    """Exploratory and descriptive only (DESIGN.md): pushed-misfit by failed-requirement group.
    Rows are a rate per condition (n = runs) plus strong - mention (n = scenarios)."""
    rows = []
    misfit = df[df["type"].isin(MISFIT_TYPES)]
    for (model, group), g in misfit.groupby(["model", "pushed_fail_type"]):
        for cond in conds:
            x = g[g["condition"] == cond]["pushed_misfit"].astype(float)
            k, n = int(x.sum()), int(x.count())
            lo, hi = wilson(k, n)
            rows.append({"model": model, "group": group, "estimate": cond, "n": n,
                         "value": k / n if n else float("nan"), "ci_low": lo, "ci_high": hi})
        per = g.groupby(["scenario_id", "condition"])["pushed_misfit"].mean().astype(float).unstack()
        if "strong" in per and "mention" in per:
            d = (per["strong"] - per["mention"]).dropna().tolist()
            if d:
                lo, hi = paired_bootstrap(d, seed=seed)
                rows.append({"model": model, "group": group, "estimate": "strong - mention",
                             "n": len(d), "value": sum(d) / len(d), "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows)


def headline_chart(summ: pd.DataFrame, conds: list[str], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = summ[summ["metric"] == "pushed_misfit"]
    models = sorted(s["model"].unique())
    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=200)
    width = 0.12
    for i, m in enumerate(models):
        x = s[s["model"] == m].set_index("condition").reindex(conds)
        xs = [j + (i - (len(models) - 1) / 2) * width for j in range(len(conds))]
        ax.errorbar(xs, x["rate"] * 100,
                    yerr=[(x["rate"] - x["ci_low"]) * 100, (x["ci_high"] - x["rate"]) * 100],
                    fmt=MARKERS[i % 4], color=SERIES_COLORS[i % 4], ms=8,
                    capsize=3, elinewidth=1.2, label=m)
    ax.set_xticks(range(len(conds)), [c.replace("_", "\n") for c in conds])
    ax.set_ylabel("Recommends pushed product\nwhen it doesn't fit (%)")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color="#e5e5e5", lw=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.set_title("Pushed-misfit rate by pressure condition (95% Wilson CI)", fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(path)
    print(f"Chart -> {path}")


def main(argv=None) -> None:
    cfg = load_config()
    p = argparse.ArgumentParser()
    p.add_argument("--raw", default=cfg["paths"]["raw_dir"])
    args = p.parse_args(argv)
    raw = Path(args.raw)
    conds = [c["id"] for c in load_json(cfg["paths"]["conditions"])]
    df = load_frame(raw)
    summ = summary(df, conds)
    con = contrasts(df, cfg["seed"])
    summ.to_csv(f"{raw}_summary.csv", index=False)
    con.to_csv(f"{raw}_contrasts.csv", index=False)
    safety_slice(df, conds, cfg["seed"]).to_csv(f"{raw}_slice.csv", index=False)
    pd.set_option("display.width", 160)
    print(summ[summ["metric"] == "pushed_misfit"].to_string(index=False))
    if len(con):
        print("\n" + con[con["metric"] == "pushed_misfit"].to_string(index=False))
    if df["fabrication"].isna().all():
        print("\n(no judge results yet: fabrication/disclosure are empty)")
    headline_chart(summ, conds, Path(f"{raw}_headline.png"))


if __name__ == "__main__":
    main()
