"""
main.py - command-line entry point.

    python main.py --run-all        full 600-run experiment + analysis + figures + tests
    python main.py --quick          24-run check of the whole pipeline (writes to results_quick/)
    python main.py --single --agvs 2 --policy nearest --buffer 2 --rep 0
                                    one run, printed metrics (useful for exploring)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import sys
import time

import matplotlib
import numpy as np
import pandas as pd
import scipy
import simpy

import analysis
from experiments import FULL_DESIGN, QUICK_DESIGN, build_configs, config_table, run_experiments
from simulation import SimConfig, run_simulation

HERE = os.path.dirname(os.path.abspath(__file__))


def run_pipeline(design: dict, results_dir: str, base_seed: int, update_report: bool, run_tests: bool) -> int:
    os.makedirs(results_dir, exist_ok=True)
    configs = build_configs(base_seed=base_seed, **design)
    print(f"Running {len(configs)} simulations "
          f"({len(design['fleet_sizes'])} fleets x {len(design['policies'])} policies x "
          f"{len(design['buffer_capacities'])} buffers x {design['replications']} replications)...")
    t0 = time.perf_counter()
    raw = run_experiments(configs)
    runtime = time.perf_counter() - t0

    raw_path = os.path.join(results_dir, "replications.csv")
    raw.to_csv(raw_path, index=False)
    config_table(design, base_seed).to_csv(os.path.join(results_dir, "model_parameters.csv"), index=False)

    print("Analysing results and drawing figures...")
    # Re-read the CSV so that every table and figure is produced from the saved file.
    raw_from_csv = pd.read_csv(raw_path)
    report = os.path.join(HERE, "technical_report.md") if update_report else None
    out = analysis.analyse(raw_from_csv, results_dir, report_path=report)

    test_status = "not run"
    if run_tests:
        import pytest
        print("Running automated tests...")
        code = pytest.main(["-q", os.path.join(HERE, "tests")])
        test_status = "PASSED" if code == 0 else f"FAILED (pytest exit code {int(code)})"

    dl = raw.groupby("policy").deadlocked.sum().to_dict()
    log = [
        f"Run log - generated {dt.datetime.now().isoformat(timespec='seconds')}",
        f"Python {platform.python_version()} | simpy {simpy.__version__} | numpy {np.__version__} | "
        f"pandas {pd.__version__} | scipy {scipy.__version__} | matplotlib {matplotlib.__version__}",
        f"Design: fleets {design['fleet_sizes']}, policies {design['policies']}, "
        f"buffers {design['buffer_capacities']}, replications {design['replications']}, base seed {base_seed}",
        f"Simulation runs completed: {len(raw)} in {runtime:.1f} s",
        "Invariant checks: run after EVERY simulation; any violation stops the program. All runs passed.",
        f"Runs ending in deadlock: {dl}",
        f"Figures written: {len(out['figures'])}",
        f"technical_report.md tables updated: {out['report_updated']}",
        f"Automated tests: {test_status}",
    ]
    with open(os.path.join(results_dir, "run_log.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(log) + "\n")
    print("\n".join(log))
    print(f"\nOutputs in: {os.path.abspath(results_dir)}")
    return 0 if test_status in ("PASSED", "not run") else 1


def run_single(args) -> int:
    cfg = SimConfig(num_agvs=args.agvs, dispatch_policy=args.policy, buffer_capacity=args.buffer,
                    replication=args.rep, base_seed=args.seed)
    metrics, _ = run_simulation(cfg)
    print(cfg)
    for k, v in metrics.items():
        print(f"  {k:40s} {v}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Integrated AGV - production line simulation")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-all", action="store_true", help="full 600-run factorial experiment")
    mode.add_argument("--quick", action="store_true", help="small 24-run pipeline check")
    mode.add_argument("--single", action="store_true", help="run one configuration and print its metrics")
    p.add_argument("--seed", type=int, default=42, help="base random seed (default 42)")
    p.add_argument("--replications", type=int, default=None, help="override replications per configuration")
    p.add_argument("--results-dir", default=None, help="output folder")
    p.add_argument("--no-tests", action="store_true", help="skip the automated tests after --run-all")
    p.add_argument("--agvs", type=int, default=2)
    p.add_argument("--policy", choices=["fifo", "nearest"], default="fifo")
    p.add_argument("--buffer", type=int, default=2)
    p.add_argument("--rep", type=int, default=0)
    args = p.parse_args(argv)

    if args.single:
        return run_single(args)
    design = dict(FULL_DESIGN if args.run_all else QUICK_DESIGN)
    if args.replications:
        design["replications"] = args.replications
    results_dir = args.results_dir or os.path.join(HERE, "results" if args.run_all else "results_quick")
    return run_pipeline(design, results_dir, args.seed,
                        update_report=args.run_all and args.results_dir is None and args.replications is None
                        and args.seed == 42,
                        run_tests=args.run_all and not args.no_tests)


if __name__ == "__main__":
    sys.exit(main())
