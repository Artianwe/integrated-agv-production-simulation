"""Tests for the statistics, fleet-sizing rule and output consistency."""

import math
import os

import numpy as np
import pandas as pd
import pytest
from scipy import stats

import analysis
from experiments import QUICK_DESIGN, build_configs, run_experiments

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")


def test_confidence_interval_formula():
    x = [5.0, 6.0, 7.0, 6.5, 5.5]
    s = analysis.mean_std_ci(x)
    half = stats.t.ppf(0.975, 4) * np.std(x, ddof=1) / math.sqrt(5)
    assert s["mean"] == pytest.approx(6.0)
    assert s["ci_low"] == pytest.approx(6.0 - half) and s["ci_high"] == pytest.approx(6.0 + half)
    assert stats.t.ppf(0.975, 19) == pytest.approx(2.093, abs=1e-3)


def test_ci_ignores_nan_and_handles_small_samples():
    assert analysis.mean_std_ci([math.nan, 2.0])["n"] == 1
    assert math.isnan(analysis.mean_std_ci([])["mean"])


def _agg(means, policy="fifo", buf=1):
    return pd.DataFrame({"policy": policy, "buffer_capacity": buf, "n_agvs": list(range(1, len(means) + 1)),
                         "throughput_per_hr_mean": means, "throughput_per_hr_ci_low": means,
                         "throughput_per_hr_ci_high": means, "mean_transport_wait_min_mean": 0.0,
                         "fleet_util_pct_mean": 50.0, "empty_travel_km_mean": 1.0, "m2_util_pct_mean": 50.0,
                         "deadlock_pct": 0.0})


def test_fleet_sizing_rule():
    r = analysis.fleet_sizing(_agg([1.0, 4.0, 5.8, 6.0, 6.0])).iloc[0]
    assert r.best_mean_throughput_per_hr == 6.0
    assert r.threshold_per_hr == pytest.approx(5.7)
    assert r.min_fleet_95pct == 3
    assert r.plateau_within_tested_range


def test_fleet_sizing_flags_missing_plateau():
    r = analysis.fleet_sizing(_agg([1.0, 2.0, 3.0, 4.0, 5.0])).iloc[0]
    assert r.min_fleet_95pct == 5 and not r.plateau_within_tested_range


def test_aggregate_and_paired_comparison_use_the_data():
    raw = run_experiments(build_configs(**QUICK_DESIGN), verbose=False)
    agg = analysis.aggregate(raw)
    g = raw[(raw.policy == "nearest") & (raw.buffer_capacity == 4) & (raw.n_agvs == 2)]
    row = agg[(agg.policy == "nearest") & (agg.buffer_capacity == 4) & (agg.n_agvs == 2)].iloc[0]
    assert row.throughput_per_hr_mean == pytest.approx(g.throughput_per_hr.mean())
    paired = analysis.paired_policy_comparison(raw)
    p = paired[(paired.buffer_capacity == 4) & (paired.n_agvs == 2) & (paired.metric == "throughput_per_hr")].iloc[0]
    f = raw[(raw.policy == "fifo") & (raw.buffer_capacity == 4) & (raw.n_agvs == 2)].set_index("replication")
    n = g.set_index("replication")
    assert p.mean_diff_nearest_minus_fifo == pytest.approx((n.throughput_per_hr - f.throughput_per_hr).mean())


# ---------------------------------------------------------------------------
# Output consistency (only when the full experiment has been run)
# ---------------------------------------------------------------------------
needs_results = pytest.mark.skipif(not os.path.exists(os.path.join(RESULTS, "replications.csv")),
                                   reason="run `python main.py --run-all` first")


@needs_results
def test_saved_results_have_600_runs_and_valid_ranges():
    raw = pd.read_csv(os.path.join(RESULTS, "replications.csv"))
    assert len(raw) == 600
    assert raw.groupby(["policy", "buffer_capacity", "n_agvs"]).size().eq(20).all()
    assert (raw.throughput_per_hr >= 0).all() and (raw.empty_travel_km >= 0).all()
    for c in ["fleet_util_pct", "m1_util_pct", "m2_util_pct"] + [f"agv{i}_util_pct" for i in range(1, 6)]:
        v = raw[c].dropna()
        assert ((v >= 0) & (v <= 100 + 1e-6)).all(), c


@needs_results
def test_aggregated_csv_matches_replication_csv():
    raw = pd.read_csv(os.path.join(RESULTS, "replications.csv"))
    agg_saved = pd.read_csv(os.path.join(RESULTS, "aggregated_results.csv"))
    agg_new = analysis.aggregate(raw)
    pd.testing.assert_series_equal(agg_saved.throughput_per_hr_mean, agg_new.throughput_per_hr_mean,
                                   check_names=False, rtol=1e-9)


@needs_results
def test_report_tables_were_generated_from_csv():
    """The table block in technical_report.md must equal tables regenerated from the CSV files."""
    tables = open(os.path.join(RESULTS, "results_tables.md"), encoding="utf-8").read()
    raw = pd.read_csv(os.path.join(RESULTS, "replications.csv"))
    agg = analysis.aggregate(raw)
    regenerated = analysis.markdown_tables(agg, analysis.fleet_sizing(agg), analysis.paired_policy_comparison(raw),
                                           analysis.paired_buffer_comparison(raw), analysis.marginal_gains(agg),
                                           analysis.analytic_checks())
    assert tables.endswith(regenerated)
    report = os.path.join(ROOT, "technical_report.md")
    if os.path.exists(report):
        text = open(report, encoding="utf-8").read()
        block = text.split(analysis.BEGIN_MARK, 1)[1].split(analysis.END_MARK, 1)[0]
        assert block.strip() == regenerated.strip()


@needs_results
def test_all_figures_exist():
    figs = os.listdir(os.path.join(RESULTS, "figures"))
    assert len([f for f in figs if f.endswith(".png")]) == 8
