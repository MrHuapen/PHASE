#!/usr/bin/env python3
"""
CPPS Interactive Communication Network Builder.
Phase 1: Left-click to place RTU, right-click to finish.
Phase 2: Input edge count, preview, refresh/clear edges, confirm save.
"""
import json, os, sys, math
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.widgets import Button, TextBox
import networkx as nx
import numpy as np

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cpps_comm_config.json")
COVERAGE_RADIUS = 2.0


def load_power_bg():
    from power_system import PowerSystem
    ps = PowerSystem()
    bus_geo = ps.get_bus_geo()
    net = ps.get_net_for_plotting()
    return ps, bus_geo, net


def draw_power_bg(ax, net, bus_geo):
    import pandapower.plotting as pplot
    bc = pplot.create_bus_collection(net, size=0.07, color="#2ecc71", zorder=2)
    lc = pplot.create_line_collection(net, color="#95a5a6", linewidths=1.2, zorder=1)
    pplot.draw_collections([lc, bc], ax=ax)
    for b, (bx, by) in bus_geo.items():
        dy = 0.14 if by >= 0 else -0.14
        ax.text(bx, by + dy, str(b), fontsize=5, ha="center", va="center",
                color="#2c3e50", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="#ccc", alpha=0.85))
    b0x, b0y = bus_geo[0]
    ax.scatter(b0x, b0y, marker="s", s=200, c="#2980b9", edgecolors="#1a5276",
               linewidths=2, zorder=15)
    ax.annotate("CTRL", (b0x, b0y), textcoords="offset points", xytext=(10, 10),
                fontsize=9, fontweight="bold", color="#2980b9")


# ============================================================
#  Phase 1: Node placement
# ============================================================

def phase1_placement():
    ps, bus_geo, net = load_power_bg()
    fig, ax = plt.subplots(figsize=(14, 12))
    fig.canvas.manager.set_window_title("Phase 1 — Left=place RTU, Right=finish")
    draw_power_bg(ax, net, bus_geo)
    ax.axis("off"); plt.tight_layout()

    placed = []              # list of (x, y)
    circle = [None]
    markers = []

    def on_move(event):
        if event.inaxes != ax:
            return
        if circle[0]:
            circle[0].remove()
        circle[0] = plt.Circle((event.xdata, event.ydata), COVERAGE_RADIUS,
                                fill=True, fc="#3498db", ec="#2980b9",
                                alpha=0.15, lw=1.5, zorder=10)
        ax.add_patch(circle[0])
        fig.canvas.draw_idle()

    def on_click(event):
        if event.inaxes != ax:
            return
        if event.button == 3:   # right-click → finish
            plt.close(fig)
            return
        if event.button != 1:   # left-click only
            return
        if event.xdata is None or event.ydata is None:
            return
        x, y = event.xdata, event.ydata
        cid = len(placed) + 1
        placed.append((x, y))
        mk = ax.scatter(x, y, marker="D", s=100, c="#e74c3c",
                        edgecolors="#922b21", linewidths=1.5, zorder=20)
        markers.append(mk)
        ax.annotate(str(cid), (x, y), textcoords="offset points",
                    xytext=(0, 10), fontsize=7, fontweight="bold",
                    color="#e74c3c", ha="center", zorder=21)
        # Print coverage
        cov = []
        for b, (bx, by) in bus_geo.items():
            if math.sqrt((x - bx)**2 + (y - by)**2) <= COVERAGE_RADIUS:
                cov.append(b)
        print(f"  RTU {cid:2d} at ({x:.3f},{y:.3f})  covers buses: {sorted(cov)}")
        ax.set_title(f"Placed {len(placed)} RTUs. Left=more, Right=finish.",
                     fontsize=12, fontweight="bold")
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_move)
    fig.canvas.mpl_connect("button_press_event", on_click)
    plt.show()
    plt.close("all")
    return bus_geo, placed


# ============================================================
#  Phase 2: Edge config + preview + save
# ============================================================

