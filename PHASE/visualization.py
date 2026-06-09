"""
CPPS Visualization — pandapower (coords from bus.geo) + networkx comm layer.
"""

import json
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import matplotlib.animation as animation
import networkx as nx
import numpy as np
import os


def _make_power_ax(ps, ax, failed_buses, failed_lines):
    """Draw power layer using pandapower.plotting — coords from bus.geo (GeoJSON)."""
    try:
        import pandapower.plotting as pplot
        net = ps.get_net_for_plotting()

        # Bus coloring: green=powered+comm, orange=powered no comm, red=dead, gold=substation
        n_buses = len(net.bus)
        bus_colors = []
        for i in range(n_buses):
            vm = net.res_bus.at[i, "vm_pu"] if i in net.res_bus.index else 1.0
            has_power = (not np.isnan(vm) and vm > 0.01)
            if i == 0:
                bus_colors.append("#f39c12")           # substation
            elif not has_power:
                bus_colors.append("#e74c3c")           # physically dead
            elif i in failed_buses:
                bus_colors.append("#e67e22")           # powered but comm lost
            else:
                bus_colors.append("#2ecc71")           # normal

        n_lines = len(net.line)
        line_colors = []
        for i in range(n_lines):
            if i in failed_lines:
                line_colors.append("#e74c3c")
            else:
                line_colors.append("#95a5a6")

        bc = pplot.create_bus_collection(net, size=0.08, color=bus_colors, zorder=2)
        lc = pplot.create_line_collection(net, color=line_colors, linewidths=1.2,
                                           zorder=1)
        all_colls = [lc, bc]

        # Add load markers
        try:
            load_c = pplot.create_load_collection(net, size=0.04, color="#e67e22", zorder=3)
            all_colls.append(load_c)
        except Exception:
            pass

        # Add generator/sgen markers
        try:
            sg_colors = []
            for i in range(len(net.sgen)):
                sg_colors.append("#f1c40f" if net.sgen.at[i, "in_service"] else "#95a5a6")
            if len(sg_colors) > 0:
                sg_c = pplot.create_sgen_collection(net, size=0.06, color=sg_colors, zorder=3)
                all_colls.append(sg_c)
        except Exception:
            pass

        pplot.draw_collections(all_colls, ax=ax)

        # Bus number labels — placed outside bus circles, alternating above/below
        bus_geo = ps.get_bus_geo()
        all_y = [y for _, (_, y) in bus_geo.items()]
        y_med = sorted(all_y)[len(all_y) // 2] if all_y else 0
        for b, (bx, by) in bus_geo.items():
            dy = 0.14 if by >= y_med else -0.14  # data-coord offset outside 0.08-radius circle
            ax.text(bx, by + dy, str(b), fontsize=5, ha="center", va="center",
                    color="#2c3e50", fontweight="bold", zorder=20,
                    clip_on=False,
                    bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="#ccc", alpha=0.85))

        # Label substation from GeoJSON coords
        try:
            g0 = net.bus.at[0, "geo"]
            if isinstance(g0, str) and g0 != "nan":
                c = json.loads(g0)["coordinates"]
                ax.annotate("Sub", (c[0], c[1]),
                            textcoords="offset points", xytext=(5, 5),
                            fontsize=8, fontweight="bold", color="#f39c12")
        except Exception:
            pass
    except Exception as e:
        ax.text(0.5, 0.5, f"pandapower plot error: {e}", ha="center",
                transform=ax.transAxes, fontsize=9, color="red")
    ax.set_title("Power Layer (IEEE 33-bus + Loads + DGs)", fontsize=11, fontweight="bold")
    ax.axis("off")


