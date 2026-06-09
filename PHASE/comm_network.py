"""
Communication Network — Spatial Grid Model with coverage radius.
Comm nodes placed on a uniform grid over the bus geographic area.
Each comm covers buses within coverage_radius R.
Comm links connect nodes within link_radius (2*R).
"""
import networkx as nx, os, json, math
import numpy as np


class CommNetwork:
    """Spatial-grid communication network with radius-based coverage."""

    def __init__(self, bus_geojson_coords, n_x=4, n_y=5, spacing=2.0,
                 coverage_radius=2.0, seed=42):
        """
        bus_geojson_coords: list of (x, y) for 33 buses (from pandapower bus.geo)
        n_x, n_y, spacing: grid dimensions and spacing
        coverage_radius R: max distance for a bus to be covered by a comm node
        """
        self.n_x = n_x; self.n_y = n_y
        self.spacing = spacing
        self.coverage_radius = coverage_radius
        self.link_radius = 2.0 * coverage_radius
        self.bus_coords = bus_geojson_coords  # list of (x,y), index = bus_id

        # ---- Step 1: Place comm nodes on uniform grid ----
        # Center grid on bus centroid
        xs = [c[0] for c in bus_geojson_coords]
        ys = [c[1] for c in bus_geojson_coords]
        cx, cy = sum(xs)/len(xs), sum(ys)/len(ys)
        x0 = cx - (n_x - 1) * spacing / 2
        y0 = cy - (n_y - 1) * spacing / 2

        self.n_total = n_x * n_y
        self.n_power_comm = self.n_total  # all are potential RTU/comm nodes
        self.comm_coords = {}
        for i in range(n_y):
            for j in range(n_x):
                cid = i * n_x + j
                self.comm_coords[cid] = (x0 + j * spacing, y0 + i * spacing)

        # ---- Step 2: Compute bus → comm coverage (within radius) ----
        self.bus_to_comm = {}   # bus_id -> list of (comm_id, distance)
        for bus_id, (bx, by) in enumerate(bus_geojson_coords):
            nearby = []
            for cid, (cx, cy) in self.comm_coords.items():
                d = math.sqrt((bx - cx)**2 + (by - cy)**2)
                if d <= coverage_radius:
                    nearby.append((cid, d))
            nearby.sort(key=lambda x: x[1])
            self.bus_to_comm[bus_id] = nearby
            if not nearby:
                # fallback: closest comm even if beyond radius
                all_dists = [(cid, math.sqrt((bx-cx)**2+(by-cy)**2))
                             for cid, (cx,cy) in self.comm_coords.items()]
                all_dists.sort(key=lambda x: x[1])
                self.bus_to_comm[bus_id] = all_dists[:1]

        # ---- Step 3: Primary coverage mapping (P2C) ----
        self.p2c = {}   # bus_id -> primary comm_id
        self.c2p = {}   # comm_id -> set of bus_ids
        for cid in range(self.n_total):
            self.c2p[cid] = set()
        for bus_id, nearby in self.bus_to_comm.items():
            primary = nearby[0][0]  # nearest comm
            self.p2c[bus_id] = primary
            self.c2p[primary].add(bus_id)

        # Ensure every comm node covers at least 1 bus (no relays)
        for cid in range(self.n_total):
            if not self.c2p[cid]:
                # Find closest bus to this comm
                cx, cy = self.comm_coords[cid]
                best_bus, best_dist = None, float('inf')
                for bus_id, (bx, by) in enumerate(bus_geojson_coords):
                    d = math.sqrt((cx - bx)**2 + (cy - by)**2)
                    if d < best_dist:
                        best_bus, best_dist = bus_id, d
                if best_bus is not None:
                    self.p2c[best_bus] = cid
                    self.c2p[cid].add(best_bus)

        # ---- Step 4: Comm link topology (within link_radius) ----
        G = nx.Graph()
        for i in range(self.n_total):
            G.add_node(i)
        for i in range(self.n_total):
            xi, yi = self.comm_coords[i]
            for j in range(i + 1, self.n_total):
                xj, yj = self.comm_coords[j]
                d = math.sqrt((xi - xj)**2 + (yi - yj)**2)
                if d <= self.link_radius:
                    G.add_edge(i, j)
        self.graph = G
        self.edges = list(G.edges())

        # ---- Stats ----
        uncovered = sum(1 for b in range(len(bus_geojson_coords))
                        if len(self.bus_to_comm[b]) == 0
                        or self.bus_to_comm[b][0][1] > coverage_radius)
        cover_counts = [len(v) for v in self.c2p.values()]
        print(f"[COMM] Spatial grid: {self.n_total} nodes ({n_x}x{n_y}), "
              f"{len(self.edges)} edges, R={coverage_radius}")
        print(f"[COMM] Coverage: {len(bus_geojson_coords)-uncovered}/{len(bus_geojson_coords)} buses covered, "
              f"max/avg buses per comm: {max(cover_counts)}/{sum(cover_counts)/len(cover_counts):.1f}")

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
