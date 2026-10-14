"""Phase-1 power: paired-by-surface exact one-sided McNemar, alpha .025 (Bonferroni over 2 hypotheses).

Usage: python scripts/phase-1-power-simulation.py
Effect sizes: pilot pooled estimates (0b + 0c, both models) and conservative shrinkage toward the null.
"""
import random
from math import comb


def mcnemar_p(b: int, c: int) -> float:
    """One-sided exact p: P(X >= b) for X ~ Binomial(b + c, 1/2)."""
    n = b + c
    return 1.0 if n == 0 else sum(comb(n, k) for k in range(b, n + 1)) / 2 ** n


def power(n_pairs: int, p_a: float, p_b: float, alpha: float = 0.025, sims: int = 4000, seed: int = 0) -> float:
    rng = random.Random(seed)
    hits = 0
    for _ in range(sims):
        b = c = 0
        for _ in range(n_pairs):
            a, bb = rng.random() < p_a, rng.random() < p_b
            b += a and not bb
            c += bb and not a
        hits += mcnemar_p(b, c) < alpha
    return hits / sims


if __name__ == "__main__":
    for label, pa, pb in [("H_leak pilot", .97, .29), ("H_leak conservative", .90, .55),
                          ("H_typed pilot", .85, .05), ("H_typed conservative", .70, .25)]:
        print(label, {n: round(power(n, pa, pb), 3) for n in (8, 12, 16, 20, 24, 32)})