def _make_comm_ax(comm_net, coupling, failed_comm, ax, comm_coords=None):
    """Draw communication layer — alive (solid) vs failed (dashed) links + node states."""
    G = nx.Graph()
    for i in range(comm_net.n_total):
        G.add_node(i)
    for a, b in comm_net.graph.edges():
        G.add_edge(a, b)

    if comm_coords is None:
        pos = nx.spring_layout(G, seed=42, k=1.5)
    else:
        pos = comm_coords

    # Split edges: alive if both endpoints alive, failed otherwise
    edges_alive = []; edges_dead = []
    for a, b in G.edges():
        if a in failed_comm or b in failed_comm:
            edges_dead.append((a, b))
        else:
            edges_alive.append((a, b))

    # Draw failed edges first (dashed, red)
    if edges_dead:
        nx.draw_networkx_edges(G, pos, ax=ax, edgelist=edges_dead,
                               edge_color="#e74c3c", width=0.6, style="dashed", alpha=0.5)

    # Draw alive edges (solid, gray-blue)
    if edges_alive:
        nx.draw_networkx_edges(G, pos, ax=ax, edgelist=edges_alive,
                               edge_color="#5dade2", width=1.0, style="solid", alpha=0.7)

    # Node coloring: RTU if it covers any bus, relay otherwise
    has_buses = set()
    if coupling:
        for c in range(comm_net.n_total):
            if coupling.get_rtu_coverage(c):
                has_buses.add(c)
    colors = []
    for n in G.nodes():
        if n in failed_comm:
            colors.append("#e74c3c")
        elif n == 0:
            colors.append("#2980b9")
        elif n in has_buses:
            colors.append("#27ae60")
        else:
            colors.append("#27ae60")  # all RTU, no relays

    nx.draw_networkx_nodes(G, pos, ax=ax, node_size=100, node_color=colors,
                           edgecolors="#333", linewidths=0.5)
    nx.draw_networkx_labels(G, pos, ax=ax,
                            labels={i: str(i) for i in range(comm_net.n_total)},
                            font_size=6)
    ax.set_title(f"Comm Layer ({comm_net.n_total} nodes, spatial grid)", fontsize=11, fontweight="bold")
    ax.axis("off")


