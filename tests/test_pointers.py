"""Tests for the server-side data-reference store.

Covers the contract the rest of MSMCP relies on: registration and lookup of
immutable payloads, distinct failures for malformed / unknown / expired /
wrong-kind references, namespace isolation, bounded resources, deterministic
release, and safe concurrent access from the threads the MCP server actually
uses (the event loop plus worker threads running scans).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from msmcp.mzml import Spectrum
from msmcp.provenance import provenance_for
from msmcp.state import store as reference_store
from msmcp.state.pointers import (
    DataKind,
    ExpiredReferenceError,
    PointerStore,
    ReferenceKindError,
    ReferencePayloadError,
    StoreLimitError,
    UnknownReferenceError,
    parse_identifier,
)


def spectrum(n_peaks: int = 3, *, index: int | None = 0) -> Spectrum:
    """A small, valid spectrum for registration."""
    mz = np.arange(100.0, 100.0 + n_peaks, dtype=np.float64)
    intensity = np.ones(n_peaks, dtype=np.float64)
    return Spectrum(
        index=index,
        ms_level=2,
        retention_time=1.5,
        precursor_mz=200.0,
        mz=mz,
        intensity=intensity,
    )


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Registration and lookup
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_register_and_lookup_spectrum(self) -> None:
        store = PointerStore()
        original = spectrum()
        reference = store.register(original, kind=DataKind.SPECTRUM)

        assert reference.kind is DataKind.SPECTRUM
        assert reference.identifier.startswith("ptr:spectrum:")
        assert reference.shape == (3,)
        assert reference.n_bytes == 3 * 8 * 2  # m/z + intensity, float64

        stored = store.lookup(reference.identifier)
        assert isinstance(stored, Spectrum)
        np.testing.assert_array_equal(stored.mz, original.mz)
        np.testing.assert_array_equal(stored.intensity, original.intensity)

    def test_register_and_lookup_peak_list(self) -> None:
        store = PointerStore()
        peaks = np.array([[100.0, 5.0], [200.0, 9.0]], dtype=np.float64)
        reference = store.register(peaks, kind=DataKind.PEAK_LIST)
        assert reference.shape == (2, 2)
        np.testing.assert_array_equal(store.lookup(reference.identifier), peaks)

    def test_register_and_lookup_embedding(self) -> None:
        store = PointerStore()
        vector = np.linspace(0.0, 1.0, 8)
        reference = store.register(vector, kind=DataKind.EMBEDDING)
        assert reference.shape == (8,)
        assert store.lookup(
            reference.identifier, expected_kind=DataKind.EMBEDDING
        ).shape == (8,)

    def test_identifier_is_opaque_and_not_an_object_id(self) -> None:
        store = PointerStore()
        payload = spectrum()
        reference = store.register(payload, kind=DataKind.SPECTRUM)
        _, uid = parse_identifier(reference.identifier)
        assert str(id(payload)) not in uid
        assert str(id(payload.mz)) not in uid
        # Stable for the reference's lifetime, and stable across lookups.
        assert reference.identifier == store.reference(reference.identifier).identifier
        assert reference.identifier == reference.identifier

    def test_provenance_is_carried_and_exposed(self) -> None:
        store = PointerStore()
        provenance = provenance_for("load_spectrum", parameters={"spectrum_index": 2})
        reference = store.register(
            spectrum(), kind=DataKind.SPECTRUM, provenance=provenance
        )
        described = reference.to_dict()
        assert described["provenance"]["operation"] == "load_spectrum"
        assert described["provenance"]["parameters"]["spectrum_index"] == 2
        # The description never contains the payload.
        assert "mz" not in described

    def test_register_rejects_wrong_payload_type(self) -> None:
        store = PointerStore()
        with pytest.raises(
            ReferencePayloadError, match=r"requires an msmcp\.mzml\.Spectrum"
        ):
            store.register([[1.0, 2.0]], kind=DataKind.SPECTRUM)

    @pytest.mark.parametrize(
        "payload",
        [
            np.zeros((2, 3)),  # not (N, 2)
            np.zeros(5),  # not 2-D
            np.zeros((0, 2)),  # empty
            "not an array",
        ],
    )
    def test_register_rejects_invalid_peak_list(self, payload: object) -> None:
        store = PointerStore()
        with pytest.raises(ReferencePayloadError):
            store.register(payload, kind=DataKind.PEAK_LIST)

    def test_register_rejects_non_1d_embedding(self) -> None:
        store = PointerStore()
        with pytest.raises(ReferencePayloadError, match="1-D"):
            store.register(np.zeros((2, 2)), kind=DataKind.EMBEDDING)

    def test_register_rejects_mismatched_spectrum_arrays(self) -> None:
        store = PointerStore()
        broken = Spectrum(
            index=0,
            ms_level=1,
            retention_time=None,
            precursor_mz=None,
            mz=np.zeros(3),
            intensity=np.zeros(2),
        )
        with pytest.raises(ReferencePayloadError, match="same length"):
            store.register(broken, kind=DataKind.SPECTRUM)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------
class TestImmutability:
    def test_stored_payload_is_read_only(self) -> None:
        store = PointerStore()
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        stored = store.lookup(reference.identifier)
        assert not stored.mz.flags.writeable
        assert not stored.intensity.flags.writeable
        with pytest.raises(ValueError):
            stored.mz[0] = 999.0

    def test_caller_mutation_does_not_reach_the_store(self) -> None:
        """The store owns its payload; a later caller write must not corrupt it."""
        store = PointerStore()
        peaks = np.array([[100.0, 1.0]], dtype=np.float64)
        reference = store.register(peaks, kind=DataKind.PEAK_LIST)

        peaks[0, 1] = 999.0  # the caller keeps its own array and mutates it

        stored = store.lookup(reference.identifier)
        assert stored[0, 1] == pytest.approx(1.0)

    def test_two_lookups_return_the_same_object(self) -> None:
        """Lookup is a reference hand-out, not a copy: this is the reuse property."""
        store = PointerStore()
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        assert store.lookup(reference.identifier) is store.lookup(reference.identifier)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------
class TestReferenceFailures:
    @pytest.mark.parametrize(
        "identifier",
        [
            "",
            "ptr",
            "ptr:",
            "ptr:spectrum",
            "ptr::abc",
            "not-a-pointer",
            "ptr:gadget:abc",
        ],
    )
    def test_malformed_identifier(self, identifier: str) -> None:
        store = PointerStore()
        with pytest.raises(UnknownReferenceError):
            store.lookup(identifier)

    def test_unknown_but_well_formed_identifier(self) -> None:
        store = PointerStore()
        with pytest.raises(UnknownReferenceError, match="No live data reference"):
            store.lookup("ptr:spectrum:deadbeef")

    def test_wrong_kind_is_reported_distinctly(self) -> None:
        store = PointerStore()
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        with pytest.raises(ReferenceKindError, match="holds 'spectrum' data"):
            store.lookup(reference.identifier, expected_kind=DataKind.PEAK_LIST)

    def test_released_reference_is_gone(self) -> None:
        store = PointerStore()
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        assert store.release(reference.identifier) is True
        with pytest.raises(UnknownReferenceError):
            store.lookup(reference.identifier)

    def test_release_is_idempotent(self) -> None:
        store = PointerStore()
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        assert store.release(reference.identifier) is True
        assert store.release(reference.identifier) is False
        assert store.release("ptr:spectrum:never-existed") is False

    def test_expired_reference_is_reported_separately(self) -> None:
        clock = FakeClock()
        store = PointerStore(clock=clock)
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM, ttl_seconds=60.0)

        assert store.lookup(reference.identifier) is not None
        clock.advance(61.0)
        with pytest.raises(ExpiredReferenceError, match="expired"):
            store.lookup(reference.identifier)
        # Expiry is a release: the entry is gone and the byte total reclaimed.
        assert len(store) == 0
        assert store.stats().total_bytes == 0

    def test_default_ttl_applies_when_registering(self) -> None:
        clock = FakeClock()
        store = PointerStore(default_ttl_seconds=10.0, clock=clock)
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        assert reference.expires_at == pytest.approx(clock.now + 10.0)
        clock.advance(11.0)
        with pytest.raises(ExpiredReferenceError):
            store.lookup(reference.identifier)

    def test_no_ttl_means_no_expiry(self) -> None:
        clock = FakeClock()
        store = PointerStore(clock=clock)
        reference = store.register(spectrum(), kind=DataKind.SPECTRUM)
        assert reference.expires_at is None
        clock.advance(10_000.0)
        assert store.lookup(reference.identifier) is not None

    def test_per_registration_ttl_overrides_the_default(self) -> None:
        clock = FakeClock()
        store = PointerStore(default_ttl_seconds=10.0, clock=clock)
        reference = store.register(
            spectrum(), kind=DataKind.SPECTRUM, ttl_seconds=1000.0
        )
        assert reference.expires_at == pytest.approx(clock.now + 1000.0)


# ---------------------------------------------------------------------------
# Namespace isolation
# ---------------------------------------------------------------------------
class TestNamespaces:
    def test_lookup_is_confined_to_the_requested_namespace(self) -> None:
        store = PointerStore()
        reference = store.register(
            spectrum(), kind=DataKind.SPECTRUM, namespace="session-a"
        )

        assert store.lookup(reference.identifier, namespace="session-a") is not None
        with pytest.raises(UnknownReferenceError, match="does not belong"):
            store.lookup(reference.identifier, namespace="session-b")

    def test_clear_removes_only_one_namespace(self) -> None:
        store = PointerStore()
        a = store.register(spectrum(), kind=DataKind.SPECTRUM, namespace="a")
        b = store.register(spectrum(), kind=DataKind.SPECTRUM, namespace="b")

        assert store.clear(namespace="a") == 1
        with pytest.raises(UnknownReferenceError):
            store.lookup(a.identifier)
        assert store.lookup(b.identifier) is not None

    def test_clear_without_a_namespace_removes_everything(self) -> None:
        store = PointerStore()
        store.register(spectrum(), kind=DataKind.SPECTRUM, namespace="a")
        store.register(spectrum(), kind=DataKind.SPECTRUM, namespace="b")
        assert store.clear() == 2
        assert len(store) == 0
        assert store.stats().total_bytes == 0
        assert store.stats().namespaces == ()


# ---------------------------------------------------------------------------
# Bounded resources
# ---------------------------------------------------------------------------
class TestLimits:
    def test_reference_count_ceiling(self) -> None:
        store = PointerStore(max_references=2)
        store.register(spectrum(), kind=DataKind.SPECTRUM)
        store.register(spectrum(), kind=DataKind.SPECTRUM)
        with pytest.raises(StoreLimitError, match="configured maximum"):
            store.register(spectrum(), kind=DataKind.SPECTRUM)

    def test_byte_ceiling(self) -> None:
        store = PointerStore(max_total_bytes=64)
        store.register(spectrum(n_peaks=4), kind=DataKind.SPECTRUM)  # 4*8*2 = 64 bytes
        with pytest.raises(StoreLimitError, match="bytes"):
            store.register(spectrum(n_peaks=4), kind=DataKind.SPECTRUM)

    def test_releasing_frees_capacity(self) -> None:
        store = PointerStore(max_references=1)
        first = store.register(spectrum(), kind=DataKind.SPECTRUM)
        with pytest.raises(StoreLimitError):
            store.register(spectrum(), kind=DataKind.SPECTRUM)
        store.release(first.identifier)
        assert store.register(spectrum(), kind=DataKind.SPECTRUM) is not None

    def test_expiry_frees_capacity_without_an_explicit_sweep(self) -> None:
        clock = FakeClock()
        store = PointerStore(max_references=1, clock=clock)
        store.register(spectrum(), kind=DataKind.SPECTRUM, ttl_seconds=5.0)
        clock.advance(6.0)
        # Registration purges expired entries before enforcing the ceiling.
        assert store.register(spectrum(), kind=DataKind.SPECTRUM) is not None

    def test_limits_are_never_satisfied_by_dropping_live_data(self) -> None:
        """Exceeding a limit raises; it must not silently evict a live payload."""
        store = PointerStore(max_references=1)
        kept = store.register(spectrum(), kind=DataKind.SPECTRUM)
        with pytest.raises(StoreLimitError):
            store.register(spectrum(), kind=DataKind.SPECTRUM)
        assert store.lookup(kept.identifier) is not None

    def test_purge_expired_reports_what_it_removed(self) -> None:
        clock = FakeClock()
        store = PointerStore(clock=clock)
        store.register(spectrum(), kind=DataKind.SPECTRUM, ttl_seconds=5.0)
        store.register(spectrum(), kind=DataKind.SPECTRUM)  # no expiry
        assert store.purge_expired() == 0
        clock.advance(6.0)
        assert store.purge_expired() == 1
        assert len(store) == 1

    def test_stats_describe_occupancy(self) -> None:
        store = PointerStore(max_references=7, max_total_bytes=4096)
        store.register(spectrum(), kind=DataKind.SPECTRUM, namespace="x")
        stats = store.stats()
        assert stats.count == 1
        assert stats.total_bytes == 48
        assert stats.max_references == 7
        assert stats.max_total_bytes == 4096
        assert stats.namespaces == ("x",)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_references": 0}, "max_references"),
            ({"max_total_bytes": -1}, "max_total_bytes"),
            ({"default_ttl_seconds": 0.0}, "default_ttl_seconds"),
        ],
    )
    def test_invalid_configuration_is_rejected(
        self, kwargs: dict, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            PointerStore(**kwargs)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
class TestConcurrency:
    def test_concurrent_registration_and_lookup(self) -> None:
        """The store is shared by the event loop and worker threads; it must hold."""
        store = PointerStore(max_references=512)
        n_threads = 8
        per_thread = 25
        errors: list[BaseException] = []
        barrier = threading.Barrier(n_threads)

        def worker(worker_id: int) -> None:
            try:
                barrier.wait(timeout=10.0)
                for i in range(per_thread):
                    reference = store.register(
                        spectrum(n_peaks=worker_id + 1),
                        kind=DataKind.SPECTRUM,
                        namespace=f"t{worker_id}",
                    )
                    stored = store.lookup(
                        reference.identifier, namespace=f"t{worker_id}"
                    )
                    assert stored.n_peaks == worker_id + 1
                    assert store.release(reference.identifier)
                    _ = i
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            for future in [pool.submit(worker, i) for i in range(n_threads)]:
                future.result(timeout=30.0)

        assert not errors, errors
        # Every registration was released, so the byte total is back to zero.
        assert store.stats().total_bytes == 0

    def test_concurrent_release_is_not_double_counted(self) -> None:
        store = PointerStore()
        references = [
            store.register(spectrum(), kind=DataKind.SPECTRUM) for _ in range(50)
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda r: store.release(r.identifier), references * 2)
            )
        # Each reference is released exactly once; retries report False.
        assert sum(results) == len(references)
        assert store.stats().total_bytes == 0
        assert len(store) == 0


# ---------------------------------------------------------------------------
# The process-wide store helpers
# ---------------------------------------------------------------------------
class TestProcessWideStore:
    def test_helpers_round_trip(self) -> None:
        reference_store.reset_store()
        reference = reference_store.store_spectrum(spectrum(n_peaks=4))
        try:
            assert reference.kind is DataKind.SPECTRUM
            assert reference_store.reference_summary(reference.identifier)["shape"] == [
                4
            ]
            assert reference_store.peak_list_of(reference.identifier).shape == (4, 2)
        finally:
            reference_store.reset_store()

    def test_embedding_references_round_trip(self) -> None:
        """The store supports embedding payloads; the model layer can publish them."""
        reference_store.reset_store()
        vector = np.linspace(-1.0, 1.0, 16)
        reference = reference_store.store_embedding(vector)
        try:
            assert reference.kind is DataKind.EMBEDDING
            assert reference.shape == (16,)
            stored = reference_store.resolve(
                reference.identifier, expected_kind=DataKind.EMBEDDING
            )
            np.testing.assert_allclose(stored, vector)
            # An embedding is not a peak list, and the store says so.
            with pytest.raises(ReferenceKindError):
                reference_store.resolve_peak_list(reference.identifier)
        finally:
            reference_store.reset_store()

    def test_peak_list_of_accepts_a_peak_list_reference(self) -> None:
        reference_store.reset_store()
        peaks = np.array([[10.0, 1.0], [20.0, 2.0]])
        reference = reference_store.store_peak_list(peaks)
        try:
            np.testing.assert_array_equal(
                reference_store.peak_list_of(reference.identifier), peaks
            )
        finally:
            reference_store.reset_store()

    def test_resolve_spectrum_rejects_a_peak_list_reference(self) -> None:
        reference_store.reset_store()
        reference = reference_store.store_peak_list(np.array([[10.0, 1.0]]))
        try:
            with pytest.raises(ReferenceKindError):
                reference_store.resolve_spectrum(reference.identifier)
        finally:
            reference_store.reset_store()

    def test_store_from_env_reads_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(reference_store.ENV_MAX_REFERENCES, "3")
        monkeypatch.setenv(reference_store.ENV_MAX_TOTAL_BYTES, "1024")
        monkeypatch.setenv(reference_store.ENV_REFERENCE_TTL, "0")
        store = reference_store.store_from_env()
        assert store.max_references == 3
        assert store.max_total_bytes == 1024
        assert store.stats().count == 0

    def test_configure_store_swaps_and_returns_the_previous(self) -> None:
        original = reference_store.get_store()
        replacement = PointerStore(max_references=1)
        try:
            assert reference_store.configure_store(replacement) is original
            assert reference_store.get_store() is replacement
        finally:
            reference_store.configure_store(original)
