# PHASE: Phased Hybrid Analysis and Simulation Engine

PHASE is a time-stepped co-simulation framework for analyzing cascading failures in cyber-physical power systems (CPPS). It couples an IEEE 33-bus distribution network (pandapower) with a dynamic-routing communication network (ns-3 OLSR) through a seven-phase iterative loop.

## Overview

Modern power grids depend on communication networks for real-time monitoring and control. When disasters disrupt both layers simultaneously, failures can propagate bidirectionally — power outages disable communication nodes, and communication failures block recovery commands, forming a self-reinforcing cascade. PHASE captures this process at fine temporal resolution: each simulation step resolves RTU battery depletion, per-flow routing delays, reachability-gated control, island detection with DG dispatch, and protection misoperation.

## File Structure

```
PHASE/
├── cpps_simulator.py          # Main simulation engine (7-phase loop)
├── power_system.py            # IEEE 33-bus power system model
├── comm_network.py            # Communication network model
├── coupling_model.py          # Cyber-physical coupling (P2C/C2P + battery)
├── visualization.py           # Result visualization
├── cpps_network_builder.py    # Interactive communication network builder
├── cpps_comm_config.json      # Default communication topology
├── scratch/
│   └── cpps_comm_sim.cc       # ns-3 C++ OLSR simulation program
└── README.md
```

## Requirements

- Python 3.8+ with packages:
  ```bash
  pip install pandapower networkx matplotlib numpy
  ```
- ns-3.47 (or compatible version) with C++17 compiler

## Installation

### 1. Install ns-3

```bash
cd ~
wget https://www.nsnam.org/releases/ns-allinone-3.47.tar.bz2
tar xjf ns-allinone-3.47.tar.bz2
cd ns-allinone-3.47/ns-3.47
```

### 2. Copy the C++ simulation program to ns-3 scratch

```bash
cp ~/PHASE/scratch/cpps_comm_sim.cc scratch/
```

### 3. Build ns-3

```bash
./ns3 build -j2
```

### 4. Configure WSL paths (Windows users)

Edit `cpps_simulator.py` and update the path constants:

```python
WSL_NS3 = "/home/<username>/ns-allinone-3.47/ns-3.47"
WSL_PROJ_LIN = "/home/<username>/PHASE"
```

## Usage

### Quick start (Python communication approximation)

```bash
cd ~/PHASE
python cpps_simulator.py --sim-mode python --duration 5 --seed 123
```

### Full ns-3 co-simulation

```bash
python cpps_simulator.py --sim-mode ns3 --duration 20 --seed 123 \
    --battery-cap 5 --over-trip 0.10 \
    --failed-lines "5,15,25" --destroyed-comm "4,5,8,9,12"
```

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--sim-mode` | `python` | `python` for fast approximation, `ns3` for full OLSR simulation |
| `--duration` | 15 | Simulation horizon (steps) |
| `--battery-cap` | 5 | RTU backup battery capacity (steps) |
| `--over-trip` | 0.10 | Protection over-trip probability |
| `--failed-lines` | — | Comma-separated line indices to destroy |
| `--destroyed-comm` | — | Comma-separated comm node indices to destroy |
| `--seed` | 42 | Random seed for reproducibility |

Run `python cpps_simulator.py --help` for all options.

## Output

After a successful run, results are saved in `output/`:
- `indicator_curves.csv` — time-series metrics (energized buses, lost load, DG output, etc.)
- `timeseries.png` — 5-panel time-series visualization
- `topology_fused.png` — fused cyber-physical topology
- `snapshots/` — per-timestep topology snapshots

## Citation

If you use PHASE in your research, please cite our paper:

> Y. Sun, Z. Shi, T. Zhang, X. Huang, and K. Li, "PHASE: A Phased Co-Simulation Framework for Cyber-Physical Cascading Failure Analysis," in *Proc. IEEE Conf.*, 2026.

## License

This project is released for academic use. Contact the authors for permissions.