def plot_topology_static(ps, comm_net, coupling, failed_buses, failed_lines,
                         failed_comm, output_path):
    """CPPS dual-layer topology: power (left) + comm (right)."""
    bus_geo = ps.get_bus_geo()
    comm_coords = comm_net.get_comm_coords()

    fig, (ax_pow, ax_comm) = plt.subplots(1, 2, figsize=(22, 9))
    _make_power_ax(ps, ax_pow, failed_buses, failed_lines)
    _make_comm_ax(comm_net, coupling, failed_comm, ax_comm, comm_coords)

    leg_p = [mpatches.Patch(color="#f39c12", label="Substation"),
             mpatches.Patch(color="#2ecc71", label="Powered + Comm OK"),
             mpatches.Patch(color="#e67e22", label="Powered, No Comm"),
             mpatches.Patch(color="#e74c3c", label="Physically Dead"),
             mpatches.Patch(color="#f1c40f", label="DG (active)"),
             mpatches.Patch(color="#95a5a6", label="DG (inactive)")]
    leg_c = [mpatches.Patch(color="#2980b9", label="Ctrl Center"),
             mpatches.Patch(color="#27ae60", label="RTU (alive)"),
             mpatches.Patch(color="#e74c3c", label="Failed Node"),
             mlines.Line2D([0],[0], color="#5dade2", lw=2, label="Link (alive)"),
             mlines.Line2D([0],[0], color="#e74c3c", lw=1, ls="dashed", label="Link (failed)")]
    ax_pow.legend(handles=leg_p, loc="lower left", fontsize=6)
    ax_comm.legend(handles=leg_c, loc="lower left", fontsize=6)

    fig.suptitle("CPPS — Power (IEEE33 + Loads + DGs) & Communication (BA 20-node)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"  [OK] Topology: {output_path}")


def plot_cascading_animation(history, ps, coupling, output_path):
    """Animated cascading failure propagation."""
    bus_geo = ps.get_bus_geo()
    comm_coords = coupling.get_comm_coords(bus_geo)

    fig, (ax_pow, ax_comm) = plt.subplots(1, 2, figsize=(18, 8))

    def draw_frame(idx):
        ax_pow.clear(); ax_comm.clear()
        s = history[min(idx, len(history) - 1)]
        rnd = s.get("round", idx)
        fb = set(s.get("disconnected_buses", []))
        fl = set(s.get("disconnected_lines", []))
        fc = {c for c in range(coupling.M)
              if coupling.get_powering_bus(c) in fb or coupling.get_powering_bus(c) is None}

        _make_power_ax(ps, ax_pow, fb, fl)
        _make_comm_ax(coupling, fc, ax_comm, comm_coords)

        lost = s.get("lost_load_ratio", 0) * 100
        surv = s.get("survival_ratio", 1) * 100
        fig.suptitle(f"Round {rnd} — Lost Load: {lost:.0f}% | Survival: {surv:.0f}%",
                     fontsize=13, fontweight="bold")

    n_frames = len(history) + 3
    ani = animation.FuncAnimation(fig, lambda i: draw_frame(min(i, len(history) - 1)),
                                  frames=n_frames, interval=1500, blit=False)
    ani.save(output_path, writer="pillow", dpi=100)
    plt.close()
    print(f"  [OK] Animation: {output_path}")


def plot_timeseries(history, output_path):
    """Key metrics vs cascade round — 3x3 layout with data throughput."""
    rnds = [h.get("t", h.get("round", i)) for i, h in enumerate(history)]
    surv = [h.get("survival_ratio", len(h.get("energized",[]))/33) * 100 for h in history]
    # Load loss in MW (actual values)
    lost_mw = [h.get("lost_load_mw", 0) for h in history]
    n_en = [h.get("num_energized", len(h.get("energized", []))) for h in history]
    n_isl = [h.get("islands", 1) for h in history]
    avg_d = [min(h.get("avg_delay", 0), 500) for h in history]
    max_d = [min(h.get("max_delay", 0), 500) for h in history]
    dg_out = [h.get("dg_output", 0.0) for h in history]
    # Data throughput (Mbps) and flow count
    data_tp = [h.get("flows_total_tp", 0.0) for h in history]
    flows_n = [h.get("flows_count", 0) for h in history]
    avg_v = []
    for h in history:
        v = h.get("voltages", np.ones(33))
        mask = v > 0.01
        avg_v.append(float(np.mean(v[mask])) if np.any(mask) else 0.0)

    fig, axes = plt.subplots(3, 3, figsize=(20, 14))

    # (0,0): Bus Survival (%)
    ax = axes[0, 0]
    ax.plot(rnds, surv, "o-", color="#2ecc71", lw=2, ms=8)
    ax.fill_between(rnds, 0, surv, alpha=0.15, color="#2ecc71")
    ax.set_ylabel("%"); ax.set_title("Bus Survival")
    ax.set_ylim(0, 105); ax.grid(True, alpha=0.3)
    for r, v in zip(rnds, surv):
        ax.annotate(f"{v:.0f}%", (r, v), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=8)

    # (0,1): Load Loss (MW)
    ax = axes[0, 1]
    ax.plot(rnds, lost_mw, "s-", color="#e74c3c", lw=2, ms=8)
    ax.fill_between(rnds, 0, lost_mw, alpha=0.15, color="#e74c3c")
    ax.set_ylabel("MW"); ax.set_title("Load Loss (MW)")
    ax.grid(True, alpha=0.3)
    for r, v in zip(rnds, lost_mw):
        ax.annotate(f"{v:.2f}", (r, v), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=8)

    # (0,2): Data Throughput (Mbps) — fills previously empty cell
    ax = axes[0, 2]
    ax.plot(rnds, data_tp, "p-", color="#1abc9c", lw=2, ms=8)
    ax.fill_between(rnds, 0, data_tp, alpha=0.15, color="#1abc9c")
    ax.set_ylabel("Mbps"); ax.set_title("Data Throughput")
    ax.grid(True, alpha=0.3)
    for r, v in zip(rnds, data_tp):
        if v > 0:
            ax.annotate(f"{v:.1f}", (r, v), textcoords="offset points",
                        xytext=(0, 10), ha="center", fontsize=8)

    # (1,0): Energized Buses
    ax = axes[1, 0]
    ax.bar(rnds, n_en, color="#3498db", alpha=0.7, width=0.6)
    ax.axhline(y=33, color="#95a5a6", ls="--", lw=1, label="Total (33)")
    ax.set_ylabel("Count"); ax.set_title("Energized Buses")
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3, axis="y")

    # (1,1): Voltage Distribution
    ax = axes[1, 1]
    vdata = [h.get("voltages", np.ones(33))[h.get("voltages", np.ones(33)) > 0.01]
             for h in history]
    vlbls = [f"t={rnds[i]}" for i, h in enumerate(history)]
    if vdata and any(len(v) > 0 for v in vdata):
        bp = ax.boxplot(vdata, labels=vlbls, patch_artist=True)
        for p in bp["boxes"]:
            p.set_facecolor("#3498db"); p.set_alpha(0.5)
    ax.set_ylabel("Voltage (pu)"); ax.set_title("Voltage Distribution")
    ax.grid(True, alpha=0.3, axis="y")

    # (1,2): Active Flow Count — fills second empty cell
    ax = axes[1, 2]
    ax.bar(rnds, flows_n, color="#1abc9c", alpha=0.7, width=0.6)
    ax.set_ylabel("Count"); ax.set_title("Active Communication Flows")
    ax.grid(True, alpha=0.3, axis="y")

    # (2,0): Islands
    ax = axes[2, 0]
    ax.plot(rnds, n_isl, "D-", color="#e67e22", lw=2, ms=6)
    ax.set_xlabel("Time (s)"); ax.set_ylabel("Count"); ax.set_title("Number of Islands")
    ax.grid(True, alpha=0.3)

    # (2,1): Communication delay
    ax = axes[2, 1]
    ax.plot(rnds, avg_d, "o-", color="#9b59b6", lw=2, ms=6, label="Avg delay")
    ax.plot(rnds, max_d, "s--", color="#e74c3c", lw=1.5, ms=5, label="Max delay")
    ax.set_xlabel("Time (s)"); ax.set_ylabel("Delay (ms)"); ax.set_title("Comm Delay Trend")
    ax.legend(fontsize=7); ax.grid(True, alpha=0.3)

    # (2,2): DG total output
    ax = axes[2, 2]
    ax.fill_between(rnds, 0, dg_out, alpha=0.3, color="#27ae60")
    ax.plot(rnds, dg_out, "^-", color="#27ae60", lw=2, ms=6)
    ax.set_xlabel("Time (s)"); ax.set_ylabel("MW"); ax.set_title("DG Total Output")
    ax.grid(True, alpha=0.3)

    fig.suptitle("CPPS Cascading Failure Metrics", fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"  [OK] Timeseries: {output_path}")


