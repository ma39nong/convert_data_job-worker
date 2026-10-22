"""AdaTempo processing step for joint-position trajectories."""
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.stats import rankdata
from ._common import validate
from .alignment import descriptor_distances, dtw_moves, phase_cost


def arc_length(x):
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(x, axis=0), axis=1))]
    return arc / arc[-1] if arc[-1] > 1e-10 else np.linspace(0, 1, len(x))


def interpolate(x, positions):
    x = np.asarray(x, float)
    return np.column_stack([np.interp(positions, np.arange(len(x)), col) for col in x.T])


def align_phase(y, reference, arc, ref_arc):
    cost = phase_cost(y, reference, arc, ref_arc)
    penalty = 0.01 * np.median(cost)
    n, m = cost.shape
    move = dtw_moves(cost, penalty)
    matches = [[] for _ in range(n)]
    i, j = n - 1, m - 1
    while i >= 0 and j >= 0:
        matches[i].append(ref_arc[j])
        k = move[i, j]
        i -= int(k != 2)
        j -= int(k != 1)
    phase = np.maximum.accumulate([np.median(v) for v in matches])
    span = phase[-1] - phase[0]
    return (phase - phase[0]) / span if span > 1e-10 else np.linspace(0, 1, n)


def adatempo(episodes, cmin=1.0, cmax=3.0, bins=160):
    """Paper Sec. III, joint-position-only CPU prototype, no policy training."""
    validate(episodes)
    if not (np.isfinite([cmin, cmax]).all() and 1 <= cmin <= cmax and bins >= 4):
        raise ValueError("1 <= cmin <= cmax and bins >= 4 required")
    targets = [gaussian_filter1d(np.asarray(e["actions"], float), 1.25, axis=0) for e in episodes]
    lo, hi = np.percentile(np.concatenate(targets), [5, 95], axis=0)
    motions = [(x - lo) / np.maximum(hi - lo, 1e-8) for x in targets]
    descriptors = [np.column_stack([x, np.asarray(e["gripper"]) * 1.2])
                   for x, e in zip(motions, episodes)]
    arcs = [arc_length(x) for x in motions]
    grid = np.linspace(0, 1, 160)
    sampled = []
    for y, arc in zip(descriptors, arcs):
        unique, idx = np.unique(arc, return_index=True)
        sampled.append(np.column_stack([np.interp(grid, unique, col[idx]) for col in y.T]))
    distances = descriptor_distances(sampled)
    medoid = int(np.argmin(np.median(distances, axis=1)))
    phases = [align_phase(y, descriptors[medoid], a, arcs[medoid])
              for y, a in zip(descriptors, arcs)]
    ranks = []
    for phase in phases:
        counts, _ = np.histogram(phase, bins=bins, range=(0, 1))
        tempo = 1 / (gaussian_filter1d(counts.astype(float), 3) + 1e-8)
        ranks.append((rankdata(tempo) - 1) / (bins - 1))
    consensus = np.median(ranks, axis=0)
    disagreement = np.median(abs(np.asarray(ranks) - consensus), axis=0)
    confidence = 1 / (1 + (disagreement / 0.15) ** 2)
    centers = (np.arange(bins) + 0.5) / bins
    results, traces = [], []
    for e, phase, motion in zip(episodes, phases, motions):
        n = len(phase)
        speed = cmin + (cmax - cmin) * np.interp(phase, centers, consensus) * np.interp(phase, centers, confidence)
        edge = max(1, int(np.ceil(n * 0.035)))
        speed[:edge] = speed[-edge:] = cmin
        # Conservatively keep wholly static episodes at 1x.
        if np.linalg.norm(np.diff(motion, axis=0), axis=1).sum() < 1e-10:
            speed[:] = 1
        smooth = []
        for t in range(n):
            future = speed[np.minimum(np.arange(t, t + 10), n - 1)]
            minimum = float(future.min())
            target = minimum - 0.10 * np.log(np.exp(-(future - minimum) / 0.10).mean())
            previous = smooth[-1] if smooth else speed[0]
            smooth.append(float(previous + np.clip(target - previous, -0.15, 0.06)))
        virtual = [0.0]
        while virtual[-1] < n - 1:
            step = float(np.interp(virtual[-1], np.arange(n), smooth))
            virtual.append(min(n - 1, virtual[-1] + max(1.0, step)))
        obs_pos, act_pos = np.asarray(virtual[:-1]), np.asarray(virtual[1:])
        results.append({"id": e["id"],
                        "qpos": interpolate(e["qpos"], obs_pos).tolist(),
                        "actions": interpolate(e["actions"], act_pos).tolist(),
                        "gripper": np.asarray(e["gripper"])[np.rint(obs_pos).astype(int)].tolist(),
                        "action_gripper": np.asarray(e["gripper"])[np.rint(act_pos).astype(int)].tolist(),
                        "source_index": obs_pos.tolist(), "action_source_index": act_pos.tolist()})
        traces.append({"id": e["id"], "phase": phase.tolist(), "speed_profile": smooth})
    return results, traces
