"""
analysis.py - statistics, fleet sizing, figures and markdown tables.

Everything here reads the replication-level results DataFrame (the same data that
is saved as results/replications.csv). No numbers are typed in by hand.
"""

from __future__ import annotations

import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from simulation import SimConfig, travel_time_min

# Metrics summarised for every configuration
METRICS = [
    "throughput_per_hr", "mean_transport_wait_min", "mean_transport_wait_lower_bound_min",
    "jobs_unassigned_at_end", "empty_travel_km", "loaded_travel_km",
    "transport_jobs_delivered_in_window", "empty_m_per_delivered_job", "deadlock_onset_min",
    "fleet_util_pct", "agv1_util_pct", "agv2_util_pct", "agv3_util_pct", "agv4_util_pct", "agv5_util_pct",
    "agv_wait_for_space_min", "m1_util_pct", "m2_util_pct", "m1_blocked_min", "m2_blocked_min",
    "mean_flow_time_min", "arrivals_in_window", "wip_at_end",
]
GROUP = ["policy", "buffer_capacity", "n_agvs"]
POLICY_LABEL = {"fifo": "FIFO", "nearest": "Nearest-pickup"}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def mean_std_ci(values, confidence=0.95) -> dict:
    """Mean, sample standard deviation (n-1) and t-based CI for the mean.

        half-width = t_{1-alpha/2, n-1} * s / sqrt(n)        (t_{0.975,19} = 2.093 for n = 20)
    NaN values (e.g. no waiting-time data in a run) are ignored.
    """
    x = np.asarray([v for v in values if not (isinstance(v, float) and math.isnan(v))], dtype=float)
    n = len(x)
    if n == 0:
        return {"mean": math.nan, "std": math.nan, "n": 0, "ci_low": math.nan, "ci_high": math.nan, "ci_half": math.nan}
    mean = float(x.mean())
    if n == 1:
        return {"mean": mean, "std": math.nan, "n": 1, "ci_low": math.nan, "ci_high": math.nan, "ci_half": math.nan}
    std = float(x.std(ddof=1))
    half = float(stats.t.ppf(1 - (1 - confidence) / 2, df=n - 1)) * std / math.sqrt(n)
    return {"mean": mean, "std": std, "n": n, "ci_low": mean - half, "ci_high": mean + half, "ci_half": half}


def aggregate(raw: pd.DataFrame) -> pd.DataFrame:
    """One row per (policy, buffer, fleet size) with mean / std / 95% CI of each metric."""
    rows = []
    for key, grp in raw.groupby(GROUP, sort=True):
        row = dict(zip(GROUP, key))
        row["replications"] = len(grp)
        row["deadlocked_runs"] = int(grp["deadlocked"].sum())
        row["deadlock_pct"] = 100.0 * grp["deadlocked"].mean()
        for m in METRICS:
            s = mean_std_ci(grp[m].tolist())
            row[f"{m}_mean"] = s["mean"]
            row[f"{m}_std"] = s["std"]
            row[f"{m}_ci_low"] = s["ci_low"]
            row[f"{m}_ci_high"] = s["ci_high"]
        rows.append(row)
    return pd.DataFrame(rows)


def _verdict(s, higher, lower):
    if s["n"] < 2 or math.isnan(s["ci_low"]):
        return "insufficient data"
    if s["ci_low"] > 0:
        return higher
    if s["ci_high"] < 0:
        return lower
    return "no significant difference"


COMPARE_METRICS = ["throughput_per_hr", "mean_transport_wait_min", "mean_transport_wait_lower_bound_min",
                   "empty_travel_km", "empty_m_per_delivered_job", "fleet_util_pct",
                   "m1_blocked_min", "m2_blocked_min"]