def phase2_edges(bus_geo, placed):
    n_comm = len(placed) + 1  # +1 for control center at bus 0
    comm_coords = {0: bus_geo[0]}
    for i, (x, y) in enumerate(placed):
        comm_coords[i + 1] = (x, y)

    # ---- Build P2C ----
    p2c = {}
    for b, (bx, by) in bus_geo.items():
        best_c, best_d = None, float("inf")
        for c, (cx, cy) in comm_coords.items():
            d = math.sqrt((bx - cx)**2 + (by - cy)**2)
            if d < best_d:
                best_c, best_d = c, d
        p2c[b] = best_c
    c2p = {c: set() for c in comm_coords}
    for b, c in p2c.items():
        c2p[c].add(b)

    edges = []      # current edge list
    max_edges = n_comm * (n_comm - 1) // 2
    default_edges = min(int(n_comm * 2), max_edges)  # ~2*N edges as default

    def k_nearest_edges(k):
        """Connect each node to its k nearest neighbors (k <= n_comm-1)."""
        elist = set()
        # Target edges = n_comm * k // 2, so k_edge ≈ 2 * target_edges / n_comm
        for i in range(n_comm):
            xi, yi = comm_coords[i]
            dists = []
            for j in range(n_comm):
                if j == i:
                    continue
                xj, yj = comm_coords[j]
                dists.append((math.sqrt((xi - xj)**2 + (yi - yj)**2), j))
            dists.sort()
            for _, j in dists[:k]:
                a, b = min(i, j), max(i, j)
                elist.add((a, b))
        return list(elist)

    edges.extend(k_nearest_edges(3))  # initial edges

    # ---- GUI ----
    fig, ax = plt.subplots(figsize=(14, 12))
    fig.canvas.manager.set_window_title(f"Phase 2 — {n_comm} nodes | Edit edges")
    plt.subplots_adjust(bottom=0.20)

    def redraw():
        ax.clear()
        # Power bg
        _, _bus_geo, net = load_power_bg()
        draw_power_bg(ax, net, _bus_geo)

        # Comm nodes
        for c, (cx, cy) in comm_coords.items():
            color = "#2980b9" if c == 0 else "#e74c3c"
            marker = "s" if c == 0 else "D"
            sz = 200 if c == 0 else 100
            ax.scatter(cx, cy, marker=marker, s=sz, c=color,
                       edgecolors="#333", linewidths=1.5, zorder=20)
            ax.annotate(str(c), (cx, cy), textcoords="offset points",
                        xytext=(0, 10), fontsize=7, fontweight="bold",
                        color=color, ha="center", zorder=21)

        # Edges
        if edges:
            for a, b in edges:
                x1, y1 = comm_coords[a]; x2, y2 = comm_coords[b]
                ax.plot([x1, x2], [y1, y2], color="#1abc9c", lw=1.2, alpha=0.6, zorder=5)

        ax.set_title(f"Phase 2 — {n_comm} nodes, {len(edges)} edges", fontsize=12, fontweight="bold")
        ax.axis("off")
        fig.canvas.draw_idle()

    # ---- Refresh button ----
    ax_refresh = plt.axes([0.15, 0.05, 0.15, 0.06])
    btn_refresh = Button(ax_refresh, "Refresh Edges")

    # ---- Clear button ----
    ax_clear = plt.axes([0.35, 0.05, 0.15, 0.06])
    btn_clear = Button(ax_clear, "Clear All")

    # ---- Edge count text box ----
    ax_tbox = plt.axes([0.55, 0.05, 0.10, 0.06])
    tbox = TextBox(ax_tbox, "Edges:", initial=str(len(edges)))

    # ---- Save button ----
    ax_save = plt.axes([0.75, 0.05, 0.15, 0.06])
    btn_save = Button(ax_save, "Save & Exit")

    def on_refresh(event):
        nonlocal edges
        try:
            target = int(tbox.text)
        except ValueError:
            target = len(edges)
        # k_nearest to achieve ~target edges: edges = n*k/2 → k = 2*target/n
        k = max(1, min(n_comm - 1, int(2 * target / n_comm + 0.5)))
        edges = k_nearest_edges(k)
        # If not exact, add/remove some random edges
        while len(edges) < target:
            import random
            a = random.randint(0, n_comm - 1)
            b = random.randint(0, n_comm - 1)
            if a == b: continue
            e = (min(a, b), max(a, b))
            if e not in edges:
                edges.append(e)
        while len(edges) > target and target >= 0:
            edges.pop()
        tbox.set_val(str(len(edges)))
        redraw()

    def on_clear(event):
        nonlocal edges
        edges = []
        tbox.set_val("0")
        redraw()

    def on_save(event):
        # Build config
        c2p_ser = {str(k): sorted(list(v)) for k, v in c2p.items()}
        config = {
            "n_comm": n_comm,
            "comm_coords": {str(k): list(v) for k, v in comm_coords.items()},
            "p2c": {str(k): v for k, v in p2c.items()},
            "c2p": c2p_ser,
            "edges": [[int(a), int(b)] for a, b in edges],
            "coverage_radius": COVERAGE_RADIUS,
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        print(f"\n  Saved: {CONFIG_FILE}")
        print(f"  Nodes: {n_comm}, Edges: {len(edges)}")
        plt.close(fig)

    def on_text_submit(text):
        on_refresh(None)

    btn_refresh.on_clicked(on_refresh)
    btn_clear.on_clicked(on_clear)
    btn_save.on_clicked(on_save)
    tbox.on_submit(on_text_submit)

    redraw()
    plt.show()
    plt.close("all")


# ============================================================
#  Main
# ============================================================

def run_builder():
    print("=" * 60)
    print("  CPPS Comm Network Builder (2-phase)")
    print(f"  Phase 1: Place RTUs → Phase 2: Configure edges → Save")
    print(f"  Coverage radius: {COVERAGE_RADIUS}")
    print("=" * 60)

    bus_geo, placed = phase1_placement()
    if len(placed) == 0:
        print("  No RTUs placed. Exiting.")
        return

    print(f"\n  Placed {len(placed)} RTUs + 1 control center = "
          f"{len(placed)+1} nodes total")
    phase2_edges(bus_geo, placed)


if __name__ == "__main__":
    run_builder()
