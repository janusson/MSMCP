"""Tests for the instrument-class defaults (audit F7).

Audit F7 found the experimental thresholds pinned as module literals.  These
tests hold three properties: the default class reproduces MSMCP's v1.0 values
exactly (so moving the constants changed no result), every default carries its
provenance, and a non-default class actually changes the threshold that is
applied *and* is recorded in the result that used it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from msmcp.instruments import (
    DEFAULT_INSTRUMENT_CLASS,
    INSTRUMENT_CLASS_NAMES,
    PROFILES,
    Tolerance,
    get_profile,
)
from msmcp.models import scoring
from msmcp.security import SecurityPolicy
from msmcp.tools import qc
from msmcp.tools.search import SearchRequest, _run_scan


# ---------------------------------------------------------------------------
# The profile table itself
# ---------------------------------------------------------------------------
class TestProfileTable:
    def test_default_class_is_generic_and_the_v1_0_values_are_unchanged(self) -> None:
        """The default profile must reproduce the literals F7 replaced.

        This is the invariant that makes the change safe: everything that does
        not name an instrument class sees exactly the pre-F7 numbers.
        """
        assert DEFAULT_INSTRUMENT_CLASS == "generic"
        profile = get_profile(None)
        assert profile is get_profile("generic")
        assert profile.precursor_tolerance.value == 5.0
        assert profile.precursor_tolerance.unit == "ppm"
        assert profile.ms2_tolerance_da == 0.02
        assert profile.diagnostic_ion_tolerance_da == 0.02
        assert profile.snr_low == 5.0
        assert profile.snr_high == 20.0
        assert profile.fdr_threshold == 0.05
        assert profile.small_library_threshold == 2000

    def test_scoring_default_comes_from_the_profile(self) -> None:
        """The classical tolerance default is the generic profile's window."""
        assert get_profile(None).ms2_tolerance_da == scoring.DEFAULT_TOLERANCE

    def test_every_class_carries_non_empty_provenance(self) -> None:
        for name in INSTRUMENT_CLASS_NAMES:
            profile = PROFILES[name]
            assert profile.instrument_class == name
            assert profile.provenance.strip(), f"{name} has no provenance"
            # A profile must be JSON-safe, because it is embedded in provenance.
            json.dumps(profile.to_dict())

    def test_accurate_mass_classes_are_tighter_than_unit_resolution_ones(self) -> None:
        """The classes must actually differ, or the table is decoration."""
        orbitrap = get_profile("orbitrap")
        ion_trap = get_profile("ion_trap")
        assert orbitrap.ms2_tolerance_da < get_profile(None).ms2_tolerance_da
        assert ion_trap.ms2_tolerance_da > get_profile(None).ms2_tolerance_da
        # ppm where the instrument resolves mass, Da where it does not.
        assert orbitrap.precursor_tolerance.unit == "ppm"
        assert ion_trap.precursor_tolerance.unit == "Da"

    def test_unknown_class_raises_and_known_classes_are_listed(self) -> None:
        with pytest.raises(ValueError, match="unknown instrument class"):
            get_profile("banana")

    def test_profiles_mapping_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            PROFILES["generic"] = PROFILES["tof"]  # type: ignore[index]

    @pytest.mark.parametrize(
        ("value", "unit"),
        [(0.0, "ppm"), (-1.0, "Da")],
    )
    def test_tolerance_rejects_non_positive(self, value: float, unit: str) -> None:
        with pytest.raises(ValueError, match="tolerance must be positive"):
            Tolerance(value, unit)  # type: ignore[arg-type]

    def test_tolerance_rejects_an_unknown_unit(self) -> None:
        with pytest.raises(ValueError, match="unknown tolerance unit"):
            Tolerance(1.0, "amu")  # type: ignore[arg-type]

    def test_tolerance_describe_keeps_one_decimal_where_it_matters(self) -> None:
        assert Tolerance(5.0, "ppm").describe() == "5.0 ppm"
        assert Tolerance(0.7, "Da").describe() == "0.7 Da"


