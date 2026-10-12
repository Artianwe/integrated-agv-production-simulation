# Viva / Interview Notes — Integrated AGV–Production-Line Simulation

Numbers quoted here come from `results/` (600 runs, base seed 42). If you change the model and rerun, re-check them against `results/results_tables.md`.

---

## 1. The project in plain English (≈1 minute)

"I simulated a small factory line: parts arrive at a raw-material store about every 10 minutes at random, and robots called AGVs carry each part to Machine 1, then Machine 2, then the finished-goods area, all on a 360 m straight track. Each machine has a small waiting area in front of it and behind it. If the area behind a machine is full, the machine has to stop and hold its finished part — that's called blocking. If the area in front is full, the AGV waits there with the part still on board.

I tested 1 to 5 AGVs, two dispatch rules — oldest-job-first (FIFO) and nearest-job-first — and buffer sizes of 1, 2 and 4 parts. That's 30 configurations, each repeated 20 times with different random arrivals, so 600 simulations.

The main findings: three AGVs with nearest-pickup dispatch were enough to reach 95% of the best throughput, and after that the line was limited by how fast parts arrive, not by the machines or the AGVs. The surprise was that FIFO deadlocked in 132 of its 300 runs — all AGVs got stuck waiting at a full machine that was itself blocked — while nearest-pickup never deadlocked. Larger buffers helped FIFO a lot but made no measurable difference to nearest-pickup."

## 2. Key numbers to remember

| Item | Value |
|---|---|
| Travel time for 120 m at 0.6 m/s | 120 / 36 = 3.33 min (200 s) |
| One loaded leg incl. handling | 3.83 min; 3 legs per part = 11.5 min |
| Loaded AGV work needed at 6 parts/h | 1.15 AGVs (before any empty travel) |
| Machine capacities | M1 10/h, M2 8.57/h; arrival rate 6/h (5.76/h realised in the 20 streams) |
| Throughput plateau | ≈ 5.61 parts/h |
| Nearest pickup: 1 / 2 / 3 AGVs | 2.73 / ≈4.97 / ≈5.57 parts/h |
| Min. fleet (95% rule) | Nearest: 3 for all buffers. FIFO: 3 (B=4), 4 (B=2), not saturated by 5 (B=1) |
| Machine utilization at plateau | M1 ≈ 57%, M2 ≈ 66% |
| Deadlocks | FIFO 132/300 runs; nearest 0/300 |

## 3. Model in one diagram

```
 Raw store (0 m) ──AGV──► [in-buf] M1 6 min [out-buf] ──AGV──► [in-buf] M2 7 min [out-buf] ──AGV──► Finished goods (360 m)
   unlimited                 cap B        cap B                    cap B        cap B                    unlimited
```

AGV cycle: idle → empty travel → pickup (0.25) → loaded travel → (wait for input space) → drop-off (0.25) → idle.
Machine cycle: starved → processing → (blocked until an output slot is free) → place part → starved.

---

## 4. Likely interview questions and answers

**Q1. What is discrete-event simulation, and why use it here?**
A model in which the state only changes at discrete events — an arrival, the end of processing, an AGV reaching a station. The clock jumps from one event to the next, so nothing happens "between" events. It suits this problem because the interesting behaviour — queues, blocking, vehicles waiting — comes from random arrivals interacting with finite resources, which is hard to solve analytically but easy to replay event by event. A 540-minute shift simulates in a few milliseconds.

**Q2. Why SimPy?**
It is a mature, open-source Python library for process-based DES. Each machine and AGV is written as an ordinary Python generator that `yield`s timeouts or resource requests, which reads almost like a description of the real process. It has the primitives I needed — `Container` for buffer slots and `FilterStore` for picking a specific part — and it integrates with NumPy, pandas and Matplotlib for analysis. It is also free and easy for someone else to rerun.