def paired_policy_comparison(raw: pd.DataFrame, metrics=COMPARE_METRICS) -> pd.DataFrame:
    """Nearest minus FIFO, paired by replication (same arrival stream).

    d_r = X_r(nearest) - X_r(FIFO);  CI of mean(d) by the t-method.
    If the CI excludes zero the difference is significant at the 95% level.
    """
    rows = []
    for (buf, n), grp in raw.groupby(["buffer_capacity", "n_agvs"], sort=True):
        fifo = grp[grp.policy == "fifo"].set_index("replication")
        near = grp[grp.policy == "nearest"].set_index("replication")
        common = fifo.index.intersection(near.index)
        for m in metrics:
            d = (near.loc[common, m] - fifo.loc[common, m]).dropna()
            s = mean_std_ci(d.tolist())
            rows.append({"buffer_capacity": buf, "n_agvs": n, "metric": m,
                         "fifo_mean": fifo.loc[common, m].mean(), "nearest_mean": near.loc[common, m].mean(),
                         "mean_diff_nearest_minus_fifo": s["mean"], "diff_ci_low": s["ci_low"],
                         "diff_ci_high": s["ci_high"], "pairs": s["n"],
                         "verdict_95pct": _verdict(s, "nearest higher", "nearest lower")})
    return pd.DataFrame(rows)


def paired_buffer_comparison(raw: pd.DataFrame, small=1, large=4,
                             metrics=("throughput_per_hr", "m1_blocked_min", "m2_blocked_min")) -> pd.DataFrame:
    """Buffer `large` minus buffer `small`, paired by replication, for each policy and fleet size."""
    rows = []
    for (pol, n), grp in raw.groupby(["policy", "n_agvs"], sort=True):
        a = grp[grp.buffer_capacity == small].set_index("replication")
        b = grp[grp.buffer_capacity == large].set_index("replication")
        common = a.index.intersection(b.index)
        if len(common) == 0:
            continue
        for m in metrics:
            d = (b.loc[common, m] - a.loc[common, m]).dropna()
            s = mean_std_ci(d.tolist())
            rows.append({"policy": pol, "n_agvs": n, "metric": m, f"buffer_{small}_mean": a.loc[common, m].mean(),
                         f"buffer_{large}_mean": b.loc[common, m].mean(), "mean_diff": s["mean"],
                         "diff_ci_low": s["ci_low"], "diff_ci_high": s["ci_high"], "pairs": s["n"],
                         "verdict_95pct": _verdict(s, f"buffer {large} higher", f"buffer {large} lower")})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Fleet sizing
# ---------------------------------------------------------------------------
def fleet_sizing(agg: pd.DataFrame, fraction=0.95, plateau_tol=0.02) -> pd.DataFrame:
    """For each (policy, buffer):

        best   = max over tested N of mean throughput
        N*     = smallest tested N with mean throughput >= fraction * best

    The threshold is relative to the best TESTED result, not a theoretical maximum.
    Plateau check: if going from the second-largest to the largest tested fleet still
    raises mean throughput by more than `plateau_tol` (relative), the tested range is
    insufficient to show saturation.
    """
    out = []
    for (pol, buf), sub in agg.groupby(["policy", "buffer_capacity"], sort=True):
        sub = sub.sort_values("n_agvs").set_index("n_agvs")
        means = sub["throughput_per_hr_mean"]
        best_n = int(means.idxmax())
        best = float(means.max())
        target = fraction * best
        meeting = [int(n) for n, v in means.items() if v >= target]
        n_star = min(meeting) if meeting else None
        sizes = list(means.index)
        gain = (means.iloc[-1] - means.iloc[-2]) / means.iloc[-2] if len(sizes) >= 2 and means.iloc[-2] > 0 else math.nan
        plateau = (not math.isnan(gain)) and gain <= plateau_tol
        row = {"policy": pol, "buffer_capacity": buf, "best_mean_throughput_per_hr": best,
               "fleet_size_at_best": best_n, "threshold_per_hr": target, "min_fleet_95pct": n_star,
               "gain_largest_two_fleets_pct": 100 * gain if not math.isnan(gain) else math.nan,
               "plateau_within_tested_range": plateau}
        if n_star is not None:
            r = sub.loc[n_star]
            row.update({
                "throughput_at_min_fleet": r["throughput_per_hr_mean"],
                "throughput_ci_low": r["throughput_per_hr_ci_low"],
                "throughput_ci_high": r["throughput_per_hr_ci_high"],
                "wait_min_at_min_fleet": r["mean_transport_wait_min_mean"],
                "fleet_util_pct_at_min_fleet": r["fleet_util_pct_mean"],
                "empty_km_at_min_fleet": r["empty_travel_km_mean"],
                "m2_util_pct_at_min_fleet": r["m2_util_pct_mean"],
                "deadlock_pct_at_min_fleet": r["deadlock_pct"],
            })
        out.append(row)
    return pd.DataFrame(out)


