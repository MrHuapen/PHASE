"""
CPPS Power System — IEEE 33-bus + DG + remote-controllable breakers.
"""
import pandapower as pp, pandapower.networks as nw
import numpy as np, json
from copy import deepcopy


class PowerSystem:
    def __init__(self):
        self.net = nw.case33bw()
        self.base_net = deepcopy(self.net)
        self._add_dgs()
        self._init_breakers()

    # ============ DG ============

    def _add_dgs(self):
        # 3 DGs — capacity intentionally below island load to force shedding
        dgs = [(14, 0.3), (22, 0.4), (30, 0.15)]  # (bus, MW)
        for bus, pmw in dgs:
            pp.create_sgen(self.net, bus, p_mw=pmw, q_mvar=0, sn_mva=0.6, name=f"DG_bus{bus}")
        # Initial state: all DGs OFF (disaster response will enable)
        self.net.sgen["in_service"] = False

    # ============ Breakers ============

    def _init_breakers(self):
        """Each line has a remote-controllable breaker; preserve original in_service."""
        self.breakers = {}
        for idx in self.net.line.index:
            orig_svc = bool(self.net.line.at[idx, "in_service"])
            self.breakers[idx] = {
                "status": "CLOSED" if orig_svc else "OPEN",
                "from_bus": int(self.net.line.at[idx, "from_bus"]),
                "to_bus": int(self.net.line.at[idx, "to_bus"]),
            }
            # Preserve original in_service — do NOT force True on tie switches

    def get_breaker(self, line_idx):
        return self.breakers.get(line_idx, {}).get("status", "CLOSED")

    def open_breaker(self, line_idx):
        if line_idx in self.breakers:
            self.breakers[line_idx]["status"] = "OPEN"
            self.net.line.at[line_idx, "in_service"] = False

    def close_breaker(self, line_idx):
        if line_idx in self.breakers:
            self.breakers[line_idx]["status"] = "CLOSED"
            self.net.line.at[line_idx, "in_service"] = True

    # ============ DG control ============

    def set_dg_output(self, bus_id, p_mw):
        """Set DG active power output at a given bus."""
        mask = (self.net.sgen["bus"] == bus_id) & (self.net.sgen["in_service"])
        if mask.any():
            idx = self.net.sgen[mask].index[0]
            old = self.net.sgen.at[idx, "p_mw"]
            self.net.sgen.at[idx, "p_mw"] = p_mw
            return old, p_mw
        return None, None

    def enable_dg(self, bus_id):
        mask = self.net.sgen["bus"] == bus_id
        if mask.any():
            self.net.sgen.loc[mask, "in_service"] = True
            # Create ext_grid at the DG bus as island slack reference
            # Only if one doesn't already exist at this bus
            if not any(self.net.ext_grid["bus"] == bus_id):
                pp.create_ext_grid(self.net, bus_id, vm_pu=1.0, va_degree=0,
                                   s_sc_max_mva=10, name=f"DG_slack_bus{bus_id}")
            return True
        return False

    # ============ Power flow ============

    def step_power_flow(self):
        """Run power flow + detect overload + identify islands."""
        try:
            pp.runpp(self.net, numba=False, calculate_voltage_angles=True,
                     init="dc", max_iteration=30)
            converged = True
        except Exception:
            converged = False

        # Identify energized/unpowered buses
        energized, unpowered = [], []
        for b in self.net.bus.index:
            try:
                vm = float(self.net.res_bus.at[b, "vm_pu"])
                if np.isnan(vm) or vm <= 0.005:
                    unpowered.append(int(b))
                else:
                    energized.append(int(b))
            except Exception:
                unpowered.append(int(b))

        # Identify overloaded lines
        overloaded = []
        for l in self.net.line.index:
            if self.net.line.at[l, "in_service"]:
                lp = self.net.res_line.at[l, "loading_percent"]
                if lp > 120.0:
                    overloaded.append((l, lp))

        total = self.net.load["p_mw"].sum()
        served = sum(self.net.load.at[i, "p_mw"]
                     for i in self.net.load.index
                     if self.net.load.at[i, "bus"] in energized)

        return {
            "converged": converged,
            "energized": energized,
            "unpowered": unpowered,
            "overloaded": overloaded,
            "total_load_mw": total,
            "served_load_mw": served,
            "lost_load_mw": total - served,
            "voltages": self.net.res_bus["vm_pu"].values.copy(),
            "line_loadings": self.net.res_line["loading_percent"].values.copy(),
        }

    # ============ Topology ============

    def get_line_topology(self):
        return [(int(r["from_bus"]), int(r["to_bus"]))
                for _, r in self.net.line.iterrows()]

    def get_bus_geo(self):
        coords = {}
        for idx, row in self.net.bus.iterrows():
            try:
                g = row.get("geo")
                if isinstance(g, str) and g != "nan":
                    c = json.loads(g)["coordinates"]
                    coords[int(idx)] = (float(c[0]), float(c[1]))
                    continue
            except Exception:
                pass
            coords[int(idx)] = (float(idx) * 0.1, 0.0)
        return coords

    def get_net_for_plotting(self):
        return self.net

    # ============ Island detection ============

    def get_islands(self):
        """Return list of sets: each set = bus IDs in one connected component (island).
        Only considers buses connected via in_service=True lines."""
        adj = {b: set() for b in self.net.bus.index}
        for idx, row in self.net.line.iterrows():
            if row["in_service"]:
                a, b = int(row["from_bus"]), int(row["to_bus"])
                adj[a].add(b); adj[b].add(a)
        visited = set(); islands = []
        for start in adj:
            if start in visited: continue
            q = [start]; comp = set()
            while q:
                u = q.pop()
                if u in visited: continue
                visited.add(u); comp.add(u)
                for v in adj[u]:
                    if v not in visited: q.append(v)
            islands.append(comp)
        return islands

    def get_island_load(self, island):
        """Total active load (MW) in an island."""
        total = 0.0
        for idx, ld in self.net.load.iterrows():
            if int(ld["bus"]) in island and ld["in_service"]:
                total += ld["p_mw"]
        return total

    def get_island_dg_capacity(self, island, active_only=False):
        """Total DG capacity (MW) in an island.
        active_only=False: count ALL installed DGs (for islanding decision).
        active_only=True:  count only DGs already in_service (for dispatch check)."""
        total = 0.0
        for idx, sg in self.net.sgen.iterrows():
            if int(sg["bus"]) in island:
                if not active_only or sg["in_service"]:
                    total += sg["p_mw"]
        return total

    def get_island_dgs(self, island):
        """Return list of (bus_id, p_mw, is_active) for DGs in island."""
        dgs = []
        for idx, sg in self.net.sgen.iterrows():
            bus = int(sg["bus"])
            if bus in island:
                dgs.append((bus, sg["p_mw"], sg["in_service"]))
        return dgs

    def dispatch_dg(self, dg_bus, p_mw_setpoint):
        """Set DG output to a specific value (clamped to [0, rated])."""
        mask = (self.net.sgen["bus"] == dg_bus)
        if mask.any():
            idx = self.net.sgen[mask].index[0]
            rated = self.net.sgen.at[idx, "p_mw"]
            actual = max(0.0, min(p_mw_setpoint, rated))
            old = self.net.sgen.at[idx, "p_mw"]
            self.net.sgen.at[idx, "p_mw"] = actual
            return old, actual
        return None, None

    def shed_load(self, bus_id, p_mw):
        """Reduce load at a bus by p_mw (never below 0)."""
        mask = self.net.load["bus"] == bus_id
        if mask.any():
            idx = self.net.load[mask].index[0]
            old = self.net.load.at[idx, "p_mw"]
            new = max(0.0, old - p_mw)
            self.net.load.at[idx, "p_mw"] = new
            return old, new
        return None, None
