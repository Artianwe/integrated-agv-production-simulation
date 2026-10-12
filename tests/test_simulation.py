"""Automated tests for the simulation model (run with:  python -m pytest -q)."""

import math

import pytest
import simpy

from dispatch import Dispatcher, oldest_job_per_station, select_fifo, select_nearest
from simulation import (BLOCKED, EXPECTED_EVENTS, FG, IDLE, M1, M2, RAW, WAIT_FOR_SPACE,
                        Buffer, Component, Factory, Machine, SimConfig, check_invariants,
                        generate_arrival_times, interval_overlap, run_simulation, travel_time_min)

TOL = 1e-9


# ---------------------------------------------------------------------------
# 1. Equations
# ---------------------------------------------------------------------------
def test_travel_time_120m_at_0_6_mps_is_3_33_min():
    # Independent hand calculation: 0.6 m/s * 60 s = 36 m/min ; 120 / 36 = 3.333 min (= 200 s)
    assert travel_time_min(120, 0.6) == pytest.approx(10 / 3)
    assert travel_time_min(120, 0.6) * 60 == pytest.approx(200.0)       # seconds
    assert travel_time_min(360, 0.6) == pytest.approx(10.0)
    assert travel_time_min(0, 0.6) == 0.0


def test_travel_time_rejects_negative_distance():
    with pytest.raises(ValueError):
        travel_time_min(-1, 0.6)


def test_interval_overlap():
    assert interval_overlap(0, 100, 60, 540) == 40          # crosses warm-up boundary
    assert interval_overlap(500, 600, 60, 540) == 40        # crosses end of run
    assert interval_overlap(100, 200, 60, 540) == 100       # fully inside
    assert interval_overlap(0, 50, 60, 540) == 0            # fully outside


# ---------------------------------------------------------------------------
# 2. Random streams and reproducibility
# ---------------------------------------------------------------------------
def test_same_seed_gives_identical_results():
    cfg = SimConfig(num_agvs=2, dispatch_policy="nearest", buffer_capacity=2, replication=3)
    m1, _ = run_simulation(cfg)
    m2, _ = run_simulation(cfg)
    assert m1 == pytest.approx(m2, nan_ok=True)


def test_replications_use_distinct_arrival_streams():
    streams = [tuple(generate_arrival_times(10, 540, 42, r)) for r in range(20)]
    assert len(set(streams)) == 20


def test_common_random_numbers_across_configurations():
    """Replication r sees the same arrivals whatever the fleet, buffer or policy."""
    a = Factory(SimConfig(num_agvs=1, dispatch_policy="fifo", buffer_capacity=1, replication=5))
    b = Factory(SimConfig(num_agvs=5, dispatch_policy="nearest", buffer_capacity=4, replication=5))
    assert a.arrival_times == b.arrival_times
    c = Factory(SimConfig(replication=6))
    assert a.arrival_times != c.arrival_times


def test_mean_interarrival_is_close_to_10_min():
    gaps = []
    for r in range(200):
        t = generate_arrival_times(10, 540, 42, r)
        gaps += [y - x for x, y in zip([0.0] + t[:-1], t)]
    assert sum(gaps) / len(gaps) == pytest.approx(10, rel=0.05)


# ---------------------------------------------------------------------------
# 3. A single component, worked out by hand
# ---------------------------------------------------------------------------
def test_single_component_timeline_matches_hand_calculation():
    """One component arriving at t = 1 min, one AGV.

    Each transport leg = pickup 0.25 + 120 m travel 3.333 + drop-off 0.25 = 3.8333 min,
    and the AGV is always already at the pickup station (empty distance 0).
    Completion = 1 + 3 legs * 3.8333 + 6 (M1) + 7 (M2) = 25.5 min.
    """
    f = Factory(SimConfig(num_agvs=1, buffer_capacity=1), arrival_times=[1.0])
    f.run()
    c = f.components[0]
    assert c.completion_time == pytest.approx(25.5)
    assert [(e, loc) for _, e, loc in c.history] == EXPECTED_EVENTS
    agv = f.agvs[0]
    assert sum(d for *_, d in agv.empty_segments) == 0           # always already in place
    assert sum(d for *_, d in agv.loaded_segments) == 360        # 3 x 120 m loaded
    assert agv.position == 360


