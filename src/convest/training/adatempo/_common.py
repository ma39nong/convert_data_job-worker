"""Shared array checks used by the two container algorithms."""
import numpy as np


def validate(episodes):
    if not episodes:
        raise ValueError("dataset must contain episodes")
    ids = [e["id"] for e in episodes]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate episode ids")
    dimension = None
    for e in episodes:
        a, q = np.asarray(e["actions"], float), np.asarray(e["qpos"], float)
        if a.ndim != 2 or len(a) < 2 or a.shape != q.shape:
            raise ValueError("actions/qpos must have matching [T,D], T >= 2")
        if a.shape[1] < 1 or not np.isfinite(a).all() or not np.isfinite(q).all():
            raise ValueError("nonfinite or empty channels")
        if dimension is not None and dimension != a.shape[1]:
            raise ValueError("inconsistent action dimensions")
        dimension = a.shape[1]
        g = np.asarray(e["gripper"], float)
        if g.shape != (len(a),) or not np.isfinite(g).all():
            raise ValueError("gripper must align with frames")
