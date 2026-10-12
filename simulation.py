"""
simulation.py - Integrated AGV + two-machine production line (SimPy model).

Process route for EVERY component:

    Raw store (0 m) --AGV--> Machine 1 (120 m) --AGV--> Machine 2 (240 m) --AGV--> Finished goods (360 m)

Main building blocks
--------------------
Buffer      : a finite (or unlimited) waiting area with N "slots".
              To place a component you must first RESERVE a free slot; if none is
              free, you wait. A slot is freed when a component is taken out.
Machine     : takes the oldest component from its input buffer, processes it,
              then must reserve a slot in its output buffer. If the output buffer
              is full the machine is BLOCKED (blocking after service) and cannot
              start another component.
AGV         : carries one component at a time: empty travel -> pickup handling ->
              loaded travel -> (wait for destination space) -> drop-off handling.
Dispatcher  : (dispatch.py) assigns idle AGVs to waiting transport jobs.
Factory     : wires everything together, records events and computes metrics.

All times are in MINUTES and all distances in METRES inside the model.
All parameter values are illustrative assumptions, not factory measurements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import simpy

from dispatch import Dispatcher

# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------
RAW = "raw_store"
M1 = "machine_1"
M2 = "machine_2"
FG = "finished_goods"

POSITIONS_M = {RAW: 0.0, M1: 120.0, M2: 240.0, FG: 360.0}

# Where a component picked up at location X must be taken next.
NEXT_DESTINATION = {RAW: M1, M1: M2, M2: FG}

# AGV states
IDLE = "idle"
EMPTY_TRAVEL = "travelling_empty"
PICKUP = "pickup_handling"
LOADED_TRAVEL = "travelling_loaded"
WAIT_FOR_SPACE = "waiting_for_buffer_space"
DROPOFF = "dropoff_handling"

# Machine states
STARVED = "starved"        # waiting for a component to arrive in its input buffer
PROCESSING = "processing"
BLOCKED = "blocked"        # finished a part but output buffer is full


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class SimConfig:
    num_agvs: int = 2
    dispatch_policy: str = "fifo"          # "fifo" or "nearest"
    buffer_capacity: int = 2               # same capacity for every machine input & output buffer
    agv_speed_m_per_s: float = 0.6
    mean_interarrival_min: float = 10.0
    m1_process_min: float = 6.0
    m2_process_min: float = 7.0
    pickup_handling_min: float = 0.25
    dropoff_handling_min: float = 0.25
    sim_duration_min: float = 540.0
    warmup_min: float = 60.0
    base_seed: int = 42
    replication: int = 0

    @property
    def agv_speed_m_per_min(self) -> float:
        return self.agv_speed_m_per_s * 60.0

    @property
    def measurement_min(self) -> float:
        return self.sim_duration_min - self.warmup_min


def travel_time_min(distance_m: float, speed_m_per_s: float) -> float:
    """Travel time in minutes:  t = d / v,  with v converted from m/s to m/min."""
    if distance_m < 0:
        raise ValueError("distance must be non-negative")
    return distance_m / (speed_m_per_s * 60.0)


def interval_overlap(start: float, end: float, window_start: float, window_end: float) -> float:
    """Length of the part of [start, end] that lies inside [window_start, window_end]."""
    return max(0.0, min(end, window_end) - max(start, window_start))


# ----------------------------------------------------------------------------
# Random numbers
# ----------------------------------------------------------------------------
def arrival_rng(base_seed: int, replication: int) -> np.random.Generator:
    """Dedicated generator for the arrival process.

    The seed depends ONLY on (base_seed, replication) - not on fleet size,
    buffer size or policy. So replication r sees exactly the same arrival
    sequence in every configuration (common random numbers), which makes
    paired comparisons fair. Different replications get statistically
    independent streams via NumPy's SeedSequence.
    """
    return np.random.default_rng(np.random.SeedSequence([base_seed, replication]))


def generate_arrival_times(mean_interarrival_min: float, horizon_min: float,
                           base_seed: int, replication: int) -> list[float]:
    """Pre-generate all component arrival times up to the simulation horizon."""
    rng = arrival_rng(base_seed, replication)
    times, t = [], 0.0
    while True:
        t += rng.exponential(mean_interarrival_min)
        if t > horizon_min:
            return times
        times.append(t)


# ----------------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------------
class Component:
    def __init__(self, component_id: int, arrival_time: float):
        self.component_id = component_id
        self.arrival_time = arrival_time
        self.completion_time = None
        self.history = []   # list of (time, event, location) - used for validation

    def log(self, time, event, location):
        self.history.append((time, event, location))


class TransportJob:
    """One request to move one component from a pickup station to its next destination."""

    def __init__(self, job_id, component, pickup_location, created_time):
        self.job_id = job_id
        self.component = component
        self.pickup_location = pickup_location
        self.destination = NEXT_DESTINATION[pickup_location]
        self.pickup_position = POSITIONS_M[pickup_location]
        self.destination_position = POSITIONS_M[self.destination]
        self.created_time = created_time     # component became available at the pickup station
        self.assigned_time = None
        self.pickup_time = None              # pickup handling finished (component on the AGV)
        self.delivered_time = None           # drop-off handling finished
        self.agv_id = None


# ----------------------------------------------------------------------------
# Buffer
# ----------------------------------------------------------------------------
class Buffer:
    """Waiting area with `capacity` slots (capacity=None means unlimited).

    Putting a component in is a two-step action:
        1. request_slot()  -> SimPy event; waits while every slot is taken
        2. add(component)  -> the component is now physically in the buffer
    Taking a component out frees its slot (release happens inside the take methods).
    """

    def __init__(self, env, name, capacity=None):
        self.env = env
        self.name = name
        self.capacity = capacity
        self.items = simpy.FilterStore(env)   # unlimited holder; the slots enforce capacity
        self.slots = simpy.Container(env, capacity=capacity, init=capacity) if capacity is not None else None
        self.max_occupancy = 0

    @property
    def occupancy(self) -> int:
        return len(self.items.items)

    def has_free_slot(self) -> bool:
        return self.slots is None or self.slots.level > 0

    def request_slot(self):
        if self.slots is None:
            ev = self.env.event()
            ev.succeed()
            return ev
        return self.slots.get(1)

    def add(self, component):
        self.items.put(component)  # never blocks (FilterStore is unlimited)
        if self.capacity is not None and self.occupancy > self.capacity:
            raise RuntimeError(f"Buffer {self.name} over capacity: {self.occupancy} > {self.capacity}")
        self.max_occupancy = max(self.max_occupancy, self.occupancy)

    def _release_slot(self):
        if self.slots is not None:
            self.slots.put(1)

    def take_oldest(self):
        """Generator: wait for, and remove, the oldest component (FIFO). Used by machines."""
        component = yield self.items.get()
        self._release_slot()
        return component

    def take_specific(self, component):
        """Generator: remove a specific component that is known to be present. Used by AGVs."""
        if component not in self.items.items:
            raise RuntimeError(f"Component {component.component_id} not in buffer {self.name}")
        taken = yield self.items.get(lambda c: c is component)
        self._release_slot()
        return taken


# ----------------------------------------------------------------------------
# Machine
# ----------------------------------------------------------------------------
class Machine:
    def __init__(self, env, factory, name, process_time_min, buffer_capacity):
        self.env = env
        self.factory = factory
        self.name = name
        self.process_time_min = process_time_min
        self.input_buffer = Buffer(env, f"{name}_input", buffer_capacity)
        self.output_buffer = Buffer(env, f"{name}_output", buffer_capacity)
        self.state = STARVED
        self.processing_intervals = []   # (start, end)
        self.blocked_intervals = []      # (start, end)
        self._open = None                # (kind, start) of an unfinished interval
        self.current = None              # component being processed or held while blocked
        self.components_processed = 0
        env.process(self.run())

    def run(self):
        while True:
            self.state = STARVED
            component = yield from self.input_buffer.take_oldest()
            self.current = component

            # --- Processing (only one component at a time) ---
            self.state = PROCESSING
            start = self.env.now
            self._open = ("processing", start)
            component.log(start, "process_start", self.name)
            yield self.env.timeout(self.process_time_min)
            self.processing_intervals.append((start, self.env.now))
            self._open = None
            self.components_processed += 1
            component.log(self.env.now, "process_end", self.name)

            # --- Blocking after service: need a free output slot ---
            slot = self.output_buffer.request_slot()
            if not slot.triggered:
                self.state = BLOCKED
                blocked_start = self.env.now
                self._open = ("blocked", blocked_start)
                self.factory.update_deadlock_status()
                yield slot
                self.blocked_intervals.append((blocked_start, self.env.now))
                self._open = None
                self.state = STARVED
                self.factory.update_deadlock_status()
            else:
                yield slot

            self.output_buffer.add(component)
            self.current = None
            component.log(self.env.now, "entered_output_buffer", self.name)
            self.factory.create_job(component, self.name)

    def close_open_interval(self, end_time):
        """At the end of the run, record an activity that is still in progress."""
        if self._open is None:
            return
        kind, start = self._open
        (self.processing_intervals if kind == "processing" else self.blocked_intervals).append((start, end_time))
        self._open = None


# ----------------------------------------------------------------------------
# AGV
# ----------------------------------------------------------------------------
class AGV:
    def __init__(self, env, factory, agv_id, start_location=RAW):
        self.env = env
        self.factory = factory
        self.config = factory.config
        self.agv_id = agv_id
        self.position = POSITIONS_M[start_location]
        self.location = start_location   # last station visited (AGVs start at the raw store)
        self.load = None                 # the component being carried (max one)
        self.state = IDLE
        self.idle_since = 0.0
        self.waiting_at = None           # machine name while WAIT_FOR_SPACE
        self.assignment_event = None
        self.state_log = [(0.0, IDLE)]
        self.busy_intervals = []         # (assigned_time, delivered_time)
        self.empty_segments = []         # (start, end, distance_m)
        self.loaded_segments = []        # (start, end, distance_m)
        self.wait_for_space_intervals = []
        self._busy_since = None
        self.jobs_completed = 0
        env.process(self.run())

    def set_state(self, state):
        self.state = state
        self.state_log.append((self.env.now, state))

    def assign(self, job):
        """Called by the dispatcher. Wakes up this AGV's process."""
        self.assignment_event.succeed(job)

    def run(self):
        while True:
            # Become idle and offer ourselves to the dispatcher.
            self.set_state(IDLE)
            self.idle_since = self.env.now
            self.assignment_event = self.env.event()
            self.factory.dispatcher.agv_became_idle(self)
            job = yield self.assignment_event
            yield from self.execute(job)

    def execute(self, job):
        cfg = self.config
        f = self.factory
        self._busy_since = self.env.now

        # 1) Empty travel to the pickup station
        self.set_state(EMPTY_TRAVEL)
        distance = abs(self.position - job.pickup_position)
        start = self.env.now
        yield self.env.timeout(travel_time_min(distance, cfg.agv_speed_m_per_s))
        self.empty_segments.append((start, self.env.now, distance))
        self.position, self.location = job.pickup_position, job.pickup_location

        # 2) Pickup handling; the component leaves the buffer when it is on the AGV
        self.set_state(PICKUP)
        yield self.env.timeout(cfg.pickup_handling_min)
        if self.load is not None:
            raise RuntimeError(f"AGV {self.agv_id} tried to carry two components")
        component = yield from f.pickup_buffer(job.pickup_location).take_specific(job.component)
        self.load = component
        job.pickup_time = self.env.now
        component.log(self.env.now, "picked_up", job.pickup_location)

        # 3) Loaded travel to the destination
        self.set_state(LOADED_TRAVEL)
        distance = abs(job.pickup_position - job.destination_position)
        start = self.env.now
        yield self.env.timeout(travel_time_min(distance, cfg.agv_speed_m_per_s))
        self.loaded_segments.append((start, self.env.now, distance))
        self.position, self.location = job.destination_position, job.destination

        # 4) Wait until the destination buffer has a free slot (AGV stays busy)
        dest_buffer = f.delivery_buffer(job.destination)
        if dest_buffer is not None:
            slot = dest_buffer.request_slot()
            if not slot.triggered:
                self.set_state(WAIT_FOR_SPACE)
                self.waiting_at = job.destination
                wait_start = self.env.now
                f.update_deadlock_status()
                yield slot
                self.wait_for_space_intervals.append((wait_start, self.env.now))
                self.waiting_at = None
                f.update_deadlock_status()
            else:
                yield slot

        # 5) Drop-off handling; the component enters the buffer when unloaded
        self.set_state(DROPOFF)
        yield self.env.timeout(cfg.dropoff_handling_min)
        self.load = None
        job.delivered_time = self.env.now
        component.log(self.env.now, "delivered", job.destination)
        if dest_buffer is None:
            f.complete(component)          # counted as finished ONLY after delivery
        else:
            dest_buffer.add(component)

        self.busy_intervals.append((self._busy_since, self.env.now))
        self._busy_since = None
        self.jobs_completed += 1

    def close_open_interval(self, end_time):
        if self._busy_since is not None:
            self.busy_intervals.append((self._busy_since, end_time))
            self._busy_since = None
        if self.state == WAIT_FOR_SPACE and self.waiting_at is not None:
            # record an unfinished wait (start = time of last state change)
            self.wait_for_space_intervals.append((self.state_log[-1][0], end_time))


