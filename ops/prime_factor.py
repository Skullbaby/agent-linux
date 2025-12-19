# ops/prime_factor.py
from __future__ import annotations

import math
import time
from typing import Any, Dict, List

from . import register_op


def _prime_factors(n: int) -> List[int]:
    factors: List[int] = []
    if n <= 1:
        return factors

    # factor out 2s
    while n % 2 == 0:
        factors.append(2)
        n //= 2

    # odd factors
    f = 3
    limit = int(math.isqrt(n))
    while f <= limit and n > 1:
        while n % f == 0:
            factors.append(f)
            n //= f
            limit = int(math.isqrt(n))
        f += 2

    if n > 1:
        factors.append(n)

    return factors


@register_op("prime_factor")
def map_prime_factor(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return prime factorization of integer n (as a list of prime factors)."""
    n_raw = payload.get("n")
    if n_raw is None:
        raise ValueError("payload.n is required")

    try:
        n = int(n_raw)
    except (TypeError, ValueError):
        raise ValueError("payload.n must be an int")

    if n < 0:
        raise ValueError("payload.n must be >= 0")

    # Safety limit: factoring huge integers can take a long time
    if n > 10**14:
        raise ValueError("payload.n too large (max 1e14)")

    start = time.time()
    factors = _prime_factors(n)
    elapsed_ms = (time.time() - start) * 1000.0

    return {
        "n": n,
        "factors": factors,
        "compute_time_ms": elapsed_ms,
    }