def marginal_gains(agg: pd.DataFrame) -> pd.DataFrame:
    """Change in mean throughput from adding one AGV (diminishing-returns view)."""
    rows = []
    for (pol, buf), sub in agg.groupby(["policy", "buffer_capacity"], sort=True):
        sub = sub.sort_values("n_agvs")
        for (n0, t0), (n1, t1) in zip(sub[["n_agvs", "throughput_per_hr_mean"]].values[:-1],
                                      sub[["n_agvs", "throughput_per_hr_mean"]].values[1:]):
            rows.append({"policy": pol, "buffer_capacity": buf, "from_n": int(n0), "to_n": int(n1),
                         "throughput_gain_per_hr": t1 - t0})
    return pd.DataFrame(rows)


def analytic_checks(cfg: SimConfig = SimConfig()) -> pd.DataFrame:
    """Hand calculations used to sanity-check the simulation (independent of SimPy)."""
    leg = travel_time_min(120, cfg.agv_speed_m_per_s) + cfg.pickup_handling_min + cfg.dropoff_handling_min
    arrival_rate = 60 / cfg.mean_interarrival_min
    rows = [
        ("AGV speed (m/min)", cfg.agv_speed_m_per_min),
        ("Travel time for 120 m (min)", travel_time_min(120, cfg.agv_speed_m_per_s)),
        ("One loaded leg incl. handling (min)", leg),
        ("Loaded AGV time per component, 3 legs (min)", 3 * leg),
        ("Mean arrival rate (components/h)", arrival_rate),
        ("Machine 1 capacity (components/h)", 60 / cfg.m1_process_min),
        ("Machine 2 capacity (components/h)", 60 / cfg.m2_process_min),
        ("Expected M1 utilization at full flow (%)", 100 * arrival_rate * cfg.m1_process_min / 60),
        ("Expected M2 utilization at full flow (%)", 100 * arrival_rate * cfg.m2_process_min / 60),
        ("Min. AGV-hours per hour for loaded work only (AGVs)", arrival_rate * 3 * leg / 60),
    ]
    return pd.DataFrame(rows, columns=["quantity", "value"])


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
# Validated categorical slots (blue, orange, aqua) and recessive greys.
COLORS = {"fifo": "#2a78d6", "nearest": "#eb6834"}
BUF_COLORS = {1: "#2a78d6", 2: "#eb6834", 4: "#1baf7a"}
MARKERS = {"fifo": "o", "nearest": "s"}
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def _style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": INK_2, "axes.labelcolor": INK, "xtick.color": INK_2, "ytick.color": INK_2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
        "axes.spines.right": False, "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
        "legend.frameon": False, "lines.linewidth": 2, "lines.markersize": 7,
    })


def _errbar(ax, x, mean, lo, hi, **kw):
    if ax.get_yscale() == "log":
        lo = np.maximum(lo, mean * 0.05)       # a log axis cannot show a CI that reaches zero
    yerr = np.vstack([mean - lo, hi - mean])
    yerr = np.nan_to_num(np.clip(yerr, 0, None))
    ax.errorbar(x, mean, yerr=yerr, capsize=3, elinewidth=1.2, **kw)