# ----------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------
class Factory:
    def __init__(self, config: SimConfig, arrival_times=None):
        self.config = config
        self.env = simpy.Environment()
        self.dispatcher = Dispatcher(self.env, config.dispatch_policy)

        self.raw_store = Buffer(self.env, RAW, capacity=None)      # unlimited outbound queue
        self.machines = {
            M1: Machine(self.env, self, M1, config.m1_process_min, config.buffer_capacity),
            M2: Machine(self.env, self, M2, config.m2_process_min, config.buffer_capacity),
        }
        self.components = []
        self.jobs = []
        self.completed = []            # components delivered to finished goods

        # Deadlock bookkeeping (see update_deadlock_status)
        self.deadlock_onset = None

        if arrival_times is None:
            arrival_times = generate_arrival_times(config.mean_interarrival_min, config.sim_duration_min,
                                                   config.base_seed, config.replication)
        self.arrival_times = list(arrival_times)

        # AGVs are created after the machines; all start idle at the raw store.
        self.agvs = [AGV(self.env, self, agv_id=i + 1) for i in range(config.num_agvs)]
        self.env.process(self.arrivals())

    # -- helpers ------------------------------------------------------------
    def pickup_buffer(self, location):
        """Where an AGV collects a component: raw store, or a machine's OUTPUT buffer."""
        if location == RAW:
            return self.raw_store
        return self.machines[location].output_buffer

    def delivery_buffer(self, location):
        """Where an AGV unloads: a machine's INPUT buffer (None = finished goods, unlimited)."""
        if location == FG:
            return None
        return self.machines[location].input_buffer

    def create_job(self, component, pickup_location):
        job = TransportJob(len(self.jobs) + 1, component, pickup_location, self.env.now)
        self.jobs.append(job)
        self.dispatcher.add_job(job)

    def complete(self, component):
        component.completion_time = self.env.now
        component.log(self.env.now, "completed", FG)
        self.completed.append(component)

    # -- arrival process ----------------------------------------------------
    def arrivals(self):
        for t in self.arrival_times:
            yield self.env.timeout(t - self.env.now)
            component = Component(len(self.components) + 1, self.env.now)
            self.components.append(component)
            component.log(self.env.now, "arrived", RAW)
            self.raw_store.add(component)           # unlimited: no slot needed
            self.create_job(component, RAW)

    # -- deadlock detection -------------------------------------------------
    def deadlock_condition(self) -> bool:
        """True when the system can never make progress again.

        Every AGV is waiting to unload at a full input buffer AND every machine
        they are waiting at is blocked (its output buffer is full). Only an AGV
        could empty those output buffers, and none is free, so nothing can move.
        """
        if not self.agvs:
            return False
        if any(a.state != WAIT_FOR_SPACE for a in self.agvs):
            return False
        return all(self.machines[a.waiting_at].state == BLOCKED for a in self.agvs)

    def update_deadlock_status(self):
        if self.deadlock_condition():
            if self.deadlock_onset is None:
                self.deadlock_onset = self.env.now
        else:
            self.deadlock_onset = None

    # -- run ----------------------------------------------------------------
    def run(self):
        self.env.run(until=self.config.sim_duration_min)
        end = self.config.sim_duration_min
        for m in self.machines.values():
            m.close_open_interval(end)
        for a in self.agvs:
            a.close_open_interval(end)
        return compute_metrics(self)




