"""Memory-bounded distances and the original DTW recurrence, compiled on CPU."""
import numpy as np
from scipy.spatial.distance import cdist

try:
    from numba import njit
except ImportError:
    def njit(fn):
        return fn


@njit
def dtw_moves(cost, penalty):
    n, m = cost.shape
    score = np.full((n + 1, m + 1), np.inf)
    score[0, 0] = 0
    move = np.zeros((n, m), dtype=np.int8)
    for i in range(n):
        for j in range(m):
            best, direction = score[i, j], 0
            vertical, horizontal = score[i, j + 1] + penalty, score[i + 1, j] + penalty
            if vertical < best:
                best, direction = vertical, 1
            if horizontal < best:
                best, direction = horizontal, 2
            score[i + 1, j + 1] = cost[i, j] + best
            move[i, j] = direction
    return move


def phase_cost(y, reference, arc, ref_arc):
    cost = cdist(y, reference, metric="sqeuclidean")
    cost += 0.17 * (arc[:, None] - ref_arc[None, :]) ** 2
    return cost


def descriptor_distances(sampled):
    flat = np.asarray(sampled).reshape(len(sampled), -1)
    return cdist(flat, flat) / np.sqrt(flat.shape[1])