**Q3. How is AGV travel time calculated?**
t = d / v. Speed is 0.6 m/s = 36 m/min, distance is |x_destination − x_pickup| on the straight line. 120 m takes 3.33 min. Empty travel uses the AGV's current position to the pickup; loaded travel uses pickup to destination. Handling adds 0.25 min at each end. A unit test checks 120 m → 3.333 min (200 s) and 360 m → 10 min.

**Q4. What is the difference between FIFO and nearest-pickup dispatch?**
FIFO serves the oldest waiting job first and gives it to the AGV that has been idle the longest. Nearest-pickup picks the AGV–job pair with the smallest empty distance, but only the oldest job at each station is eligible; ties go to the older job, then the lower AGV ID. FIFO is "fair" by job age; nearest-pickup minimises wasted driving.

**Q5. Which policy was better, and why?**
Nearest-pickup was never significantly worse and was significantly better in 9 of 15 buffer/fleet combinations — the cases where AGVs were scarce. It also drove 15–54 m less empty distance per job in every combination. The main reason is that an AGV that has just dropped a part at a machine is right next to that machine's output buffer, so nearest-pickup clears outputs immediately; FIFO keeps sending AGVs back to the old raw-store jobs, which leaves machine outputs full and causes blocking and deadlock. With 4–5 AGVs (B ≥ 2) the two policies gave the same throughput.

**Q6. What is the difference between machine processing and machine blocking?**
Processing is time spent actually working on a part. Blocking is time the machine has finished a part but cannot put it into its full output buffer, so it holds the part and cannot start the next one. Utilization counts only processing. Blocked time is measured from the end of processing to the moment an output slot becomes free, clipped to the 60–540 min window.

**Q7. What role do the finite buffers play?**
They decouple the machines from the AGVs. A larger output buffer lets a machine keep working when no AGV is available; a larger input buffer lets AGVs unload without waiting. Under FIFO, increasing the buffer from 1 to 4 raised throughput by 0.78–3.09 parts/h for 1–4 AGVs and reduced deadlocks. Under nearest-pickup the buffer size made no significant difference, because the dispatch rule already kept outputs clear and the machines were not the constraint.

**Q8. How did you implement the buffers?**
Each buffer has B slots (a SimPy `Container`). To put a part in, you first reserve a slot; if none is free you wait. The slot is freed when the part is taken out. The machine removes its part from the input buffer when processing starts, so the part being processed does not count towards the input capacity. Tests check that occupancy never exceeds B.

**Q9. What is the deadlock you found?**
A circular wait. Every AGV is loaded and waiting at a machine's full input buffer; that machine is blocked because its output buffer is full; only an AGV could empty that output buffer, but all AGVs are stuck. Nothing can move again. My model doesn't prevent it — the specification says an AGV waits until it can unload — but it detects it and reports how often it happens. FIFO deadlocked in 132/300 runs, nearest-pickup in 0/300. Real systems prevent this, for example by only dispatching a job when space at the destination can be reserved.

**Q10. How do you identify the bottleneck?**
Look at which resource is busiest and which queue is growing. With 1–2 AGVs, AGVs are over 92% busy and a backlog of jobs builds up at the raw store — transport is the bottleneck. With 3+ AGVs (nearest-pickup), AGV utilization drops to ~70%, machines are busy only 57% (M1) and 66% (M2), and throughput tracks the arrival rate — so the arrival process is the limit. Machine 2 is the busiest machine and would be the next bottleneck if demand rose above 8.57/h.

**Q11. What do AGV utilization and empty travel tell you?**
Utilization is the share of the 480-min window in which an AGV is assigned to a job — travelling, handling or waiting to unload. Very high utilization (>90%) means AGVs are the constraint; low utilization means spare capacity. Empty travel is wasted driving; it is calculated from the AGV's position to the pickup, pro-rated to the measurement window. Because total empty distance grows when more work is done, I also report empty metres per delivered job, which is the fairer comparison between policies.