# ----------------------------------------------------------------------------
# Metrics (measurement window only)
# ----------------------------------------------------------------------------
def _prorated_distance(segments, lo, hi):
    """Distance driven inside [lo, hi]. Speed is constant during a segment, so the
    distance inside the window is proportional to the time inside the window."""
    total = 0.0
    for start, end, distance in segments:
        duration = end - start
        if duration > 0:
            total += distance * interval_overlap(start, end, lo, hi) / duration
    return total


def compute_metrics(f: Factory) -> dict:
    """Performance metrics for one run, measured in the window W = [warm-up, end).

    Boundary rules (applied consistently):
      * throughput  : a component counts if it is delivered to finished goods inside W.
      * waiting time: transport jobs CREATED inside W that were assigned before the end.
                      Jobs still unassigned at the end are reported as a backlog and in
                      a lower-bound waiting time (their wait is at least end - created).
      * distance    : only the part of each travel segment that lies inside W.
      * utilization / blocking : only the overlap of each busy/blocked interval with W.
    """
    cfg = f.config
    lo, hi = cfg.warmup_min, cfg.sim_duration_min
    window_min = hi - lo
    window_h = window_min / 60.0

    # A. Throughput
    completed_in_window = [c for c in f.completed if lo <= c.completion_time < hi]
    throughput = len(completed_in_window) / window_h

    # B. Transport waiting time
    created_in_window = [j for j in f.jobs if lo <= j.created_time < hi]
    assigned = [j for j in created_in_window if j.assigned_time is not None]
    waits = [j.assigned_time - j.created_time for j in assigned]
    waits_lower_bound = [(j.assigned_time if j.assigned_time is not None else hi) - j.created_time
                         for j in created_in_window]

    # C. Distances
    empty_m = sum(_prorated_distance(a.empty_segments, lo, hi) for a in f.agvs)
    loaded_m = sum(_prorated_distance(a.loaded_segments, lo, hi) for a in f.agvs)

    deliveries_in_window = sum(1 for j in f.jobs if j.delivered_time is not None and lo <= j.delivered_time < hi)

    # D. AGV utilization (assigned -> drop-off finished, incl. waiting for buffer space)
    agv_util = {a.agv_id: 100.0 * sum(interval_overlap(s, e, lo, hi) for s, e in a.busy_intervals) / window_min
                for a in f.agvs}
    agv_wait_space_min = sum(interval_overlap(s, e, lo, hi) for a in f.agvs for s, e in a.wait_for_space_intervals)

    # E/F. Machine utilization and blocking
    m = {}
    for name, short in ((M1, "m1"), (M2, "m2")):
        mach = f.machines[name]
        busy = sum(interval_overlap(s, e, lo, hi) for s, e in mach.processing_intervals)
        blocked = sum(interval_overlap(s, e, lo, hi) for s, e in mach.blocked_intervals)
        m[f"{short}_util_pct"] = 100.0 * busy / window_min
        m[f"{short}_blocked_min"] = blocked
        m[f"{short}_blocked_pct"] = 100.0 * blocked / window_min

    flow_times = [c.completion_time - c.arrival_time for c in completed_in_window]
    deadlocked = f.deadlock_condition()

    metrics = {
        "throughput_per_hr": throughput,
        "completed_in_window": len(completed_in_window),
        "arrivals_in_window": sum(1 for t in f.arrival_times if lo <= t < hi),
        "mean_transport_wait_min": float(np.mean(waits)) if waits else math.nan,
        "mean_transport_wait_lower_bound_min": float(np.mean(waits_lower_bound)) if waits_lower_bound else math.nan,
        "jobs_created_in_window": len(created_in_window),
        "jobs_unassigned_at_end": len(f.dispatcher.pending_jobs),
        "empty_travel_km": empty_m / 1000.0,
        "loaded_travel_km": loaded_m / 1000.0,
        "transport_jobs_delivered_in_window": deliveries_in_window,
        "empty_m_per_delivered_job": empty_m / deliveries_in_window if deliveries_in_window else math.nan,
        "fleet_util_pct": float(np.mean(list(agv_util.values()))) if agv_util else math.nan,
        "agv_wait_for_space_min": agv_wait_space_min,
        **m,
        "mean_flow_time_min": float(np.mean(flow_times)) if flow_times else math.nan,
        "raw_queue_at_end": f.raw_store.occupancy,
        "wip_at_end": len(f.components) - len(f.completed),
        "deadlocked": bool(deadlocked),
        "deadlock_onset_min": f.deadlock_onset if deadlocked else math.nan,
    }
    for agv_id in range(1, 6):
        metrics[f"agv{agv_id}_util_pct"] = agv_util.get(agv_id, math.nan)
    return metrics


