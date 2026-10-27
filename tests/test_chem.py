"""Tests for the cheminformatics tools: adduct shifts & isotope annotation.

Covers ``predict_adduct_offset`` (valid and invalid adducts) and
``annotate_isotopes`` (formula parsing and isotope math against known
exact masses).
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from msmcp.tools.chem import (
    _ADDUCT_DB,
    NEUTRON_MASS,
    _isotope_pattern,
    _parse_formula,
    _smiles_to_formula,
)

# Independent reference data for the adduct convention used in this test.
# Neutral atomic masses (Da), from NIST/CIAAW evaluated isotopic masses, and
# CODATA electron mass. An ion mass is the neutral-atom composition minus
# net_charge * electron_mass. Ionization/binding energies are neglected.
# These constants are deliberately test-local: production constants and
# _ADDUCT_DB are not the numerical oracle.
REFERENCE_ATOM_MASSES = {
    "H": 1.00782503223,
    "C": 12.00000000000,
    "N": 14.00307400443,
    "O": 15.99491461957,
    "Na": 22.98976928200,
    "K": 38.96370648640,
    "Cl": 34.96885268200,
}
REFERENCE_ELECTRON_MASS = 0.000548579909065
REFERENCE_ADDUCTS = {
    "[M+H]+": ({"H": 1}, 1),
    "[M+Na]+": ({"Na": 1}, 1),
    "[M+K]+": ({"K": 1}, 1),
    "[M+NH4]+": ({"N": 1, "H": 4}, 1),
    "[M+H-H2O]+": ({"H": -1, "O": -1}, 1),
    "[M+2H]2+": ({"H": 2}, 2),
    "[M+3H]3+": ({"H": 3}, 3),
    "[M+2Na-H]+": ({"Na": 2, "H": -1}, 1),
    "[M-H]-": ({"H": -1}, -1),
    "[M+Cl]-": ({"Cl": 1}, -1),
    "[M+HCOO]-": ({"C": 1, "H": 1, "O": 2}, -1),
    "[M+CH3COO]-": ({"C": 2, "H": 3, "O": 2}, -1),
    "[M-H2O-H]-": ({"H": -3, "O": -1}, -1),
    "[M+Na-2H]-": ({"Na": 1, "H": -2}, -1),
}


def reference_adduct_shift(adduct: str) -> tuple[float, int]:
    """Independent neutral-atom/electron mass convention for an adduct."""
    composition, charge = REFERENCE_ADDUCTS[adduct]
    neutral_atom_delta = sum(
        REFERENCE_ATOM_MASSES[element] * count for element, count in composition.items()
    )
    return neutral_atom_delta - charge * REFERENCE_ELECTRON_MASS, charge


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
            ("[M+2H]2+", "+2.014553", "+2", "positive", "(M + 2.014553) / |2| Da"),
            ("[M+3H]3+", "+3.021829", "+3", "positive", "(M + 3.021829) / |3| Da"),
            ("[M+2Na-H]+", "+44.971165", "+1", "positive", "M + 44.971165 Da"),
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
            ("[M+Na-2H]-", "+20.974668", "-1", "negative", "(M + 20.974668) / |-1| Da"),
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

    def test_every_adduct_matches_independent_neutral_atom_ion_mass_reference(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        """Audit every canonical shift without using production constants.

        For a neutral molecule mass M, the ion mass is computed as the
        neutral-atom delta minus net charge times the electron mass. Expected
        m/z then uses the absolute charge magnitude. The multiple-charge
        references pin the cases that previously subtracted electrons twice.
        """
        neutral_mass = 500.0
        for adduct, (_, charge) in REFERENCE_ADDUCTS.items():
            expected_shift, expected_charge = reference_adduct_shift(adduct)
            assert expected_charge == charge
            assert _ADDUCT_DB[adduct]["charge"] == charge
            assert _ADDUCT_DB[adduct]["shift"] == pytest.approx(
                expected_shift, abs=5e-9
            ), adduct

            out = chem_tools["predict_adduct_offset"](adduct_string=adduct)
            assert f"Exact mass shift (Δ): {expected_shift:+.6f} Da" in out
            assert f"|{abs(charge)}|" in out or abs(charge) == 1

        # Independent, literature-scale neutral-mass examples for the
        # multiply charged ions (M = 500 Da), using |z| rather than signed z.
        assert (
            neutral_mass + reference_adduct_shift("[M+2H]2+")[0]
        ) / 2 == pytest.approx(251.007276452, abs=1e-9)
        assert (
            neutral_mass + reference_adduct_shift("[M+3H]3+")[0]
        ) / 3 == pytest.approx(167.673943119, abs=1e-9)

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

    @pytest.mark.parametrize(
        "malformed",
        [
            "C6H12O6!",
            "C6H12O6?",
            "C6-H12O6",
            "C6 H12O6",
            "6C6H12O6",
            "C6.5H12O6",
            "C6H12O6$Na",
            "C0H2",
            "C2H0",
            "C-1H2",
        ],
    )
    def test_malformed_or_nonpositive_formula_counts_are_rejected(
        self, malformed: str
    ) -> None:
        with pytest.raises(ValueError):
            _parse_formula(malformed)

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

    def test_isotopologue_shifts_are_substitution_differences(self) -> None:
        """M+1 is shifted by ¹³C - ¹²C, not by the neutron mass.

        A +1 isotopologue substitutes one isotope into the molecule, so the mass
        shift is the isotope's mass minus the *monoisotopic* mass of that
        element (¹³C - ¹²C = +1.003355 Da).  The neutron mass (1.008665 Da) is
        the difference between the isotope and its constituent particles, not
        between two neutral atoms; using it put M+1 about 5.3 mDa (~18 ppm at
        m/z 300) too high, i.e. outside the ppm tolerance this project works to.
        """
        pattern = _isotope_pattern(_parse_formula("C6H12O6"))
        mono = pattern[0][0]
        # The unresolved M+1 peak is the intensity-weighted mean of its fine
        # structure: ¹³C (94%), ¹⁷O (3%), ²H (2%) → +1.0034424 Da.
        shift = pattern[1][0] - mono
        assert shift == pytest.approx(1.0034424, abs=1e-6)
        # It must sit between the pure ¹³C (1.0033548) and ²H (1.0062767) shifts.
        assert 1.0033548 < shift < 1.0062767
        # The old neutron-based value must stay excluded.
        assert abs(pattern[1][0] - (mono + NEUTRON_MASS)) > 5.0e-3

    def test_carbon_m1_abundance(self) -> None:
        pattern = _isotope_pattern(_parse_formula("C"))
        assert pattern[1][1] == pytest.approx(0.0107 / 0.9893, rel=1e-6)

    def test_multi_carbon_matches_the_binomial_closed_form(self) -> None:
        """C-only M+k equals the exact binomial ratio, an independent reference.

        With only ¹³C contributing to +1 (and no heavier carbon isotope),
        ``M+k / M = C(n, k) * (a₁₃/a₁₂)**k`` for an ``n``-carbon formula.  This
        is the closed-form multinomial result, independent of the convolution
        the implementation uses, and pins the multi-carbon M+2 that the earlier
        ``p₁²/2`` pool approximation got wrong.
        """
        n_carbon = 8
        ratio = 0.0107 / 0.9893
        pattern = _isotope_pattern(_parse_formula("C8"), max_isotopologue=4)
        assert pattern[0][1] == 1.0
        for k in (1, 2, 3):
            expected = math.comb(n_carbon, k) * ratio**k
            assert pattern[k][1] == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize(
        ("formula", "m1", "m2"),
        [
            ("H2O", 0.000611, 0.002055),
            ("C6H12O6", 0.068560, 0.014329),
            ("C8H10N4O2", 0.103212, 0.008941),
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

    def test_dichlorine_carries_an_m4_peak(self) -> None:
        """Cl₂ is the case the Δn ≤ 2 shortcut could not express.

        Two ³⁷Cl atoms put abundance at M+4; the exact multinomial gives the
        textbook ≈ 9:6:1 (M : M+2 : M+4) pattern.
        """
        pattern = _isotope_pattern(_parse_formula("Cl2"), max_isotopologue=5)
        assert len(pattern) == 5
        ratio = 0.2422 / 0.7578
        assert pattern[1][1] == pytest.approx(0.0, abs=1e-12)  # no M+1
        assert pattern[2][1] == pytest.approx(2 * ratio, rel=1e-6)
        assert pattern[3][1] == pytest.approx(0.0, abs=1e-12)  # no M+3
        assert pattern[4][1] == pytest.approx(ratio**2, rel=1e-6)

    def test_dibromine_is_near_one_two_one(self) -> None:
        """Br₂ has no monoisotopic-dominant isotope: M, M+2, M+4 ≈ 1 : 2 : 1."""
        pattern = _isotope_pattern(_parse_formula("Br2"), max_isotopologue=5)
        assert pattern[2][1] == pytest.approx(1.9456, abs=1e-3)
        assert pattern[4][1] == pytest.approx(0.9463, abs=1e-3)

    def test_m2_is_exact_not_a_pool_approximation(self) -> None:
        """M+2 must be the exact multinomial coefficient, not ``p1²/2``.

        With a single carbon the two-¹³C pathway is impossible, so methane's
        M+2 (one ¹³C + one ²H, or two ²H) is ~5e-6 — whereas the earlier
        ``p1²/2`` pool approximation returned ~9.6e-4, two orders of magnitude
        too high.  This pins the exact expansion.
        """
        pattern = _isotope_pattern(_parse_formula("CH4"))
        assert 0.0 < pattern[2][1] < 1.0e-5

    def test_isotopologue_count_is_honoured(self) -> None:
        """``max_isotopologue`` sets the number of rows, not just a label."""
        assert len(_isotope_pattern(_parse_formula("C8H10N4O2"), 5)) == 5
        assert len(_isotope_pattern(_parse_formula("C8H10N4O2"), 1)) == 1
        with pytest.raises(ValueError, match="at least 1"):
            _isotope_pattern(_parse_formula("C8H10N4O2"), 0)


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
        assert "| M+1         |             181.0668 |             0.0686 |" in out
        assert "| M+2         |             182.0680 |             0.0143 |" in out

    def test_isotopologues_parameter_reveals_the_halogen_m4(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        """Default is three rows; raising it exposes a halogen's M+4 peak."""
        default = chem_tools["annotate_isotopes"](identifier="CHCl3")
        assert "M+4" not in default
        extended = chem_tools["annotate_isotopes"](identifier="CHCl3", isotopologues=5)
        assert "| M+4" in extended
        # Isotopologue counts outside the advertised range are rejected.
        with pytest.raises(ValidationError):
            chem_tools["annotate_isotopes"](identifier="CHCl3", isotopologues=11)

    def test_explicit_formula_works_without_rdkit(
        self,
        chem_tools: dict[str, Callable[..., str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Formula parsing is core behavior and must not depend on RDKit."""
        import sys

        monkeypatch.setitem(sys.modules, "rdkit", None)
        out = chem_tools["annotate_isotopes"](identifier="C6H12O6")
        assert "## Isotope Pattern: C6H12O6" in out
        assert "Monoisotopic mass: **180.0634 Da**" in out

    def test_smiles_conversion_fails_closed_without_rdkit(
        self,
        chem_tools: dict[str, Callable[..., str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unknown SMILES must never pass through as formula text."""
        import sys

        monkeypatch.setitem(sys.modules, "rdkit", None)
        assert _smiles_to_formula("CO") is None
        assert _smiles_to_formula("CCO") is None
        error = chem_tools["annotate_isotopes"](identifier="CO", is_smiles=True)
        assert error.startswith("ERROR:")
        assert "RDKit" in error
        assert "C6H12O6" in chem_tools["annotate_isotopes"](identifier="C6H12O6")

    def test_rdkit_smiles_conversion_when_available(self) -> None:
        pytest.importorskip("rdkit")
        assert _smiles_to_formula("CO") == "CH4O"
        assert _smiles_to_formula("CCO") == "C2H6O"
        assert _smiles_to_formula("this is not SMILES") is None

    def test_unknown_element_reports_error(
        self, chem_tools: dict[str, Callable[..., str]]
    ) -> None:
        out = chem_tools["annotate_isotopes"](identifier="CaCl2")
        assert out.startswith("ERROR: Unknown element 'Ca' in formula 'CaCl2'.")