**Q12. How does fleet size affect throughput, and where are the diminishing returns?**
For nearest-pickup the second AGV added about 2.25 parts/h, the third about 0.6/h, and the fourth and fifth almost nothing (≤ 0.04/h). Diminishing returns start after the third AGV. In this model they are caused by demand saturation — you cannot deliver more parts than arrive — not by machine saturation. To see a machine-limited regime I would have to run higher arrival rates, which I didn't do in this study.

**Q13. How did you choose the fleet size?**
For each policy and buffer size I took the highest mean throughput among the tested fleets and found the smallest fleet within 95% of it. That gives 3 AGVs for nearest-pickup. The 95% threshold is relative to the best result in this experiment, not a theoretical maximum. For FIFO with buffer 1 throughput was still rising 13.8% at 5 AGVs, so I state that the tested range is insufficient there.

**Q14. How do random seeds and reproducibility work?**
Arrival times come from a dedicated NumPy generator seeded with `SeedSequence([42, r])` for replication r. The same r gives the same arrivals in every configuration — common random numbers — so differences between policies or fleets are not caused by luck in arrivals. Different r give independent streams. Running the program twice gives identical results; tests check both properties.

**Q15. What do the confidence intervals mean?**
For each configuration I have 20 independent replication results. The 95% CI is mean ± t(0.975, 19) · s/√20, with t = 2.093. It says how precisely 20 runs pin down the model's long-run average under these assumptions. It does not say the model matches a real factory. For policy comparisons I used paired differences on the same arrival streams, which removes shared noise and gives much narrower intervals.

**Q16. What's the difference between verification and validation, and what did you do?**
Verification: "did I build the model right?" — I wrote 57 automated tests (hand-calculated single-part timeline of 25.5 min, travel times, blocking intervals, buffer capacity, dispatch tie-breaks, reproducibility, deadlock genuineness, utilization recomputed independently from the AGV state log) and an invariant checker that runs after every one of the 600 simulations (route order, conservation of parts, no double processing, capacity, AGV one-job-at-a-time, metric ranges). I also checked flow balance: M2 utilization ≈ throughput × 7 min / 60. Validation: "did I build the right model?" — that would need real factory data, which I did not have, so the model is not validated.

**Q17. How do you handle the warm-up and activities that cross the window boundary?**
The first 60 minutes are simulated but not measured. Throughput counts deliveries between 60 and 540 min. For utilization, blocking and distance I take only the part of each interval inside the window: overlap = max(0, min(end, 540) − max(start, 60)). Distance is pro-rated by the same fraction because speed is constant.

**Q18. Why is the "mean waiting time" sometimes lower with fewer AGVs?**
Because it only averages jobs that were eventually assigned. In overloaded or deadlocked runs the longest-waiting jobs never get assigned, so they drop out of the average. That's why I also report a lower bound that includes them (their wait counted up to the end of the run) and the number of unassigned jobs. For FIFO with 1 AGV and B = 1: 18 min for assigned jobs, at least 214 min including the rest.

**Q19. What are the main limitations?**
Illustrative, uncalibrated parameters; deterministic machines with no breakdowns; constant-speed AGVs with no collisions, congestion or batteries; a straight single track; no deadlock prevention; 8-hour terminating runs with a fixed 60-min warm-up; t-based CIs without multiple-comparison correction.

**Q20. How would you extend this to a real factory?**
Collect real cycle times, arrival patterns, AGV speed/acceleration and handling times and fit distributions; validate by comparing simulated and measured throughput and queue lengths; add the real layout as a graph with path conflicts; model breakdowns and battery charging; add a deadlock-avoidance rule; then use the validated model to test fleet sizes and buffer layouts before buying equipment.

---

## 5. Things NOT implemented (don't claim them)

- No battery, charging, breakdown, collision avoidance or path planning.
- No deadlock prevention — only detection.
- No optimisation algorithm, auction or machine learning in dispatching.
- No sensitivity analysis on arrival rate or processing times (listed as an extension).
- No calibration or validation with real data.