# ----------------------------------------------------------------------------
# Invariant checks (run after every simulation)
# ----------------------------------------------------------------------------
EXPECTED_EVENTS = [
    ("arrived", RAW), ("picked_up", RAW), ("delivered", M1),
    ("process_start", M1), ("process_end", M1), ("entered_output_buffer", M1),
    ("picked_up", M1), ("delivered", M2),
    ("process_start", M2), ("process_end", M2), ("entered_output_buffer", M2),
    ("picked_up", M2), ("delivered", FG), ("completed", FG),
]
EPS = 1e-9


def _overlaps(intervals):
    ordered = sorted(intervals)
    return any(b[0] < a[1] - EPS for a, b in zip(ordered, ordered[1:]))


def check_invariants(f: Factory, metrics: dict | None = None) -> list[str]:
    """Return a list of violated rules. An empty list means every check passed."""
    problems = []
    cfg = f.config

    # 1. Every component follows the route in order; times never go backwards;
    #    completion only after delivery to finished goods.
    for c in f.components:
        events = [(e, loc) for _, e, loc in c.history]
        if events != EXPECTED_EVENTS[:len(events)]:
            problems.append(f"component {c.component_id} broke the route: {events}")
        times = [t for t, _, _ in c.history]
        if any(t2 < t1 - EPS for t1, t2 in zip(times, times[1:])):
            problems.append(f"component {c.component_id}: time goes backwards")
        if (c.completion_time is not None) != (events == EXPECTED_EVENTS):
            problems.append(f"component {c.component_id}: completion flag inconsistent with history")

    # 2. Component conservation: nothing disappears or is duplicated.
    in_system = (f.raw_store.occupancy
                 + sum(m.input_buffer.occupancy + m.output_buffer.occupancy + (m.current is not None)
                       for m in f.machines.values())
                 + sum(a.load is not None for a in f.agvs))
    if in_system + len(f.completed) != len(f.components):
        problems.append(f"conservation broken: {len(f.components)} arrived, "
                        f"{len(f.completed)} completed, {in_system} in system")
    if len({id(c) for c in f.completed}) != len(f.completed):
        problems.append("a component was completed twice")

    # 3. Machines: one component at a time; never processing while blocked; each
    #    component processed exactly once per machine.
    for m in f.machines.values():
        if _overlaps(m.processing_intervals):
            problems.append(f"{m.name} processed two components at once")
        if _overlaps(m.processing_intervals + m.blocked_intervals):
            problems.append(f"{m.name} processed while blocked")
        for b in (m.input_buffer, m.output_buffer):
            if b.max_occupancy > b.capacity:
                problems.append(f"{b.name} exceeded capacity ({b.max_occupancy} > {b.capacity})")
    for c in f.components:
        for name in (M1, M2):
            if sum(1 for _, e, loc in c.history if e == "process_start" and loc == name) > 1:
                problems.append(f"component {c.component_id} processed twice at {name}")

    # 4. AGVs: one job at a time; one component at a time; jobs have consistent timestamps.
    for a in f.agvs:
        if _overlaps(a.busy_intervals):
            problems.append(f"AGV {a.agv_id} had overlapping jobs")
    for j in f.jobs:
        if j.assigned_time is not None and j.assigned_time < j.created_time - EPS:
            problems.append(f"job {j.job_id} assigned before creation")
        if j.pickup_time is not None and (j.assigned_time is None or j.pickup_time < j.assigned_time - EPS):
            problems.append(f"job {j.job_id} picked up before assignment")
        if j.delivered_time is not None and (j.pickup_time is None or j.delivered_time < j.pickup_time - EPS):
            problems.append(f"job {j.job_id} delivered without (or before) pickup")
    fg_deliveries = sum(1 for j in f.jobs if j.destination == FG and j.delivered_time is not None)
    if fg_deliveries != len(f.completed):
        problems.append("completed count differs from deliveries to finished goods")

    # 5. Metric ranges
    if metrics is not None:
        for key in ("throughput_per_hr", "empty_travel_km", "loaded_travel_km",
                    "m1_blocked_min", "m2_blocked_min", "agv_wait_for_space_min"):
            if metrics[key] < -EPS:
                problems.append(f"{key} is negative")
        util_keys = ["fleet_util_pct", "m1_util_pct", "m2_util_pct"] + [f"agv{i}_util_pct" for i in range(1, cfg.num_agvs + 1)]
        for key in util_keys:
            if not (-EPS <= metrics[key] <= 100 + 1e-6):
                problems.append(f"{key}={metrics[key]} outside 0-100 %")
        max_km = cfg.num_agvs * cfg.agv_speed_m_per_min * cfg.measurement_min / 1000.0
        if metrics["empty_travel_km"] + metrics["loaded_travel_km"] > max_km + 1e-6:
            problems.append("AGVs travelled further than physically possible")
    return problems


class SimulationError(Exception):
    """Raised when an invariant is violated - this indicates a bug, not a result."""


def run_simulation(config: SimConfig, check: bool = True):
    """Build and run one factory. Returns (metrics dict, Factory)."""
    factory = Factory(config)
    metrics = factory.run()
    if check:
        problems = check_invariants(factory, metrics)
        if problems:
            raise SimulationError(f"{config}: " + "; ".join(problems[:5]))
    return metrics, factory
