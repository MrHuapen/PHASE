#!/usr/bin/env python3
"""
CPPS Step-by-Step Cascading Simulator (Batch 3)
RTU backup battery + state monitoring + reachability-gated control + extended sim time.
"""
import os, sys, subprocess, csv, time as _time, random, math

WSL_PROJ = "\\\\wsl.localhost\\Ubuntu\\home\\dr_huapen\\ns3test\\cpps-sim"
WSL_NS3 = "/home/dr_huapen/ns3test/ns-allinone-3.47/ns-3.47"
WSL_PROJ_LIN = "/home/dr_huapen/ns3test/cpps-sim"

from power_system import PowerSystem
from coupling_model import CouplingModel
from comm_network import CommNetwork


class _SavedCommNetwork:
    """Thin wrapper for a pre-saved comm network config (from interactive builder)."""
    def __init__(self, n_total, edges, comm_coords):
        self.n_total = n_total
        self.edges = edges
        self.comm_coords = comm_coords
        import networkx as nx
        G = nx.Graph()
        for i in range(n_total):
            G.add_node(i)
        for a, b in edges:
            G.add_edge(a, b)
        self.graph = G

    def get_comm_coords(self, bus_geo=None):
        return dict(self.comm_coords)

    def write_ns3_config(self, failed_nodes, round_num, config_path, sim_mode=0):
        active = set(range(self.n_total)) - set(failed_nodes)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(f"# Config — Round {round_num}, sim_mode={sim_mode}\n")
            f.write(f"n_nodes {self.n_total}\n")
            f.write(f"n_edges {len(self.edges)}\n")
            f.write(f"sim_time 2.0\n")
            f.write(f"sim_mode {sim_mode}\n")
            for a, b in self.edges:
                f.write(f"edge {a} {b}\n")
            f.write("active_nodes " + " ".join(str(n) for n in sorted(active)) + "\n")
            f.write("failed_nodes " + " ".join(str(n) for n in sorted(set(failed_nodes))) + "\n")

    def parse_ns3_output(self, csv_path):
        import csv
        delays = {}; unreachable = set()
        if not os.path.exists(csv_path):
            return delays, unreachable
        with open(csv_path, "r") as f:
            for row in csv.DictReader(f):
                fid = int(row.get("flow_id", 0))
                dly = float(row.get("mean_delay_ms", -1))
                reach = int(row.get("reachable", 0))
                delays[fid] = dly
                if reach == 0:
                    unreachable.add(fid)
        return delays, unreachable