# ---------------------------------------------------------------------------
# 4. Buffers and machine blocking (tested in isolation)
# ---------------------------------------------------------------------------
class _FakeFactory:
    def __init__(self):
        self.jobs = []

    def update_deadlock_status(self):
        pass

    def create_job(self, component, location):
        self.jobs.append((component.component_id, location))


def test_buffer_respects_capacity():
    env = simpy.Environment()
    buf = Buffer(env, "test", capacity=2)
    r1, r2, r3 = buf.request_slot(), buf.request_slot(), buf.request_slot()
    assert r1.triggered and r2.triggered and not r3.triggered   # third request must wait
    a, b = Component(1, 0), Component(2, 0)
    buf.add(a)
    buf.add(b)

    def taker():
        yield env.timeout(5)
        yield from buf.take_specific(a)

    env.process(taker())
    env.run(until=10)
    assert r3.triggered and r3.ok        # slot freed at t = 5
    assert buf.max_occupancy == 2


def test_machine_blocking_after_service():
    """Capacity 1 buffers. Three parts are fed in; nobody empties the output until t = 20.

    Expected:  part1 0-6 -> output ; part2 6-12 -> BLOCKED 12-20 (output full)
               t=20 output emptied -> part2 enters output, part3 (waiting in the input
               buffer since t=6) is processed 20-26, then the machine is blocked again
               from 26 because the output still holds part2.
    """
    env = simpy.Environment()
    fake = _FakeFactory()
    m = Machine(env, fake, M1, 6.0, buffer_capacity=1)
    parts = [Component(i, 0) for i in (1, 2, 3)]

    def feeder():
        for p in parts:
            yield m.input_buffer.request_slot()
            m.input_buffer.add(p)

    def remover():
        yield env.timeout(20)
        yield from m.output_buffer.take_specific(parts[0])

    snapshot = {}

    def observer():
        yield env.timeout(8)
        snapshot["input"] = m.input_buffer.occupancy
        snapshot["current"] = m.current.component_id
        yield env.timeout(7)        # t = 15
        snapshot["state_15"] = m.state

    env.process(feeder())
    env.process(remover())
    env.process(observer())
    env.run(until=30)
    m.close_open_interval(30)

    assert m.processing_intervals == [(0, 6), (6, 12), (20, 26)]
    assert m.blocked_intervals == [(12, 20), (26, 30)]
    assert snapshot == {"input": 1, "current": 2, "state_15": BLOCKED}
    # input buffer capacity counts only WAITING parts: 1 waiting while 1 is processed
    assert m.input_buffer.max_occupancy == 1
    assert m.output_buffer.max_occupancy == 1
    # Blocked time is not processing time: 18 min processing in [0, 30]
    assert sum(e - s for s, e in m.processing_intervals) == 18


# ---------------------------------------------------------------------------
# 5. Dispatch policies (pure functions)
# ---------------------------------------------------------------------------
class _A:
    def __init__(self, agv_id, position, idle_since):
        self.agv_id, self.position, self.idle_since = agv_id, position, idle_since


class _J:
    def __init__(self, job_id, loc, created):
        from simulation import POSITIONS_M
        self.job_id, self.pickup_location, self.created_time = job_id, loc, created
        self.pickup_position = POSITIONS_M[loc]


def test_fifo_picks_oldest_job_and_longest_idle_agv():
    agvs = [_A(1, 240, 5.0), _A(2, 0, 2.0), _A(3, 120, 2.0)]
    jobs = [_J(1, M2, 9.0), _J(2, RAW, 3.0), _J(3, M1, 4.0)]
    agv, job = select_fifo(agvs, jobs)
    assert job.job_id == 2 and agv.agv_id == 2       # tie on idle_since -> lower id


