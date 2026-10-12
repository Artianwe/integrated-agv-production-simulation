"""
dispatch.py - AGV dispatch policies and the central dispatcher.

The dispatcher keeps two pools:
    * idle AGVs       (vehicles waiting for work)
    * pending jobs    (transport requests that no AGV has accepted yet)

Whenever something changes (a new job appears, or an AGV becomes idle) the
dispatcher is called. It repeatedly asks the active POLICY to pick one
(AGV, job) pair, assigns it, and removes BOTH from their pools before picking
the next pair. Removing them immediately is what "reserves" a job and a vehicle,
so neither can be assigned twice - even when several assignments happen at the
same simulation time.

No polling is used: dispatching is purely event-driven.

The two policies are written as small pure functions so they can be unit-tested
without running a simulation.
"""

from __future__ import annotations


# ----------------------------------------------------------------------------
# Policy A: FIFO
# ----------------------------------------------------------------------------
def select_fifo(idle_agvs, pending_jobs):
    """Pick (agv, job) using First-In-First-Out.

    Job choice: the OLDEST waiting job (smallest creation time; job id breaks
                exact ties).
    AGV choice: the AGV that has been idle the LONGEST (smallest idle_since;
                AGV id breaks ties).

    Returns None if either pool is empty.
    """
    if not idle_agvs or not pending_jobs:
        return None
    job = min(pending_jobs, key=lambda j: (j.created_time, j.job_id))
    agv = min(idle_agvs, key=lambda a: (a.idle_since, a.agv_id))
    return agv, job


# ----------------------------------------------------------------------------
# Policy B: nearest pickup
# ----------------------------------------------------------------------------
def oldest_job_per_station(pending_jobs):
    """Return only the oldest pending job at each pickup station.

    If three finished components wait at Machine 1, only the oldest of them is
    eligible. This stops the policy from 'jumping the queue' at one station.
    """
    oldest = {}
    for job in pending_jobs:
        current = oldest.get(job.pickup_location)
        if current is None or (job.created_time, job.job_id) < (current.created_time, current.job_id):
            oldest[job.pickup_location] = job
    return list(oldest.values())


def select_nearest(idle_agvs, pending_jobs):
    """Pick the (agv, job) pair with the smallest EMPTY travel distance.

    empty distance = |AGV position - job pickup position|

    Ties are broken by job creation time, then AGV id (then job id, which can
    only matter if two jobs have the identical creation time).
    Returns None if either pool is empty.
    """
    if not idle_agvs or not pending_jobs:
        return None
    candidates = oldest_job_per_station(pending_jobs)
    best_key, best_pair = None, None
    for agv in idle_agvs:
        for job in candidates:
            distance = abs(agv.position - job.pickup_position)
            key = (distance, job.created_time, agv.agv_id, job.job_id)
            if best_key is None or key < best_key:
                best_key, best_pair = key, (agv, job)
    return best_pair


POLICIES = {
    "fifo": select_fifo,
    "nearest": select_nearest,
}


# ----------------------------------------------------------------------------
# Central dispatcher
# ----------------------------------------------------------------------------
class Dispatcher:
    """Event-driven central dispatcher shared by all AGVs."""

    def __init__(self, env, policy_name):
        if policy_name not in POLICIES:
            raise ValueError(f"Unknown dispatch policy '{policy_name}'. Choose from {list(POLICIES)}")
        self.env = env
        self.policy_name = policy_name
        self.select = POLICIES[policy_name]
        self.idle_agvs = []
        self.pending_jobs = []
        self.assignments_made = 0

    # Called by the factory when a component becomes ready for pickup.
    def add_job(self, job):
        self.pending_jobs.append(job)
        self.dispatch()

    # Called by an AGV when it has finished a job (or at the start of the run).
    def agv_became_idle(self, agv):
        self.idle_agvs.append(agv)
        self.dispatch()

    def dispatch(self):
        """Assign as many (AGV, job) pairs as possible right now."""
        while True:
            pair = self.select(self.idle_agvs, self.pending_jobs)
            if pair is None:
                return
            agv, job = pair
            # Reserve both: remove from the pools BEFORE choosing the next pair.
            self.idle_agvs.remove(agv)
            self.pending_jobs.remove(job)
            job.assigned_time = self.env.now
            job.agv_id = agv.agv_id
            self.assignments_made += 1
            agv.assign(job)