def plot_fused_topology(ps, comm_net, coupling, failed_buses, failed_lines,
                        failed_comm, output_path):
    """Single fused view: power + BA comm network + dependency edges + DG markers."""
    import pandapower.plotting as pplot
    bus_geo = ps.get_bus_geo()
    comm_coords = comm_net.get_comm_coords(bus_geo)
    net = ps.get_net_for_plotting()
    comm_edges = list(comm_net.graph.edges())

    fig, ax = plt.subplots(figsize=(18, 14))

    # ---- Power layer (solid circles, gray lines) ----
    n_buses = len(net.bus)
    bus_colors_p = []
    for i in range(n_buses):
        vm = net.res_bus.at[i, "vm_pu"] if i in net.res_bus.index else 1.0
        has_power = (not np.isnan(vm) and vm > 0.01)
        if i == 0:
            bus_colors_p.append("#f39c12")
        elif not has_power:
            bus_colors_p.append("#e74c3c")
        elif i in failed_buses:
            bus_colors_p.append("#e67e22")
        else:
            bus_colors_p.append("#2ecc71")

    n_lines = len(net.line)
    line_colors_p = []
    for i in range(n_lines):
        line_colors_p.append("#e74c3c" if i in failed_lines else "#bdc3c7")

    bc = pplot.create_bus_collection(net, size=0.07, color=bus_colors_p, zorder=4)
    lc = pplot.create_line_collection(net, color=line_colors_p, linewidths=1.5, zorder=1)
    pplot.draw_collections([lc, bc], ax=ax)

    # Bus number labels — fused view
    all_y = [y for _, (_, y) in bus_geo.items()]
    y_med = sorted(all_y)[len(all_y)//2] if all_y else 0
    for b, (bx, by) in bus_geo.items():
        dy = 0.14 if by >= y_med else -0.14
        ax.text(bx, by + dy, str(b), fontsize=5, ha="center", va="center",
                color="#2c3e50", fontweight="bold", zorder=20,
                clip_on=False,
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="#ccc", alpha=0.85))

    # ---- Communication layer (BA scale-free, 20 nodes) ----
    G = nx.Graph()
    for i in range(comm_net.n_total):
        G.add_node(i)
    comm_edges = list(comm_net.graph.edges())
    for a, b in comm_edges:
        G.add_edge(a, b)

    has_buses_f = set()
    if coupling:
        for c in range(comm_net.n_total):
            if coupling.get_rtu_coverage(c):
                has_buses_f.add(c)
    node_cols = []; node_szs = []
    for n in G.nodes():
        if n in failed_comm:             node_cols.append("#e74c3c")
        elif n == 0:                     node_cols.append("#2980b9")
        elif n in has_buses_f:           node_cols.append("#27ae60")
        else:                            node_cols.append("#27ae60")
        node_szs.append(60)

    ecols = ["#e74c3c" if (a in failed_comm or b in failed_comm) else "#1abc9c"
             for a, b in comm_edges]
    nx.draw_networkx_edges(G, comm_coords, ax=ax, edgelist=comm_edges,
                           edge_color=ecols, width=0.5, style="--", alpha=0.5)
    nx.draw_networkx_nodes(G, comm_coords, ax=ax, node_color=node_cols,
                           node_size=node_szs, edgecolors="#333", linewidths=0.3)

    # ---- Dependency edges (1:1 bus↔comm) ----
    for bus in range(33):
        if bus not in bus_geo or bus not in comm_coords: continue
        bx, by = bus_geo[bus]; cx, cy = comm_coords[bus]
        ax.plot([cx, bx], [cy, by], linestyle="-.", color="#9b59b6",
                linewidth=0.3, alpha=0.3, zorder=0)

    # ---- DG markers ----
    for dg_b in [14, 22, 30]:
        if dg_b in bus_geo:
            active = any(int(sg["bus"]) == dg_b and sg["in_service"]
                        for _, sg in ps.net.sgen.iterrows())
            ax.scatter(*bus_geo[dg_b], marker="*", s=120,
                       c="#f1c40f" if active else "#95a5a6",
                       edgecolors="#333", linewidths=0.5, zorder=12)

    # ---- Legend ----
    legend_elements = [
        mpatches.Patch(color="#f39c12", label="Substation (Bus 0)"),
        mpatches.Patch(color="#2ecc71", label="Power Bus (active)"),
        mpatches.Patch(color="#e74c3c", label="Power Bus (failed)"),
        mlines.Line2D([0], [0], color="#bdc3c7", lw=2, label="Power Line (active)"),
        mlines.Line2D([0], [0], color="#e74c3c", lw=2, label="Power Line (failed)"),
        mpatches.Patch(color="#2980b9", label="Comm Ctrl Center"),
        mpatches.Patch(color="#27ae60", label="Comm RTU"),
        mlines.Line2D([0], [0], color="#1abc9c", lw=2, ls="--", label="Fiber Link (active)"),
        mlines.Line2D([0], [0], color="#e74c3c", lw=2, ls="--", label="Fiber Link (failed)"),
        mlines.Line2D([0], [0], color="#9b59b6", lw=1, ls="-.", label="Dependency (bus↔RTU)"),
    ]
    ax.legend(handles=legend_elements, loc="lower left", fontsize=8,
              ncol=2, framealpha=0.92, edgecolor="#ccc")

    ax.set_title(
        "CPPS Fused Topology — Power Grid + Communication Network Overlay\n"
        "Solid circles = power buses  |  Squares/Diamonds = comm nodes  |  "
        "Gray solid = power lines  |  Cyan dashed = fiber links  |  "
        "Purple dotted = power↔comm dependency",
        fontsize=13, fontweight="bold", pad=16)
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close()
    print(f"  [OK] Fused topology: {output_path}")