class CPPS_Simulator:
    def __init__(self, sim_duration=15, sim_mode="python",
                 battery_enabled=True, battery_cap=5,
                 over_trip_prob=0.10, cmd_timeout=100.0, report_timeout=150.0,
                 report_period=0.5, protection_threshold=3,
                 destroyed_comm="4,5,8,9,12", failed_lines="5,15,25",
                 seed=None, clean_snapshots=True):
        self.T = sim_duration
        self.clean_snapshots = clean_snapshots
        self.sim_mode = sim_mode
        self.over_trip_prob = over_trip_prob
        self.cmd_timeout = cmd_timeout
        self.report_timeout = report_timeout
        self.report_period = report_period
        self.protection_threshold = protection_threshold
        # Parse destroyed comm nodes and failed lines
        self.destroyed_comm = set(int(x) for x in destroyed_comm.split(",") if x.strip())
        self.failed_lines_init = [int(x) for x in failed_lines.split(",") if x.strip()]
        if seed is not None:
            random.seed(seed)
        self.ps = PowerSystem()
        # Try loading saved interactive config first
        import json
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "cpps_comm_config.json")
        if os.path.exists(config_path):
            print("[INIT] Loading saved comm network config...")
            with open(config_path) as f:
                cfg = json.load(f)
            n_comm = cfg["n_comm"]
            p2c = {int(k): v for k, v in cfg["p2c"].items()}
            edges = [(a, b) for a, b in cfg["edges"]]
            comm_coords = {int(k): tuple(v) for k, v in cfg["comm_coords"].items()}
            self.comm = _SavedCommNetwork(n_comm, edges, comm_coords)
            self.coupling = CouplingModel(num_buses=33, num_comm=n_comm,
                                          p2c_map=p2c, battery_enabled=battery_enabled,
                                          battery_cap=battery_cap)
        else:
            # Fall back to spatial grid model
            bus_coords = [self.ps.get_bus_geo()[i] for i in range(33)]
            self.comm = CommNetwork(bus_coords, n_x=4, n_y=5, spacing=2.0, coverage_radius=2.0)
            self.coupling = CouplingModel(num_buses=33, num_comm=self.comm.n_total,
                                          p2c_map=self.comm.p2c, battery_enabled=battery_enabled,
                                          battery_cap=battery_cap)

        self.failed_power_lines = set()
        self.failed_power_buses = set()
        self.history = []

        # Command queue + state table
        self.command_queue = []
        self.state_table = {}
        # report_period, cmd_timeout, report_timeout already set from params

        # Flag: per-step ns-3 delay from last comm_status.csv
        self.avg_delay_now = 0.0
        self.max_delay_now = 0.0
        self.flows_count = 0
        self.flows_total_tp = 0.0  # total throughput (Mbps) across all flows

    # ============ ns-3 bridge ============

    @property
    def _is_wsl(self):
        import platform
        return "microsoft" in platform.uname().release.lower()

    @property
    def _proj_dir(self):
        return WSL_PROJ_LIN if self._is_wsl else WSL_PROJ

    def _write_ns3_config(self):
        dead = self.coupling.perma_failed | {c for c in range(self.coupling.M)
                   if not self.coupling.is_comm_alive(c)}
        cfg = os.path.join(self._proj_dir, "cpps_comm_config.txt")
        sim_flag = 1 if self.sim_mode == "ns3" else 0
        self.comm.write_ns3_config(dead, 0, cfg, sim_mode=sim_flag)

    def _run_ns3_step(self, t):
        self._write_ns3_config()
        cmd = (f'cp "{WSL_PROJ_LIN}/cpps_comm_config.txt" {WSL_NS3}/ && '
               f'cd {WSL_NS3} && '
               f'./ns3 run cpps_comm_sim --no-build && '
               f'cp comm_status.csv flowmon-output.xml "{WSL_PROJ_LIN}/"')
        if self._is_wsl:
            r = subprocess.run(["bash", "-c", cmd],
                               capture_output=True, text=True, timeout=60)
        else:
            r = subprocess.run(["wsl.exe", "--user", "dr_huapen", "--exec",
                                "bash", "-l", "-c", cmd],
                               capture_output=True, text=True, timeout=60)
        ok = (r.returncode == 0)
        if not ok:
            print(f"  [COMM] ns-3 FAILED: rc={r.returncode}")
            if r.stderr:
                print(f"  [COMM] stderr: {r.stderr[:400]}")
            if r.stdout:
                print(f"  [COMM] stdout: {r.stdout[:400]}")
            return False
        csv_p = os.path.join(self._proj_dir, "comm_status.csv")
        if not os.path.exists(csv_p):
            print(f"  [COMM] WARNING: {csv_p} not found after ns-3 run")
            return False
        nlines = sum(1 for _ in open(csv_p))
        if nlines <= 1:
            print(f"  [COMM] WARNING: comm_status.csv has only {nlines} lines (no flow data)")
            return False
        delays, _ = self.comm.parse_ns3_output(csv_p)
        reachable = [v for v in delays.values() if v >= 0]
        self.flows_count = len(reachable)
        # Raw ns-3 throughput (fixed-rate RTU flows)
        tp_raw = 0.0
        with open(csv_p, "r") as f:
            for row in csv.DictReader(f):
                try:
                    tp_raw += float(row.get("throughput_mbps", 0))
                except (ValueError, KeyError):
                    pass
        self.flows_total_tp_raw = tp_raw  # will be scaled in run() loop
        self.avg_delay_now = (sum(reachable) / max(len(reachable), 1)
                              if reachable else 999.0)
        self.max_delay_now = max(reachable) if reachable else 999.0
        return ok

    # ============ Main loop ============

    def run(self):
        # Clean old snapshots
        if self.clean_snapshots:
            snap_dir = os.path.join(self._proj_dir, "output", "snapshots")
            if os.path.exists(snap_dir):
                import glob
                for f in glob.glob(os.path.join(snap_dir, "*.png")):
                    os.remove(f)
                print(f"[CLEAN] Removed old snapshots from {snap_dir}")

        print("=" * 70)
        print("  CPPS Batch 3 — RTU Battery + State Table + Reachability-Gated Control")
        print(f"  Power: 33 buses, 3 DGs | Comm: {self.comm.n_total} nodes spatial grid | OLSR")
        bat = "OFF" if not self.coupling.battery_enabled else f"{self.coupling.battery_cap}s"
        print(f"  Duration: {self.T}s | RTU battery: {bat}")
        print("=" * 70)
        print("\n  Comm Node → Power Bus Coverage (all RTU):")
        for c in range(self.coupling.M):
            buses = self.coupling.get_rtu_coverage(c)
            print(f"    Comm {c:2d} (RTU) : buses {sorted(buses)}")

        # ---------- T=0: Disaster ----------
        print(f"\n[T=0.00s] {'='*60}")
        print(f"[DISASTER] Typhoon strikes: lines {self.failed_lines_init} destroyed\n")
        for l in self.failed_lines_init:
            self.ps.open_breaker(l)
            self.failed_power_lines.add(l)
            a = self.ps.breakers[l]['from_bus']; b = self.ps.breakers[l]['to_bus']
            print(f"  [POWER] Breaker L{l} OPEN (bus {a}<->{b})")

        # ---- Fixed comm nodes destroyed by typhoon ----
        destroyed_comm = set(self.destroyed_comm)
        ctrl_comm = self.coupling.get_comm_node_for_bus(0)
        destroyed_comm.discard(ctrl_comm)
        for c in destroyed_comm:
            self.coupling.perma_failed.add(c)
            self.coupling.battery[c] = 0.0
            buses = self.coupling.get_rtu_coverage(c)
            label = f"covers buses {sorted(buses)}" if buses else "relay node"
            print(f"  [NODE-FAIL] Comm {c} DIRECTLY DESTROYED by typhoon ({label})")
        print(f"  [DISASTER] {len(destroyed_comm)} comm nodes destroyed + "
              f"{len(self.failed_power_lines)} power lines broken")

        state = self.ps.step_power_flow()
        islands = self.ps.get_islands()
        self._init_state_table(state)
        self._print_state(0, state, islands)
        # Track state changes for snapshots
        prev_energized = len(state["energized"])
        prev_islands = len(islands)
        prev_alive_comm = sum(1 for c in range(self.coupling.M)
                              if self.coupling.is_comm_alive(c))

        # Save T=0 (initial disaster) snapshot
        snap_dir = os.path.join(self._proj_dir, "output", "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        from visualization import plot_topology_static as _snap_plot
        dead = {c for c in range(self.coupling.M) if not self.coupling.is_comm_alive(c)}
        _snap_plot(self.ps, self.comm, self.coupling,
                   set(self.failed_power_buses),
                   set(self.failed_power_lines),
                   dead, os.path.join(snap_dir, "T00_initial.png"))
        print(f"  [SNAP] Initial state saved: snapshots/T00_initial.png\n")

        self.history.append({"t": 0, "islands": len(islands), "dg_output": 0.0,
                             "avg_delay": 0.0, "flows_count": 0, "flows_total_tp": 0.0, **state})

        # ---------- T=1..T ----------
        prev_lost_load = state["lost_load_mw"]
        for t in range(1, self.T + 1):
            print(f"\n[T={t:.2f}s] {'='*60}")

            # ---- Phase 1: Battery drain (gradual multi-step) ----
            unpowered = set(state["unpowered"])
            newly_depleted = self.coupling.drain_batteries(unpowered, dt=1.0)
            if newly_depleted:
                print(f"\n  [BATT] {len(newly_depleted)} RTUs lost backup power: {sorted(newly_depleted)}")
                for c in sorted(newly_depleted):
                    buses = self.coupling.get_rtu_coverage(c)
                    print(f"    Comm {c} (buses {buses}) -> DEAD (battery exhausted)")
                    for b in buses:
                        self.failed_power_buses.add(b)
            else:
                draining = [c for c in range(self.coupling.M)
                            if not self.coupling.is_comm_alive(c) and c not in self.coupling.perma_failed
                            and self.coupling.get_rtu_coverage(c) and self.coupling.get_rtu_coverage(c).issubset(unpowered)]
                if draining:
                    remaining = {c: f"{self.coupling.battery.get(c,0):.0f}s" for c in draining}
                    print(f"    Batteries draining: {remaining}")

            # ---- Phase 2: ns-3 OLSR ----
            alive_comm = sum(1 for c in range(self.coupling.M)
                             if self.coupling.is_comm_alive(c))
            dead_comm = self.coupling.perma_failed
            print(f"\n  [COMM] ns-3 OLSR (t={t}s), alive={alive_comm}/{self.coupling.M}, "
                  f"perma_dead={len(dead_comm)}")
            ok = self._run_ns3_step(t)
            if not ok:
                print("  [COMM] ERROR")
                continue
            # Scale throughput by energized-bus ratio (reflects actual reporting buses)
            n_en = len(state["energized"])
            self.flows_total_tp = self.flows_total_tp_raw * (n_en / 33.0)
            print(f"    Flows: {self.flows_count}, avg_delay={self.avg_delay_now:.1f}ms, "
                  f"max_delay={self.max_delay_now:.1f}ms")

            # ---- Phase 3: Status reports ----
            fresh = 0; stale = 0
            for bus in state["energized"]:
                # Both modes: use avg_delay approx (ns3 mode has extra traffic affecting avg_delay)
                dly = random.uniform(self.avg_delay_now * 0.5,
                                     self.avg_delay_now * 1.5) if self.flows_count else 999
                if dly < self.report_timeout:
                    self.state_table[bus] = {
                        "status": "energized",
                        "voltage": float(state["voltages"][bus]) if bus < len(state["voltages"]) else 0,
                        "load_mw": float(self.ps.net.load[self.ps.net.load["bus"] == bus]["p_mw"].sum()),
                        "last_update": t,
                    }
                    fresh += 1
                else:
                    stale += 1
            print(f"\n  [REPORT] Status reports: fresh={fresh}, stale={stale}")

            # ---- Phase 3.5: Delay-triggered protection ----
            # If a bus hasn't reported for consecutive cycles, trip it
            for bus in state["energized"]:
                last_up = self.state_table.get(bus, {}).get("last_update", 0)
                if t - last_up >= self.protection_threshold:
                    # Protection: shed load at this bus — comm lost → conservative default
                    ld_mask = self.ps.net.load["bus"] == bus
                    if ld_mask.any():
                        mw = float(self.ps.net.load[ld_mask]["p_mw"].sum())
                        if mw > 0.001:
                            self.ps.shed_load(bus, mw)
                            print(f"  [PROT] Bus {bus}: {mw:.4f}MW shed (no report for {t-last_up}s)")

            # ---- Phase 4: Execute pending commands (only on reachable buses) ----
            print(f"\n  [CMD] Queue: {len(self.command_queue)} pending")
            executed = []
            for send_t, exec_t, cmd in self.command_queue:
                bus = cmd.get("bus", -1)
                cnode = self.coupling.get_comm_node_for_bus(bus)
                # Check reachability: comm node must be alive (battery > 0 or powered)
                reachable = (cnode is not None and self.coupling.is_comm_alive(cnode)
                             and cnode not in self.coupling.perma_failed)
                # Also check delay
                dly = random.uniform(self.avg_delay_now * 0.5,
                                     self.avg_delay_now * 1.5) if self.flows_count else 999
                if reachable and dly < self.cmd_timeout:
                    executed.append(cmd)
                else:
                    reason = "TIMEOUT" if dly >= self.cmd_timeout else "UNREACHABLE"
                    print(f"    CMD {cmd['type']} bus{bus} -> {reason} (delay={dly:.0f}ms)")
            self.command_queue = []
            for cmd in executed:
                self._apply_command(cmd)
            if not executed:
                print(f"    No commands executed")

            # ---- Phase 5: Islanding + DG dispatch (reachability-gated) ----
            state = self.ps.step_power_flow()
            islands = self.ps.get_islands()
            print(f"\n  [ISLAND] {len(islands)} islands:")
            new_cmds = []

            for i, isl in enumerate(islands):
                load = self.ps.get_island_load(isl)
                dg_cap = self.ps.get_island_dg_capacity(isl)
                if 0 in isl:
                    print(f"    Island {i}: {len(isl)} buses, load={load:.4f}MW, Substation (OK)")
                    continue

                if dg_cap < 0.001:
                    print(f"    Island {i}: {len(isl)} buses, NO POWER SOURCE -> BLACKOUT")
                    for b in isl:
                        self.failed_power_buses.add(b)
                    continue

                # Island with DG potential
                dgs = self.ps.get_island_dgs(isl)
                # Only reachable DGs can be controlled
                reachable_dgs = []
                unreachable_dgs = []
                for dg_bus, rated, active in dgs:
                    cnode = self.coupling.get_comm_node_for_bus(dg_bus)
                    if cnode is not None and self.coupling.is_comm_alive(cnode):
                        reachable_dgs.append((dg_bus, rated, active))
                    else:
                        unreachable_dgs.append((dg_bus, rated))

                avail_cap = sum(r[1] for r in reachable_dgs)
                deficit = load - avail_cap

                print(f"    Island {i}: {len(isl)} buses, load={load:.4f}MW, "
                      f"DG_avail={avail_cap:.3f}MW (reachable={len(reachable_dgs)}, "
                      f"unreachable={len(unreachable_dgs)}), deficit={deficit:.4f}MW")

                for dg_bus, rated, active in reachable_dgs:
                    if not active:
                        new_cmds.append({"type": "dg_enable", "bus": dg_bus, "mw": rated})
                        print(f"      DG bus {dg_bus} ENABLE cmd enqueued (rated={rated:.3f}MW)")

                if deficit > 0:
                    print(f"      DEFICIT {deficit:.4f}MW — shedding load (reachable buses only)")
                    total_il = load if load > 0 else 1e-6
                    for idx, ld in self.ps.net.load.iterrows():
                        b = int(ld["bus"])
                        if b not in isl or not ld["in_service"]:
                            continue
                        cnode = self.coupling.get_comm_node_for_bus(b)
                        if cnode is None or not self.coupling.is_comm_alive(cnode):
                            continue
                        frac = ld["p_mw"] / total_il
                        shed = min(ld["p_mw"], deficit * frac * 1.1)
                        if shed > 0.001:
                            new_cmds.append({"type": "shed", "bus": b, "mw": shed})
                            print(f"        Shed {shed:.4f}MW at bus {b}")
                else:
                    for dg_bus, rated, _ in reachable_dgs:
                        setpoint = min(rated, load * rated / max(avail_cap, 0.001))
                        new_cmds.append({"type": "dg_set", "bus": dg_bus, "mw": setpoint})
                        print(f"      DG bus {dg_bus} setpoint={setpoint:.3f}MW")

            for cmd in new_cmds:
                self.command_queue.append((t, t + 0.5, cmd))

            # ---- Phase 6: Protection misoperation due to comm loss ----
            # Check ALL lines (not just overloaded ones) for protection issues
            misoperated = 0
            for l_idx in range(len(self.ps.net.line)):
                if not self.ps.net.line.at[l_idx, "in_service"]:
                    continue
                if l_idx in self.failed_power_lines:
                    continue
                lp = self.ps.net.res_line.at[l_idx, "loading_percent"]
                fb = int(self.ps.net.line.at[l_idx, "from_bus"])
                cnode = self.coupling.get_comm_node_for_bus(fb)
                reachable = (cnode is not None and self.coupling.is_comm_alive(cnode))
                dly = random.uniform(self.avg_delay_now * 0.5,
                                     self.avg_delay_now * 1.5) if self.flows_count else 999

                if lp > 120.0:
                    # Overloaded: needs to trip
                    if reachable and dly < self.cmd_timeout:
                        self.command_queue.append((t, t + 0.5,
                            {"type": "open_breaker", "line": l_idx}))
                        print(f"    L{l_idx}: {lp:.1f}% > 120% -> CMD open (delay={dly:.0f}ms)")
                    else:
                        # FAIL-TO-TRIP (comm lost, no coordinated protection)
                        print(f"    L{l_idx}: {lp:.1f}% > 120% -> FAIL-TO-TRIP (unreachable, delay={dly:.0f}ms)")
                        # Auto-protection after timeout
                        self.ps.open_breaker(l_idx)
                        self.failed_power_lines.add(l_idx)
                        misoperated += 1
                elif lp < 30.0 and (not reachable or dly > self.cmd_timeout):
                    # Lightly loaded but comm lost -> OVER-TRIP (protection misoperates)
                    # Only apply with small probability to avoid destroying the network
                    if random.random() < self.over_trip_prob:
                        self.ps.open_breaker(l_idx)
                        self.failed_power_lines.add(l_idx)
                        print(f"    L{l_idx}: {lp:.1f}% -> OVER-TRIP (comm lost, protection misoperated!)")
                        misoperated += 1

            if misoperated > 0:
                print(f"    [PROT] {misoperated} lines affected by protection misoperation")
                new_failures_flag = True

            # ---- Phase 7: Convergence ----
            state = self.ps.step_power_flow()
            self._print_state(t, state, islands)
            self.history.append({"t": t, "islands": len(islands),
                                 "dg_output": sum(sg["p_mw"] for _, sg in self.ps.net.sgen.iterrows() if sg["in_service"]),
                                 "avg_delay": self.avg_delay_now, "flows_count": self.flows_count,
                                 "flows_total_tp": self.flows_total_tp, **state})

            # Snapshot: save topology when state changes
            curr_energized = len(state["energized"])
            curr_islands = len(islands)
            curr_alive = sum(1 for c in range(self.coupling.M)
                             if self.coupling.is_comm_alive(c))
            if (curr_energized != prev_energized or curr_islands != prev_islands
                    or curr_alive != prev_alive_comm):
                snap_dir = os.path.join(self._proj_dir, "output", "snapshots")
                os.makedirs(snap_dir, exist_ok=True)
                from visualization import plot_topology_static as _snap_plot
                dead = {c for c in range(self.coupling.M) if not self.coupling.is_comm_alive(c)}
                snap_path = os.path.join(snap_dir, f"T{t:02d}_b{curr_energized}_i{curr_islands}.png")
                _snap_plot(self.ps, self.comm, self.coupling,
                           set(self.failed_power_buses),
                           set(self.failed_power_lines),
                           dead, snap_path)
                print(f"    [SNAP] Saved: snapshots/T{t:02d}_b{curr_energized}_i{curr_islands}.png")
                prev_energized, prev_islands, prev_alive_comm = curr_energized, curr_islands, curr_alive

            # Multi-round cascade: stop only if no new failures AND load stable
            delta_load = abs(state["lost_load_mw"] - prev_lost_load)
            prev_lost_load = state["lost_load_mw"]
            if (not newly_depleted and not new_cmds and delta_load < 0.001
                    and misoperated == 0):
                print(f"\n  >>> Cascade converged at t={t}s (no new failures, load stable)")
                break
            if state["lost_load_mw"] >= state["total_load_mw"] * 0.99:
                print(f"\n  >>> SYSTEM COLLAPSED at t={t}s (100% load loss)")
                break

        self._print_summary()
        self._generate_visualizations()

    # ============ Helpers ============

    def _init_state_table(self, state):
        for b in state["energized"]:
            self.state_table[b] = {"status": "energized", "last_update": 0}
        for b in state["unpowered"]:
            self.state_table[b] = {"status": "unpowered", "last_update": 0}

    def _apply_command(self, cmd):
        t = cmd["type"]
        if t == "open_breaker":
            self.ps.open_breaker(cmd["line"])
            self.failed_power_lines.add(cmd["line"])
            print(f"    Applied: open breaker L{cmd['line']}")
        elif t == "dg_enable":
            self.ps.enable_dg(cmd["bus"])
        elif t == "dg_set":
            self.ps.dispatch_dg(cmd["bus"], cmd["mw"])
            print(f"    Applied: DG bus{cmd['bus']} -> {cmd['mw']:.3f}MW")
        elif t == "shed":
            self.ps.shed_load(cmd["bus"], cmd["mw"])
            print(f"    Applied: shed {cmd['mw']:.4f}MW at bus {cmd['bus']}")

    def _print_state(self, t, state, islands):
        bat_info = ""
        depleted = [c for c in range(self.coupling.M)
                    if c in self.coupling.perma_failed]
        if depleted:
            bat_info = f", battery_depleted={len(depleted)}"
        print(f"\n  [STATE t={t}s] {len(state['energized'])}/33 buses, "
              f"{len(islands)} islands, "
              f"served={state['served_load_mw']:.4f}MW "
              f"({state['served_load_mw']/max(state['total_load_mw'],0.001)*100:.1f}%), "
              f"lost={state['lost_load_mw']:.4f}MW{bat_info}")

    def _print_summary(self):
        last = self.history[-1]
        alive = sum(1 for c in range(self.coupling.M)
                    if self.coupling.is_comm_alive(c))
        dg_tot = sum(sg["p_mw"] for _, sg in self.ps.net.sgen.iterrows()
                     if sg["in_service"])
        print(f"\n{'='*70}")
        print(f"  CASCADE SIMULATION SUMMARY (Batch 3)")
        print(f"{'='*70}")
        print(f"  Total steps: {len(self.history)}")
        print(f"  Final energized buses: {len(last['energized'])}/33")
        print(f"  Final served load: {last['served_load_mw']:.4f}/"
              f"{last['total_load_mw']:.4f} MW")
        print(f"  Failed lines: {len(self.failed_power_lines)}")
        print(f"  Failed buses: {len(self.failed_power_buses)}")
        print(f"  Comm alive: {alive}/{self.coupling.M}")
        print(f"  Comm perma-dead: {len(self.coupling.perma_failed)}")
        print(f"  DG output: {dg_tot:.3f} MW")
        print(f"  Avg delay: {last.get('avg_delay', 0):.1f}ms")
        print(f"{'='*70}")

        # Export 5 key indicator curves to CSV
        csv_indicators = os.path.join(self._proj_dir, "output", "indicator_curves.csv")
        with open(csv_indicators, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["t", "energized_buses", "lost_load_mw", "dg_output_mw",
                             "islands", "flows_count", "flows_total_tp_mbps",
                             "avg_delay_ms", "comm_alive"])
            for h in self.history:
                n_en = h.get("num_energized", len(h.get("energized", [])))
                writer.writerow([
                    h.get("t", h.get("round", 0)),
                    n_en,
                    round(h.get("lost_load_mw", 0), 4),
                    round(h.get("dg_output", 0), 4),
                    h.get("islands", 1),
                    h.get("flows_count", self.flows_count),
                    round(h.get("flows_total_tp", self.flows_total_tp), 4),
                    round(h.get("avg_delay", self.avg_delay_now), 2),
                    h.get("comm_alive", sum(1 for c in range(self.coupling.M)
                        if self.coupling.is_comm_alive(c))),
                ])
        print(f"  [CSV] Indicator curves exported: output/indicator_curves.csv")

    def _generate_visualizations(self):
        print(f"\nGenerating visualizations...")
        output = os.path.join(WSL_PROJ, "output")
        os.makedirs(output, exist_ok=True)
        from visualization import (plot_topology_static, plot_fused_topology, plot_timeseries)
        last = self.history[-1]
        dead = self.coupling.perma_failed
        plot_topology_static(self.ps, self.comm, self.coupling,
                             set(last.get("disconnected_buses", self.failed_power_buses)),
                             set(last.get("disconnected_lines", self.failed_power_lines)),
                             dead,
                             os.path.join(output, "topology_static.png"))
        plot_fused_topology(self.ps, self.comm, self.coupling,
                            set(last.get("disconnected_buses", self.failed_power_buses)),
                            set(last.get("disconnected_lines", self.failed_power_lines)),
                            dead,
                            os.path.join(output, "topology_fused.png"))
        plot_timeseries(self.history, os.path.join(output, "timeseries.png"))
        print(f"  Output: {output}/")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="CPPS Cascading Failure Simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                                # default run
  %(prog)s --sim-mode ns3 --over-trip 0.20                # ns3 mode, higher trip prob
  %(prog)s --duration 30 --failed-lines 0,5,15            # 30s, different disaster
  %(prog)s --destroyed-comm 4,5,8,9,12,2                  # 6 comm nodes destroyed
  %(prog)s --battery-cap 8 --battery-off                  # 8s battery, then off
  %(prog)s --seed 123 --over-trip 0.15 --cmd-timeout 80   # reproducible, tuned
        """)
    p.add_argument("--duration", type=int, default=20,
                   help="Simulation duration in seconds")
    p.add_argument("--sim-mode", choices=["python","ns3"], default="ns3",
                   help="Comm simulation mode (default: ns3)")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for reproducible runs")
    # Battery
    p.add_argument("--battery-off", action="store_true",
                   help="Disable RTU backup battery (instant death)")
    p.add_argument("--battery-cap", type=int, default=5,
                   help="RTU battery capacity in seconds (default: 5)")
    # Protection
    p.add_argument("--over-trip", type=float, default=0.10,
                   help="OVER-TRIP probability per step per line (default: 0.10)")
    p.add_argument("--cmd-timeout", type=float, default=100.0,
                   help="Command timeout in ms (default: 100)")
    p.add_argument("--report-timeout", type=float, default=150.0,
                   help="Status report timeout in ms (default: 150)")
    p.add_argument("--report-period", type=float, default=0.5,
                   help="Report interval in seconds (default: 0.5)")
    p.add_argument("--prot-threshold", type=int, default=3,
                   help="Cycles before protection trip (default: 3)")
    # Disaster
    p.add_argument("--failed-lines", type=str, default="5,15,25",
                   help="Power lines to destroy (default: 5,15,25)")
    p.add_argument("--destroyed-comm", type=str, default="4,5,8,9,12",
                   help="Comm nodes to destroy (default: 4,5,8,9,12)")
    p.add_argument("--no-clean-snapshots", action="store_true",
                   help="Keep old snapshots (default: auto-clean)")
    args = p.parse_args()

    sim = CPPS_Simulator(
        sim_duration=args.duration,
        sim_mode=args.sim_mode,
        seed=args.seed,
        battery_enabled=not args.battery_off,
        battery_cap=args.battery_cap,
        over_trip_prob=args.over_trip,
        cmd_timeout=args.cmd_timeout,
        report_timeout=args.report_timeout,
        report_period=args.report_period,
        protection_threshold=args.prot_threshold,
        failed_lines=args.failed_lines,
        destroyed_comm=args.destroyed_comm,
        clean_snapshots=not args.no_clean_snapshots,
    )
    sim.run()