def test_nearest_picks_smallest_empty_distance():
    agvs = [_A(1, 0, 0), _A(2, 240, 0)]
    jobs = [_J(1, RAW, 1.0), _J(2, M2, 5.0)]
    agv, job = select_nearest(agvs, jobs)
    assert job.job_id == 1 and agv.agv_id == 1        # distance 0 (tie with AGV2/job2 -> older job)


def test_nearest_only_considers_oldest_job_at_each_station():
    jobs = [_J(1, M1, 7.0), _J(2, M1, 3.0), _J(3, RAW, 5.0)]
    eligible = {j.job_id for j in oldest_job_per_station(jobs)}
    assert eligible == {2, 3}
    agv, job = select_nearest([_A(1, 120, 0)], jobs)
    assert job.job_id == 2


def test_nearest_tie_breaks_creation_time_then_agv_id():
    agvs = [_A(2, 120, 0), _A(1, 120, 0)]
    jobs = [_J(1, RAW, 4.0), _J(2, M2, 2.0)]          # both 120 m away
    agv, job = select_nearest(agvs, jobs)
    assert job.job_id == 2 and agv.agv_id == 1


def test_dispatcher_never_assigns_a_job_or_agv_twice():
    env = simpy.Environment()
    d = Dispatcher(env, "nearest")
    received = []

    class _Agv(_A):
        def assign(self, job):
            received.append((self.agv_id, job.job_id))

    for a in (_Agv(1, 0, 0), _Agv(2, 0, 0), _Agv(3, 240, 0)):
        d.idle_agvs.append(a)
    for j in (_J(1, RAW, 0), _J(2, RAW, 0.5)):
        j.assigned_time = j.agv_id = None
        d.pending_jobs.append(j)
    d.dispatch()
    assert len(received) == 2
    assert len({a for a, _ in received}) == 2 and len({j for _, j in received}) == 2
    assert len(d.idle_agvs) == 1 and not d.pending_jobs


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        Dispatcher(simpy.Environment(), "random")


# ---------------------------------------------------------------------------
# 6. Whole-model invariants over many configurations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("policy", ["fifo", "nearest"])
@pytest.mark.parametrize("n_agvs", [1, 2, 3, 5])
@pytest.mark.parametrize("buffer_capacity", [1, 2, 4])
def test_invariants_hold(policy, n_agvs, buffer_capacity):
    for rep in range(3):
        cfg = SimConfig(num_agvs=n_agvs, dispatch_policy=policy, buffer_capacity=buffer_capacity, replication=rep)
        metrics, f = run_simulation(cfg, check=False)
        assert check_invariants(f, metrics) == []
        assert metrics["throughput_per_hr"] >= 0
        assert metrics["empty_travel_km"] >= 0
        assert 0 <= metrics["fleet_util_pct"] <= 100
        for i in range(1, n_agvs + 1):
            assert 0 <= metrics[f"agv{i}_util_pct"] <= 100


def test_checker_detects_a_planted_violation():
    _, f = run_simulation(SimConfig(num_agvs=2, dispatch_policy="nearest"))
    f.machines[M1].processing_intervals.append((100.0, 103.0))   # overlapping fake job
    assert any("two components at once" in p for p in check_invariants(f))


def test_component_counted_only_after_delivery_to_finished_goods():
    _, f = run_simulation(SimConfig(num_agvs=3, dispatch_policy="nearest", buffer_capacity=2))
    for c in f.components:
        events = [e for _, e, _ in c.history]
        if c.completion_time is None:
            assert "completed" not in events
        else:
            t_delivered = [t for t, e, loc in c.history if e == "delivered" and loc == FG][0]
            assert c.completion_time == t_delivered
    # components that finished Machine 2 but are not yet delivered are NOT completed
    waiting = [c for c in f.components if any(e == "process_end" and loc == M2 for _, e, loc in c.history)
               and c.completion_time is None]
    assert all(c not in f.completed for c in waiting)


