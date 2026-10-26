"""Vector maths: exact, deterministic, no database."""

import math

import pytest

from app.retrieval.vectors import (
    cosine_similarity,
    l2_normalize,
    pack_vector,
    unpack_vector,
)


def test_pack_and_unpack_round_trip() -> None:
    vector = [0.5, -1.25, 3.0, 0.0]

    assert unpack_vector(pack_vector(vector)) == pytest.approx(vector)


def test_packed_vector_is_four_bytes_per_dimension() -> None:
    assert len(pack_vector([1.0, 2.0, 3.0])) == 12


def test_unpack_rejects_a_truncated_blob() -> None:
    with pytest.raises(ValueError, match="multiple of 4"):
        unpack_vector(b"\x00\x00\x01")


def test_normalize_produces_unit_length() -> None:
    normalized = l2_normalize([3.0, 4.0])

    assert math.sqrt(sum(value * value for value in normalized)) == pytest.approx(1.0)
    assert normalized == pytest.approx([0.6, 0.8])


def test_normalize_leaves_the_zero_vector_alone() -> None:
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_cosine_of_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_of_opposite_vectors_is_minus_one() -> None:
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_ignores_magnitude() -> None:
    assert cosine_similarity([1.0, 1.0], [10.0, 10.0]) == pytest.approx(1.0)


def test_cosine_with_a_zero_vector_is_zero() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_rejects_mismatched_dimensions() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])
