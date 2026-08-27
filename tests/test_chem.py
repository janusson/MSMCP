"""Tests for the cheminformatics tools: adduct shifts & isotope annotation.

Covers ``predict_adduct_offset`` (valid and invalid adducts) and
``annotate_isotopes`` (formula parsing and isotope math against known
exact masses).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from msmcp.tools.chem import (
    _ADDUCT_DB,
    NEUTRON_MASS,
    _isotope_pattern,
    _parse_formula,
)


# ---------------------------------------------------------------------------
# predict_adduct_offset — valid adducts
# ---------------------------------------------------------------------------
class TestPredictAdductOffsetValid:
    """Known adducts must return the exact mass shift with correct metadata."""

    @pytest.mark.parametrize(
        ("adduct", "shift", "charge", "polarity", "offset"),
        [
            ("[M+H]+", "+1.007276", "+1", "positive", "M + 1.007276 Da"),
            ("[M+Na]+", "+22.989221", "+1", "positive", "M + 22.989221 Da"),
            ("[M+K]+", "+38.963158", "+1", "positive", "M + 38.963158 Da"),
            ("[M+NH4]+", "+18.033826", "+1", "positive", "M + 18.033826 Da"),
            ("[M+H-H2O]+", "-17.003288", "+1", "positive", "M - 17.003288 Da"),
            ("[M+2H]2+", "+2.013456", "+2", "positive", "(M + 2.013456) / |2| Da"),
            ("[M+3H]3+", "+3.020184", "+3", "positive", "(M + 3.020184) / |3| Da"),
            ("[M+2Na-H]+", "+44.971714", "+1", "positive", "M + 44.971714 Da"),
            ("[M-H]-", "-1.007276", "-1", "negative", "(M - 1.007276) / |-1| Da"),
            ("[M+Cl]-", "+34.969401", "-1", "negative", "(M + 34.969401) / |-1| Da"),
            ("[M+HCOO]-", "+44.998203", "-1", "negative", "(M + 44.998203) / |-1| Da"),
            (
                "[M+CH3COO]-",
                "+59.013853",
                "-1",
                "negative",
                "(M + 59.013853) / |-1| Da",
            ),
            ("[M-H2O-H]-", "-19.017841", "-1", "negative", "(M - 19.017841) / |-1| Da"),
            ("[M+Na-2H]-", "+20.975765", "-1", "negative", "(M + 20.975765) / |-1| Da"),
        ],
    )
    def test_known_adduct(
        self,
        chem_tools: dict[str, Callable[..., str]],
        adduct: str,
        shift: str,
        charge: str,
        polarity: str,
        offset: str,
    ) -> None:
        out = chem_tools["predict_adduct_offset"](adduct_string=adduct)
        assert out.startswith(f"Adduct: {adduct}\n")
        assert f"Polarity: {polarity}\n" in out
        assert f"Charge state: {charge}\n" in out
        assert f"Exact mass shift (Δ): {shift} Da\n" in out
        assert f"m/z offset for neutral M: {offset}\n" in out
        assert f"Formula:  m/z = {offset}" in out

    def test_every_database_entry_is_accepted(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        """Completeness guard: every canonical adduct in _ADDUCT_DB resolves."""
        for adduct in _ADDUCT_DB:
            out = chem_tools["predict_adduct_offset"](adduct_string=adduct)
            assert not out.startswith("REJECTED"), f"{adduct} was rejected"

    @pytest.mark.parametrize(
        ("given", "canonical"),
        [("[m+na]+", "[M+Na]+"), ("[m-h]-", "[M-H]-"), ("[M+nh4]+", "[M+NH4]+")],
    )
    def test_case_insensitive_fallback(
        self,
        chem_tools: dict[str, Callable[..., str]],
        given: str,
        canonical: str,
    ) -> None:
        out = chem_tools["predict_adduct_offset"](adduct_string=given)
        assert out.startswith(f"Adduct: {canonical}\n")


class TestPredictAdductOffsetInvalid:
    """Non-standard or hallucinated adducts must be explicitly rejected."""

    @pytest.mark.parametrize(
        "bad",
        ["[M+H2O]+", "[M+Li]+", "[M+2]+", "M+H", "[M]", "   "],
    )
    def test_unknown_adduct_is_rejected(
        self, chem_tools: dict[str, Callable[..., str]], bad: str
    ) -> None:
        out = chem_tools["predict_adduct_offset"](adduct_string=bad)
        assert out.startswith("REJECTED: ")
        assert f"'{bad}' is not a recognised ionisation adduct." in out
        assert "Supported adducts are:" in out

    @pytest.mark.parametrize("too_short", ["", "ab"])
    def test_too_short_adduct_raises_validation_error(
        self, chem_tools: dict[str, Callable[..., str]], too_short: str
    ) -> None:
        with pytest.raises(ValidationError):
            chem_tools["predict_adduct_offset"](adduct_string=too_short)


# ---------------------------------------------------------------------------
# Formula parser
# ---------------------------------------------------------------------------
class TestParseFormula:
    def test_simple_formula(self) -> None:
        assert _parse_formula("C6H12O6") == {"C": 6, "H": 12, "O": 6}

    def test_implied_count_defaults_to_one(self) -> None:
        assert _parse_formula("H2O") == {"H": 2, "O": 1}

    def test_repeated_element_accumulates(self) -> None:
        assert _parse_formula("CH3CH3") == {"C": 2, "H": 6}

    def test_multi_digit_count(self) -> None:
        assert _parse_formula("C60") == {"C": 60}

    def test_salt_formula(self) -> None:
        assert _parse_formula("NaCl") == {"Na": 1, "Cl": 1}

    def test_unknown_element_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown element 'Ca'"):
            _parse_formula("CaCl2")

    @pytest.mark.parametrize("empty", ["", "   ", "42"])
    def test_unparseable_input_raises(self, empty: str) -> None:
        with pytest.raises(ValueError, match="Could not parse any elements"):
            _parse_formula(empty)


# ---------------------------------------------------------------------------
# Isotope math — known exact masses & abundances
# ---------------------------------------------------------------------------
class TestIsotopePattern:
    """Validate monoisotopic masses against literature exact masses."""

    @pytest.mark.parametrize(
        ("formula", "expected_mono"),
        [
            ("CH4", 16.031300128),  # methane
            ("H2O", 18.010564683),  # water
            ("C2H6O", 46.041864811),  # ethanol
            ("C6H12O6", 180.063388098),  # glucose
            ("C7H6O", 106.041864811),  # benzaldehyde
            ("C8H10N4O2", 194.080375574),  # caffeine
        ],
    )
    def test_monoisotopic_mass_matches_literature(
        self, formula: str, expected_mono: float
    ) -> None:
        pattern = _isotope_pattern(_parse_formula(formula))
        assert pattern[0][0] == pytest.approx(expected_mono, abs=1e-6)
        assert pattern[0][1] == 1.0  # normalised to M

    def test_isotopologue_spacing_is_one_neutron(self) -> None:
        pattern = _isotope_pattern(_parse_formula("C6H12O6"))
        assert pattern[1][0] == pytest.approx(pattern[0][0] + NEUTRON_MASS, abs=1e-9)
        assert pattern[2][0] == pytest.approx(
            pattern[0][0] + 2 * NEUTRON_MASS, abs=1e-9
        )

    def test_carbon_m1_abundance(self) -> None:
        pattern = _isotope_pattern(_parse_formula("C"))
        assert pattern[1][1] == pytest.approx(0.0107 / 0.9893, rel=1e-6)

    @pytest.mark.parametrize(
        ("formula", "m1", "m2"),
        [
            ("H2O", 0.000611, 0.002055),
            ("C6H12O6", 0.068560, 0.014680),
            ("C8H10N4O2", 0.103212, 0.009436),
            # Halogens: the secondary isotope (³⁷Cl, ⁸¹Br) is an M+2
            # contributor, so M+1 is zero and M+2 carries the signal.
            ("Cl", 0.0, 0.319609),
            ("Br", 0.0, 0.972776),
        ],
    )
    def test_abundances_match_expected_arithmetic(
        self, formula: str, m1: float, m2: float
    ) -> None:
        pattern = _isotope_pattern(_parse_formula(formula))
        assert pattern[1][1] == pytest.approx(m1, abs=1e-5)
        assert pattern[2][1] == pytest.approx(m2, abs=1e-5)

    def test_chlorine_m2_reflects_37cl(self) -> None:
        """Single Cl: ³⁷Cl is an M+2 contributor, so M+1 is ~0%."""
        pattern = _isotope_pattern(_parse_formula("Cl"))
        # No Δn = 1 isotope exists for Cl → no M+1 peak.
        assert pattern[1][1] == pytest.approx(0.0, abs=1e-12)
        # ³⁷Cl / ³⁵Cl abundance ratio ≈ 0.3196 → M+2 ≈ 31.96%.
        assert pattern[2][1] == pytest.approx(0.2422 / 0.7578, rel=1e-6)


# ---------------------------------------------------------------------------
# annotate_isotopes — end-to-end
# ---------------------------------------------------------------------------
class TestAnnotateIsotopes:
    def test_glucose_end_to_end(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = chem_tools["annotate_isotopes"](identifier="C6H12O6")
        assert "## Isotope Pattern: C6H12O6" in out
        assert "Monoisotopic mass: **180.0634 Da**" in out
        assert "| M           |             180.0634 |             1.0000 |" in out
        assert "| M+1         |             181.0721 |             0.0686 |" in out
        assert "| M+2         |             182.0807 |             0.0147 |" in out

    def test_smiles_identifier_resolves_to_formula(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        # "CCO" (ethanol) → C2H6O via RDKit or the deterministic dev fallback.
        out = chem_tools["annotate_isotopes"](identifier="CCO", is_smiles=True)
        assert "## Isotope Pattern: C2H6O" in out
        assert "Monoisotopic mass: **46.0419 Da**" in out

    def test_unknown_element_reports_error(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = chem_tools["annotate_isotopes"](identifier="CaCl2")
        assert out.startswith("ERROR: Unknown element 'Ca' in formula 'CaCl2'.")
