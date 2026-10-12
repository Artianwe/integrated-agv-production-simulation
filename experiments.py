"""
experiments.py - experimental design and batch execution.

Full factorial design:
    5 fleet sizes x 2 dispatch policies x 3 buffer capacities x 20 replications = 600 runs

Random numbers: replication r always uses the arrival stream seeded by
SeedSequence([base_seed, r]). Every configuration therefore sees the SAME arrivals
in replication r (common random numbers), while different replications are
independent. Processing, travel and handling times are deterministic, so the
arrival stream is the only source of randomness.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import asdict

import pandas as pd

from simulation import SimConfig, run_simulation

FULL_DESIGN = {
    "fleet_sizes": (1, 2, 3, 4, 5),
    "policies": ("fifo", "nearest"),
    "buffer_capacities": (1, 2, 4),
    "replications": 20,
}

# Small design for checking the model quickly (24 runs).
QUICK_DESIGN = {
    "fleet_sizes": (1, 2, 3),
    "policies": ("fifo", "nearest"),
    "buffer_capacities": (1, 4),
    "replications": 2,
}


def build_configs(fleet_sizes, policies, buffer_capacities, replications, base_seed=42, **overrides):
    """List of SimConfig objects, one per simulation run."""
    configs = []
    for policy, buffer_capacity, n_agvs, rep in itertools.product(
            policies, buffer_capacities, fleet_sizes, range(replications)):
        configs.append(SimConfig(num_agvs=n_agvs, dispatch_policy=policy, buffer_capacity=buffer_capacity,
                                 base_seed=base_seed, replication=rep, **overrides))
    return configs


def run_experiments(configs, verbose=True) -> pd.DataFrame:
    """Run every configuration (with invariant checks) and return one row per run."""
    rows = []
    t0 = time.perf_counter()
    for i, cfg in enumerate(configs, start=1):
        metrics, _ = run_simulation(cfg, check=True)   # raises SimulationError if any rule is broken
        row = {
            "policy": cfg.dispatch_policy,
            "buffer_capacity": cfg.buffer_capacity,
            "n_agvs": cfg.num_agvs,
            "replication": cfg.replication,
            "arrival_seed": f"SeedSequence([{cfg.base_seed}, {cfg.replication}])",
        }
        row.update(metrics)
        rows.append(row)
        if verbose and (i % 100 == 0 or i == len(configs)):
            print(f"  {i:4d}/{len(configs)} runs done ({time.perf_counter() - t0:.1f} s)")
    return pd.DataFrame(rows)


def config_table(design, base_seed=42) -> pd.DataFrame:
    """The fixed model parameters, saved alongside the results for traceability."""
    base = asdict(SimConfig(base_seed=base_seed))
    for key in ("num_agvs", "dispatch_policy", "buffer_capacity", "replication"):
        base.pop(key)
    base.update({
        "fleet_sizes": " ".join(map(str, design["fleet_sizes"])),
        "policies": " ".join(design["policies"]),
        "buffer_capacities": " ".join(map(str, design["buffer_capacities"])),
        "replications": design["replications"],
    })
    return pd.DataFrame({"parameter": list(base), "value": [str(v) for v in base.values()]})
