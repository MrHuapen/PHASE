"""
CPPS Coupling Model — accepts P2C mapping from spatial CommNetwork.
"""
import numpy as np


class CouplingModel:
    def __init__(self, num_buses=33, num_comm=20, p2c_map=None,
                 battery_enabled=False, battery_cap=5):
        self.N = num_buses
        self.M = num_comm
        self.battery_enabled = battery_enabled
        self.battery_cap = battery_cap

        # P2C: bus_id -> comm_id (from CommNetwork spatial model)
        self.p2c = p2c_map if p2c_map else {i: i for i in range(min(num_buses, num_comm))}
        self.c2p = {c: set() for c in range(self.M)}
        for bus, comm in self.p2c.items():
            self.c2p[comm].add(bus)

        self.dep_p2c = np.zeros((self.N, self.M), dtype=bool)
        for bus, comm in self.p2c.items():
            self.dep_p2c[bus, comm] = True

        self.dep_c2p = np.zeros((self.N, self.M), dtype=bool)
        for comm, buses in self.c2p.items():
            for bus in buses:
                self.dep_c2p[bus, comm] = True

        # Battery state
        self.battery = {c: battery_cap for c in range(self.M)}
        self.perma_failed = set()

    def drain_batteries(self, unpowered_buses, dt=1.0):
        newly_depleted = set()
        for comm in range(self.M):
            if comm in self.perma_failed:
                continue
            covered = self.c2p[comm]
            if not covered:
                continue
            if covered.issubset(unpowered_buses):
                if not self.battery_enabled:
                    self.perma_failed.add(comm)
                    self.battery[comm] = 0.0
                    newly_depleted.add(comm)
                else:
                    self.battery[comm] = max(0.0, self.battery[comm] - dt)
                    if self.battery[comm] <= 0:
                        self.perma_failed.add(comm)
                        newly_depleted.add(comm)
        return newly_depleted

    def is_comm_alive(self, comm):
        return comm not in self.perma_failed and self.battery.get(comm, 0) > 0

    def comm_nodes_affected_by_power_failure(self, failed_buses):
        affected = set()
        for comm, covered in self.c2p.items():
            if covered and covered.issubset(failed_buses):
                affected.add(comm)
        if 0 in failed_buses:
            affected.update(range(self.M))
        return affected

    def power_buses_affected_by_comm_failure(self, failed_comm):
        affected = set()
        for comm in failed_comm:
            for i in range(self.N):
                if self.dep_c2p[i, comm]:
                    affected.add(int(i))
        return affected

    def get_rtu_coverage(self, rtu_id):
        return sorted(self.c2p.get(rtu_id, set()))

    def get_comm_node_for_bus(self, bus_id):
        return self.p2c.get(bus_id, None)

    def get_powering_bus(self, comm_node):
        for bus in range(self.N):
            if self.dep_p2c[bus, comm_node]:
                return bus
        return None

    def __repr__(self):
        alive = sum(1 for c in range(self.M) if self.is_comm_alive(c))
        bat = "ON" if self.battery_enabled else "OFF"
        return (f"CouplingModel({self.N} buses → {self.M} comm, "
                f"alive={alive}/{self.M}, battery={bat})")
