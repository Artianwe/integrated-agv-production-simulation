# Integrated AGV–Production-Line Simulation

Discrete-event simulation (Python + SimPy) of a two-machine production line served by Automated Guided Vehicles. It studies how **AGV fleet size**, **dispatch policy** (FIFO vs nearest pickup) and **buffer capacity** affect throughput, waiting, empty travel, utilization and machine blocking.

> All parameters are illustrative assumptions, not measurements from a real factory.

```
Raw store (0 m) → AGV → Machine 1 (120 m, 6 min) → AGV → Machine 2 (240 m, 7 min) → AGV → Finished goods (360 m)
```

## Install

Python 3.10+ is recommended.

```bash
pip install -r requirements.txt
```

## Run

| Command | What it does |
|---|---|
| `python main.py --run-all` | Full experiment (600 runs), CSV files, 8 figures, tables inserted into `technical_report.md`, then the automated tests. Takes a few seconds. |
| `python main.py --quick` | 24-run check of the whole pipeline; writes to `results_quick/`. |
| `python main.py --single --agvs 3 --policy nearest --buffer 2 --rep 0` | One run; prints every metric. |
| `python -m pytest -q` | Automated tests only. |

Options: `--seed`, `--replications`, `--results-dir`, `--no-tests`. The report tables are only overwritten by the default full run (seed 42, 20 replications, default folder).

## Project structure

| File | Purpose |
|---|---|
| `simulation.py` | Layout, `Buffer`, `Machine`, `AGV`, `Factory`, metrics, invariant checks, deadlock detection |
| `dispatch.py` | FIFO and nearest-pickup policies and the central dispatcher |
| `experiments.py` | Factorial design (full and quick) and the batch runner |
| `analysis.py` | Confidence intervals, paired comparisons, fleet sizing, figures, markdown tables |
| `main.py` | Command-line entry point |
| `tests/` | 57 automated tests (equations, blocking, buffers, dispatch, reproducibility, output consistency) |
| `technical_report.md` | Full report with results |
| `viva_notes.md` | Plain-English explanation and 20 interview questions with answers |
| `results/` | Generated outputs (see below) |

## Outputs (`results/`)

| File | Content |
|---|---|
| `replications.csv` | One row per run (600) with every metric |
| `aggregated_results.csv` | Mean, std, 95% CI per configuration |
| `paired_policy_comparison.csv` | Nearest − FIFO, paired by replication |
| `paired_buffer_comparison.csv` | Buffer 4 − buffer 1, paired by replication |
| `fleet_sizing.csv` | 95%-of-best fleet-sizing rule and plateau check |
| `marginal_gains.csv` | Throughput gain per added AGV |
| `analytic_checks.csv` | Hand-calculation checks (travel time, capacities) |
| `model_parameters.csv` | Parameters used |
| `results_tables.md` | All tables in markdown (also inserted into the report) |
| `run_log.txt` | Versions, run count, runtime, deadlock counts, test status |
| `figures/fig1…fig8*.png` | Throughput, waiting time, empty travel, AGV utilization, machine utilization/blocking, buffer comparison, policy comparison, deadlock frequency |

## Changing the model

All parameters are fields of `SimConfig` in `simulation.py` (speed, arrival mean, processing times, handling times, run length, warm-up). The experimental factors are in `FULL_DESIGN` in `experiments.py`.

## Relation to the earlier AGV Fleet Sizing Simulator

Reused and adapted: travel-time and window-overlap functions, the event-driven central dispatcher, FIFO selection, common-random-numbers seeding, waiting-time censoring, t-based CIs, paired comparison and the 95% fleet-sizing rule, plot colours. Rewritten: machines and buffers (finite buffers, blocking after service, AGVs waiting to unload), nearest-pickup restricted to the oldest job per station, new layout and parameters, per-AGV utilization, machine blocking and deadlock detection.