def test_agv_waits_to_unload_when_input_buffer_full():
    """Burst of 12 parts in the first 1.2 min with capacity-1 buffers forces AGVs to wait."""
    arrivals = [0.1 * k for k in range(1, 13)]
    f = Factory(SimConfig(num_agvs=3, dispatch_policy="nearest", buffer_capacity=1), arrival_times=arrivals)
    metrics = f.run()
    assert check_invariants(f, metrics) == []
    waits = [(a, s, e) for a in f.agvs for s, e in a.wait_for_space_intervals]
    assert waits, "expected at least one AGV to wait for buffer space"
    for agv, s, e in waits:
        # waiting time is part of an assigned (busy) job
        assert any(bs <= s + TOL and e <= be + TOL for bs, be in agv.busy_intervals)
        assert (s, WAIT_FOR_SPACE) in agv.state_log


def test_single_agv_never_carries_two_components():
    _, f = run_simulation(SimConfig(num_agvs=1, dispatch_policy="nearest", buffer_capacity=4))
    a = f.agvs[0]
    picks = sorted(j.pickup_time for j in f.jobs if j.pickup_time is not None)
    drops = sorted(j.delivered_time for j in f.jobs if j.delivered_time is not None)
    # between two consecutive pickups there must be a drop-off
    for p_now, p_next in zip(picks, picks[1:]):
        assert any(p_now < d <= p_next for d in drops)
    assert a.load is None or a.load in f.components


def test_utilization_matches_independent_state_log_calculation():
    """Busy time recomputed from the AGV state log must equal the reported utilization."""
    cfg = SimConfig(num_agvs=3, dispatch_policy="fifo", buffer_capacity=4, replication=2)
    metrics, f = run_simulation(cfg)
    lo, hi = cfg.warmup_min, cfg.sim_duration_min
    for a in f.agvs:
        log = a.state_log + [(hi, "END")]
        busy = sum(interval_overlap(t0, t1, lo, hi) for (t0, s), (t1, _) in zip(log, log[1:]) if s != IDLE)
        assert 100 * busy / (hi - lo) == pytest.approx(metrics[f"agv{a.agv_id}_util_pct"], abs=1e-6)


def test_deadlock_detection_reports_a_genuine_deadlock():
    """FIFO, 5 AGVs, capacity 1, replication 0 deadlocks (found during development).
    Verify the end state really is stuck: every AGV is loaded and waiting at a machine
    whose output buffer is full, and nothing is completed after the onset."""
    metrics, f = run_simulation(SimConfig(num_agvs=5, dispatch_policy="fifo", buffer_capacity=1, replication=0))
    assert metrics["deadlocked"]
    onset = metrics["deadlock_onset_min"]
    for a in f.agvs:
        assert a.state == WAIT_FOR_SPACE and a.load is not None
        m = f.machines[a.waiting_at]
        assert m.state == BLOCKED and not m.output_buffer.has_free_slot()
        assert not m.input_buffer.has_free_slot()
    assert all(c.completion_time <= onset for c in f.completed)


def test_no_deadlock_flag_when_system_flows():
    metrics, _ = run_simulation(SimConfig(num_agvs=5, dispatch_policy="nearest", buffer_capacity=4))
    assert not metrics["deadlocked"] and math.isnan(metrics["deadlock_onset_min"])


def test_both_policies_see_same_operating_conditions():
    a = Factory(SimConfig(dispatch_policy="fifo", replication=1))
    b = Factory(SimConfig(dispatch_policy="nearest", replication=1))
    assert a.arrival_times == b.arrival_times
    for name in (M1, M2):
        assert a.machines[name].process_time_min == b.machines[name].process_time_min
        assert a.machines[name].input_buffer.capacity == b.machines[name].input_buffer.capacity
