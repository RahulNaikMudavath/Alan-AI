"""Float32 vector packing and similarity maths.

Embeddings are stored as raw little-endian float32 rather than JSON or a pickle:
compact, exact, and independent of any third-party library. At RecallKit's
scale (thousands of memories per user, a few hundred dimensions) plain Python
is fast enough; ``numpy`` or ``sqlite-vec`` are the upgrade path if measurements
ever justify them.
"""

import math
import struct
from collections.abc import Sequence

FLOAT32_SIZE = 4


def pack_vector(vector: Sequence[float]) -> bytes:
    """Pack a vector into a little-endian float32 blob."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack a blob written by :func:`pack_vector`.

    Raises ValueError on a blob that cannot be a float32 vector, which is how
    corrupt rows are detected at read time instead of crashing a request.
    """
    if len(blob) % FLOAT32_SIZE != 0:
        raise ValueError("embedding blob length is not a multiple of 4 bytes")
    return list(struct.unpack(f"<{len(blob) // FLOAT32_SIZE}f", blob))


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """Scale a vector to unit length, leaving an all-zero vector untouched."""
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1], or 0.0 when either vector is all zeros.

    Writing normalized vectors makes the dot product sufficient, but this
    divides by the norms anyway so a row written by an older version or by hand
    still scores correctly.
    """
    if len(left) != len(right):
        raise ValueError(f"vector length mismatch: {len(left)} != {len(right)}")
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