# ---------------------------------------------------------------------------
# validate_precursor — the precursor gate is the class's, in the class's unit
# ---------------------------------------------------------------------------
class TestValidatePrecursorInstrumentClass:
    def test_default_reproduces_the_v1_0_ppm_gate(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = sim_tools["validate_precursor"](
            theoretical_mass=194.0804, experimental_mass=194.0807
        )
        assert "≤ 5.0 ppm threshold" in out
        assert "Instrument class:  generic" in out

    def test_a_ppm_gate_that_passes_fails_a_unit_resolution_da_gate(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        """5 ppm at m/z 1e6 is 5 Da — far outside a 0.5 Da ion-trap window."""
        generic = sim_tools["validate_precursor"](
            theoretical_mass=1_000_000.0,
            experimental_mass=1_000_005.0,
            instrument_class="generic",
        )
        ion_trap = sim_tools["validate_precursor"](
            theoretical_mass=1_000_000.0,
            experimental_mass=1_000_005.0,
            instrument_class="ion_trap",
        )
        assert generic.startswith("VALIDATION PASSED")
        assert ion_trap.startswith("VALIDATION REJECTED")
        assert "0.5 Da acceptance threshold" in ion_trap

    def test_a_da_window_that_passes_fails_the_ppm_gate(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        """0.04 Da is 400 ppm at m/z 100 — inside a 0.5 Da window, outside 5 ppm."""
        ppm = sim_tools["validate_precursor"](
            theoretical_mass=100.0, experimental_mass=100.04, instrument_class="generic"
        )
        da = sim_tools["validate_precursor"](
            theoretical_mass=100.0, experimental_mass=100.04, instrument_class="ion_trap"
        )
        assert ppm.startswith("VALIDATION REJECTED")
        assert da.startswith("VALIDATION PASSED")
        assert "Mass error:         0.04 Da" in da
        assert "Instrument class:  ion_trap" in da

    def test_unknown_class_is_rejected_by_the_schema(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            sim_tools["validate_precursor"](
                theoretical_mass=100.0,
                experimental_mass=100.0,
                instrument_class="banana",
            )


# ---------------------------------------------------------------------------
# compute_cosine — the m/z window is the class's default
# ---------------------------------------------------------------------------
class TestComputeCosineInstrumentClass:
    def test_the_class_window_changes_the_match(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        query = [[100.0, 1.0], [200.0, 1.0]]
        reference = [[100.015, 1.0], [200.0, 1.0]]  # 0.015 Da shift on one peak

        generic = sim_tools["compute_cosine"](
            query_peaks=query, reference_peaks=reference
        )
        orbitrap = sim_tools["compute_cosine"](
            query_peaks=query, reference_peaks=reference, instrument_class="orbitrap"
        )

        # 0.015 Da is inside the 0.02 Da generic window but outside the 0.01 Da
        # orbitrap window.
        assert "Cosine Similarity: **1.0000**" in generic
        assert "instrument class 'generic'" in generic
        assert "Cosine Similarity: **1.0000**" not in orbitrap
        assert "instrument class 'orbitrap'" in orbitrap
        assert "±0.010 Da" in orbitrap

    def test_an_explicit_tolerance_still_wins(
        self, sim_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = sim_tools["compute_cosine"](
            query_peaks=[[100.0, 1.0]],
            reference_peaks=[[100.0, 1.0]],
            ms2_tolerance=0.5,
            instrument_class="orbitrap",
        )
        assert "explicit ms2_tolerance" in out
        assert "±0.500 Da" in out


# ---------------------------------------------------------------------------
# generate_qc_summary — the diagnostic window and SNR bands are the class's
# ---------------------------------------------------------------------------
class TestQCInstrumentClass:
    def test_default_reproduces_the_v1_0_labels(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = qc_tools["generate_qc_summary"](str(valid_mzml))
        assert "within ±0.02 Da of the theoretical m/z" in out
        assert "Spectra < 5 SNR" in out
        assert "Spectra > 20 SNR" in out
        assert "**Instrument class:** generic" in out

    def test_a_unit_resolution_class_widens_the_diagnostic_window(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = qc_tools["generate_qc_summary"](
            str(valid_mzml), instrument_class="triple_quad"
        )
        assert "within ±0.7 Da of the theoretical m/z" in out
        assert "**Instrument class:** triple_quad" in out


# ---------------------------------------------------------------------------
# search_library — thresholds in the report *and* in the provenance record
# ---------------------------------------------------------------------------
def test_search_report_and_provenance_record_the_instrument_class() -> None:
    outcome = _run_scan(
        SearchRequest(
            database_file="library/f7-test.db",
            experimental_peaks=((110.0713, 40.0), (120.0808, 100.0), (136.0757, 60.0)),
            experimental_file="experimental/f7.mzML",
            instrument_class="orbitrap",
        )
    )
    report = outcome.report
    assert "Instrument class: orbitrap" in report
    assert "±0.01 Da" in report
    assert "Scoring method: classical (greedy peak matching, ±0.01 Da)" in report

    parameters = outcome.provenance.to_dict()["parameters"]
    assert parameters["instrument_class"] == "orbitrap"
    assert parameters["ms2_tolerance_da"] == 0.01
    assert parameters["fdr_threshold"] == 0.05
    assert parameters["small_library_threshold"] == 2000
    assert parameters["instrument_defaults_provenance"]