def _policy_panels(agg, metric, ylabel, title, fname, out_dir, ref_line=None, ref_label=None, ylim0=True,
                   secondary=None, secondary_label="", log=False):
    buffers = sorted(agg.buffer_capacity.unique())
    fig, axes = plt.subplots(1, len(buffers), figsize=(4.2 * len(buffers), 3.8), sharey=True, squeeze=False)
    for ax, buf in zip(axes[0], buffers):
        for i, pol in enumerate(sorted(agg.policy.unique())):
            s = agg[(agg.policy == pol) & (agg.buffer_capacity == buf)].sort_values("n_agvs")
            x = s.n_agvs + (i - 0.5) * 0.08          # small offset so error bars don't overlap
            _errbar(ax, x, s[f"{metric}_mean"].values, s[f"{metric}_ci_low"].values, s[f"{metric}_ci_high"].values,
                    color=COLORS[pol], marker=MARKERS[pol], label=POLICY_LABEL[pol])
            if secondary:
                ax.plot(x, s[f"{secondary}_mean"].values, color=COLORS[pol], linestyle=":", marker=MARKERS[pol],
                        markerfacecolor="none", linewidth=1.5, label=f"{POLICY_LABEL[pol]} - {secondary_label}")
        if log:
            ax.set_yscale("log")
        if ref_line is not None:
            ax.axhline(ref_line, color=INK_2, linestyle=":", linewidth=1.2, label=ref_label)
        ax.set_title(f"Buffer capacity = {buf}")
        ax.set_xlabel("AGV fleet size")
        ax.set_xticks(sorted(agg.n_agvs.unique()))
        if ylim0 and not log:
            ax.set_ylim(bottom=0)
            ax.margins(y=0.08)
    axes[0][0].set_ylabel(ylabel)
    axes[0][-1].legend(loc="best", fontsize=8.5)
    fig.suptitle(title, fontweight="bold", color=INK)
    fig.tight_layout()
    path = os.path.join(out_dir, fname)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def make_figures(raw, agg, paired, out_dir) -> list[str]:
    _style()
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    demand = raw.groupby("replication").arrivals_in_window.first().mean() / 8.0
    paths.append(_policy_panels(
        agg, "throughput_per_hr", "Throughput (components/h)",
        "Fig. 1 - Throughput vs AGV fleet size (mean, 95% CI)", "fig1_throughput_vs_fleet.png", out_dir,
        ref_line=demand, ref_label=f"Mean arrivals in window ({demand:.2f}/h)"))
    paths.append(_policy_panels(
        agg, "mean_transport_wait_min", "Transport waiting time (min, log scale)",
        "Fig. 2 - Transport waiting time vs fleet size (log scale)",
        "fig2_waiting_time_vs_fleet.png", out_dir, secondary="mean_transport_wait_lower_bound_min",
        secondary_label="incl. unassigned", log=True))
    paths.append(_policy_panels(
        agg, "empty_travel_km", "Empty travel in window (km)",
        "Fig. 3 - Empty travel distance vs fleet size (mean, 95% CI)", "fig3_empty_travel_vs_fleet.png", out_dir))
    paths.append(_policy_panels(
        agg, "fleet_util_pct", "Mean AGV utilization (%)",
        "Fig. 4 - AGV utilization vs fleet size (busy incl. waiting to unload)", "fig4_agv_utilization_vs_fleet.png",
        out_dir))

    # Fig 5 - machine utilization (row 1) and blocking (row 2)
    buffers = sorted(agg.buffer_capacity.unique())
    fig, axes = plt.subplots(2, len(buffers), figsize=(4.2 * len(buffers), 7), sharey="row", squeeze=False)
    for col, buf in enumerate(buffers):
        for pol in sorted(agg.policy.unique()):
            s = agg[(agg.policy == pol) & (agg.buffer_capacity == buf)].sort_values("n_agvs")
            for mach, ls, mk in (("m1", "-", "o"), ("m2", "--", "^")):
                lbl = f"{POLICY_LABEL[pol]} - {mach.upper()}"
                axes[0][col].plot(s.n_agvs, s[f"{mach}_util_pct_mean"], color=COLORS[pol], linestyle=ls, marker=mk, label=lbl)
                axes[1][col].plot(s.n_agvs, s[f"{mach}_blocked_min_mean"], color=COLORS[pol], linestyle=ls, marker=mk, label=lbl)
        axes[0][col].set_title(f"Buffer capacity = {buf}")
        for r in (0, 1):
            axes[r][col].set_xticks(sorted(agg.n_agvs.unique()))
            axes[r][col].set_ylim(bottom=0)
        axes[1][col].set_xlabel("AGV fleet size")
    axes[0][0].set_ylabel("Machine utilization (% of 480 min)")
    axes[1][0].set_ylabel("Machine blocked time (min per 480 min)")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4)
    fig.suptitle("Fig. 5 - Machine utilization (processing only) and blocking (mean of 20 runs)", fontweight="bold")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    p = os.path.join(out_dir, "fig5_machine_utilization_blocking.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)

    # Fig 6 - throughput by buffer capacity, one panel per policy
    pols = sorted(agg.policy.unique())
    fig, axes = plt.subplots(1, len(pols), figsize=(5.2 * len(pols), 3.8), sharey=True, squeeze=False)
    for ax, pol in zip(axes[0], pols):
        for i, buf in enumerate(buffers):
            s = agg[(agg.policy == pol) & (agg.buffer_capacity == buf)].sort_values("n_agvs")
            _errbar(ax, s.n_agvs + (i - 1) * 0.08, s.throughput_per_hr_mean.values, s.throughput_per_hr_ci_low.values,
                    s.throughput_per_hr_ci_high.values, color=BUF_COLORS.get(buf, INK_2), marker="o",
                    label=f"Buffer = {buf}")
        ax.axhline(demand, color=INK_2, linestyle=":", linewidth=1.2, label="Mean arrival rate")
        ax.set_title(f"{POLICY_LABEL[pol]} dispatch")
        ax.set_xlabel("AGV fleet size")
        ax.set_xticks(sorted(agg.n_agvs.unique()))
        ax.set_ylim(bottom=0)
    axes[0][0].set_ylabel("Throughput (components/h)")
    axes[0][-1].legend(loc="lower right")
    fig.suptitle("Fig. 6 - Throughput across buffer capacities (mean, 95% CI)", fontweight="bold")
    fig.tight_layout()
    p = os.path.join(out_dir, "fig6_throughput_by_buffer.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)

    # Fig 7 - paired difference nearest - FIFO
    items = [("throughput_per_hr", "Δ throughput (components/h)"),
             ("mean_transport_wait_lower_bound_min", "Δ waiting time incl. unassigned (min)"),
             ("empty_m_per_delivered_job", "Δ empty travel per delivered job (m)")]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.9), squeeze=False)
    for ax, (m, lbl) in zip(axes[0], items):
        for i, buf in enumerate(buffers):
            s = paired[(paired.metric == m) & (paired.buffer_capacity == buf)].sort_values("n_agvs")
            _errbar(ax, s.n_agvs + (i - 1) * 0.08, s.mean_diff_nearest_minus_fifo.values, s.diff_ci_low.values,
                    s.diff_ci_high.values, color=BUF_COLORS.get(buf, INK_2), marker="o", label=f"Buffer = {buf}")
        ax.axhline(0, color=INK, linewidth=1)
        ax.set_ylabel(lbl)
        ax.set_xlabel("AGV fleet size")
        ax.set_xticks(sorted(agg.n_agvs.unique()))
    axes[0][0].legend(loc="best")
    fig.suptitle("Fig. 7 - Nearest-pickup minus FIFO, paired by replication (mean difference, 95% CI)",
                 fontweight="bold")
    fig.tight_layout()
    p = os.path.join(out_dir, "fig7_policy_comparison.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)

    # Fig 8 - deadlock frequency
    fig, axes = plt.subplots(1, len(buffers), figsize=(4.2 * len(buffers), 3.6), sharey=True, squeeze=False)
    width = 0.38
    for ax, buf in zip(axes[0], buffers):
        for i, pol in enumerate(pols):
            s = agg[(agg.policy == pol) & (agg.buffer_capacity == buf)].sort_values("n_agvs")
            xs = s.n_agvs + (i - 0.5) * width
            ax.bar(xs, s.deadlock_pct, width=width - 0.04, color=COLORS[pol], label=POLICY_LABEL[pol])
            for x, pct, k, n in zip(xs, s.deadlock_pct, s.deadlocked_runs, s.replications):
                ax.text(x, pct + 1.5, f"{k}/{n}", ha="center", va="bottom", fontsize=7, color=INK_2)
        ax.set_title(f"Buffer capacity = {buf}")
        ax.set_xlabel("AGV fleet size")
        ax.set_xticks(sorted(agg.n_agvs.unique()))
        ax.set_ylim(0, 112)
    axes[0][0].set_ylabel("Runs ending in deadlock (%)")
    axes[0][-1].legend(loc="upper right")
    fig.suptitle("Fig. 8 - Share of replications that ended in a transport deadlock", fontweight="bold")
    fig.tight_layout()
    p = os.path.join(out_dir, "fig8_deadlock_frequency.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Markdown tables (generated, then inserted into technical_report.md)
# ---------------------------------------------------------------------------
def _f(x, d=2):
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def _ci(mean, lo, hi, d=2):
    return f"{_f(mean, d)} [{_f(lo, d)}, {_f(hi, d)}]"


def markdown_tables(agg, sizing, paired, buf_cmp, gains, checks) -> str:
    L = []
    L.append("#### Table R1 - Hand-calculation checks\n")
    L.append("| Quantity | Value |\n|---|---:|")
    for _, r in checks.iterrows():
        L.append(f"| {r.quantity} | {_f(r.value, 3)} |")

    L.append("\n#### Table R2 - Results per configuration (mean over replications; throughput with 95% CI)\n")
    L.append("| Policy | Buffer | AGVs | Throughput /h [95% CI] | Std | Wait (min) | Unassigned jobs at end | "
             "Empty km | AGV util % | M1 util % | M2 util % | M1 blocked min | M2 blocked min | Deadlock runs |")
    L.append("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in agg.sort_values(GROUP).iterrows():
        L.append(f"| {POLICY_LABEL[r.policy]} | {r.buffer_capacity} | {r.n_agvs} | "
                 f"{_ci(r.throughput_per_hr_mean, r.throughput_per_hr_ci_low, r.throughput_per_hr_ci_high)} | "
                 f"{_f(r.throughput_per_hr_std)} | {_f(r.mean_transport_wait_min_mean)} | "
                 f"{_f(r.jobs_unassigned_at_end_mean, 1)} | {_f(r.empty_travel_km_mean)} | {_f(r.fleet_util_pct_mean, 1)} | "
                 f"{_f(r.m1_util_pct_mean, 1)} | {_f(r.m2_util_pct_mean, 1)} | {_f(r.m1_blocked_min_mean, 1)} | "
                 f"{_f(r.m2_blocked_min_mean, 1)} | {r.deadlocked_runs}/{r.replications} |")

    L.append("\n#### Table R2b - Diagnostics that explain Table R2\n")
    L.append("Waiting time in R2 averages only jobs that were eventually assigned. When a run deadlocks or the fleet "
             "is overloaded, many jobs are never assigned, so the lower bound below (unassigned jobs counted with "
             "their wait up to the end of the run) is the fairer comparison. AGV utilization counts time spent "
             "waiting to unload as busy time, as specified.\n")
    L.append("| Policy | Buffer | AGVs | Wait, assigned jobs (min) | Wait lower bound incl. unassigned (min) | "
             "Transport jobs delivered | Empty m per delivered job | AGV wait-to-unload (min, fleet total) | "
             "Deadlock runs | Mean deadlock onset (min) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in agg.sort_values(GROUP).iterrows():
        L.append(f"| {POLICY_LABEL[r.policy]} | {r.buffer_capacity} | {r.n_agvs} | {_f(r.mean_transport_wait_min_mean)} | "
                 f"{_f(r.mean_transport_wait_lower_bound_min_mean)} | {_f(r.transport_jobs_delivered_in_window_mean, 1)} | "
                 f"{_f(r.empty_m_per_delivered_job_mean, 1)} | {_f(r.agv_wait_for_space_min_mean, 1)} | "
                 f"{r.deadlocked_runs}/{r.replications} | {_f(r.deadlock_onset_min_mean, 0)} |")

    L.append("\n#### Table R3 - Fleet sizing (smallest tested fleet reaching 95% of the best observed mean throughput)\n")
    L.append("| Policy | Buffer | Best mean /h (at N) | 95% threshold /h | Min. fleet | Throughput at min. fleet [95% CI] | "
             "Wait (min) | AGV util % | Deadlock % | Gain between two largest fleets | Plateau shown? |")
    L.append("|---|---:|---|---:|---:|---|---:|---:|---:|---:|---|")
    for _, r in sizing.iterrows():
        L.append(f"| {POLICY_LABEL[r.policy]} | {r.buffer_capacity} | {_f(r.best_mean_throughput_per_hr)} ({r.fleet_size_at_best}) | "
                 f"{_f(r.threshold_per_hr)} | {r.min_fleet_95pct} | "
                 f"{_ci(r.throughput_at_min_fleet, r.throughput_ci_low, r.throughput_ci_high)} | "
                 f"{_f(r.wait_min_at_min_fleet)} | {_f(r.fleet_util_pct_at_min_fleet, 1)} | "
                 f"{_f(r.deadlock_pct_at_min_fleet, 0)} | {_f(r.gain_largest_two_fleets_pct, 1)}% | "
                 f"{'yes' if r.plateau_within_tested_range else 'NO - range insufficient'} |")

    L.append("\n#### Table R4 - Marginal throughput gain from adding one AGV (components/h)\n")
    piv = gains.assign(step=gains.from_n.astype(str) + "→" + gains.to_n.astype(str)).pivot_table(
        index=["policy", "buffer_capacity"], columns="step", values="throughput_gain_per_hr")
    L.append("| Policy | Buffer | " + " | ".join(piv.columns) + " |")
    L.append("|---|---:|" + "---:|" * len(piv.columns))
    for (pol, buf), r in piv.iterrows():
        L.append(f"| {POLICY_LABEL[pol]} | {buf} | " + " | ".join(f"{v:+.2f}" for v in r.values) + " |")

    L.append("\n#### Table R5 - Paired comparison: nearest-pickup minus FIFO (same arrival streams)\n")
    L.append("| Buffer | AGVs | Δ throughput /h [95% CI] | Verdict | Δ wait, assigned jobs (min) | "
             "Δ wait lower bound (min) [95% CI] | Δ empty km total [95% CI] | Δ empty m per delivered job [95% CI] |")
    L.append("|---:|---:|---|---|---:|---|---|---|")
    d = lambda r, k=2: _ci(r.mean_diff_nearest_minus_fifo, r.diff_ci_low, r.diff_ci_high, k)
    for (buf, n), g in paired.groupby(["buffer_capacity", "n_agvs"], sort=True):
        g = g.set_index("metric")
        th, w, wl = g.loc["throughput_per_hr"], g.loc["mean_transport_wait_min"], g.loc["mean_transport_wait_lower_bound_min"]
        e, ej = g.loc["empty_travel_km"], g.loc["empty_m_per_delivered_job"]
        L.append(f"| {buf} | {n} | {d(th)} | {th.verdict_95pct} | {_f(w.mean_diff_nearest_minus_fifo)} | {d(wl)} | "
                 f"{d(e)} | {d(ej, 1)} |")

    L.append("\n#### Table R7 - Utilization of each AGV (% of the 480-min window, mean over replications)\n")
    L.append("| Policy | Buffer | AGVs | AGV 1 | AGV 2 | AGV 3 | AGV 4 | AGV 5 | Fleet mean |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in agg.sort_values(GROUP).iterrows():
        L.append(f"| {POLICY_LABEL[r.policy]} | {r.buffer_capacity} | {r.n_agvs} | "
                 + " | ".join(_f(r[f"agv{i}_util_pct_mean"], 1) for i in range(1, 6))
                 + f" | {_f(r.fleet_util_pct_mean, 1)} |")

    if len(buf_cmp):
        L.append("\n#### Table R6 - Paired effect of buffer capacity 4 vs 1 (buffer 4 minus buffer 1)\n")
        L.append("| Policy | AGVs | Δ throughput /h [95% CI] | Verdict | Δ M1 blocked min [95% CI] | Δ M2 blocked min [95% CI] |")
        L.append("|---|---:|---|---|---|---|")
        for (pol, n), g in buf_cmp.groupby(["policy", "n_agvs"], sort=True):
            g = g.set_index("metric")
            th, b1, b2 = g.loc["throughput_per_hr"], g.loc["m1_blocked_min"], g.loc["m2_blocked_min"]
            L.append(f"| {POLICY_LABEL[pol]} | {n} | {_ci(th.mean_diff, th.diff_ci_low, th.diff_ci_high)} | "
                     f"{th.verdict_95pct} | {_ci(b1.mean_diff, b1.diff_ci_low, b1.diff_ci_high, 1)} | "
                     f"{_ci(b2.mean_diff, b2.diff_ci_low, b2.diff_ci_high, 1)} |")
    return "\n".join(L) + "\n"


BEGIN_MARK = "<!-- BEGIN AUTO-GENERATED TABLES (do not edit by hand; regenerated by main.py) -->"
END_MARK = "<!-- END AUTO-GENERATED TABLES -->"


def insert_tables_into_report(report_path, tables_md) -> bool:
    """Replace the block between the markers in the report with freshly generated tables."""
    if not os.path.exists(report_path):
        return False
    text = open(report_path, encoding="utf-8").read()
    if BEGIN_MARK not in text or END_MARK not in text:
        return False
    head, rest = text.split(BEGIN_MARK, 1)
    _, tail = rest.split(END_MARK, 1)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(head + BEGIN_MARK + "\n\n" + tables_md + "\n" + END_MARK + tail)
    return True


def analyse(raw: pd.DataFrame, results_dir: str, report_path: str | None = None) -> dict:
    """Run the full analysis on replication-level results and write every output file."""
    fig_dir = os.path.join(results_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    agg = aggregate(raw)
    paired = paired_policy_comparison(raw)
    buf_cmp = paired_buffer_comparison(raw) if {1, 4} <= set(raw.buffer_capacity) else pd.DataFrame()
    sizing = fleet_sizing(agg)
    gains = marginal_gains(agg)
    checks = analytic_checks()

    agg.to_csv(os.path.join(results_dir, "aggregated_results.csv"), index=False)
    paired.to_csv(os.path.join(results_dir, "paired_policy_comparison.csv"), index=False)
    if len(buf_cmp):
        buf_cmp.to_csv(os.path.join(results_dir, "paired_buffer_comparison.csv"), index=False)
    sizing.to_csv(os.path.join(results_dir, "fleet_sizing.csv"), index=False)
    gains.to_csv(os.path.join(results_dir, "marginal_gains.csv"), index=False)
    checks.to_csv(os.path.join(results_dir, "analytic_checks.csv"), index=False)

    figures = make_figures(raw, agg, paired, fig_dir)
    tables = markdown_tables(agg, sizing, paired, buf_cmp, gains, checks)
    with open(os.path.join(results_dir, "results_tables.md"), "w", encoding="utf-8") as fh:
        fh.write("# Auto-generated results tables\n\n" + tables)
    inserted = insert_tables_into_report(report_path, tables) if report_path else False
    return {"aggregated": agg, "paired": paired, "buffer": buf_cmp, "sizing": sizing,
            "gains": gains, "figures": figures, "tables_md": tables, "report_updated": inserted}
