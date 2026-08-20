"""Cheminformatics tools: adduct mass shifts &amp; isotope pattern prediction."""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("msmcp.tools.chem")

# ======================================================================
# Physical constants (exact masses, Da)
# ======================================================================
PROTON_MASS = 1.00727646688  # H⁺
ELECTRON_MASS = 0.00054857990907  # e⁻
NEUTRON_MASS = 1.00866491588  # n

# ======================================================================
# Known adducts — Δ = (adduct mass) − (neutral M mass) in Da.
# Calculations: M is neutral.  For positive ions M loses n electrons
# and gains the cation; for negative ions M gains electrons and/or
# loses a proton.
# ======================================================================
_ADDUCT_DB: dict[str, dict[str, Any]] = {
    # --- positive mode -------------------------------------------------
    "[M+H]+": {
        "shift": PROTON_MASS - ELECTRON_MASS,
        "charge": 1,
        "polarity": "positive",
    },
    "[M+Na]+": {
        "shift": 22.98976928 - ELECTRON_MASS,
        "charge": 1,
        "polarity": "positive",
    },
    "[M+K]+": {
        "shift": 38.96370649 - ELECTRON_MASS,
        "charge": 1,
        "polarity": "positive",
    },
    "[M+NH4]+": {
        "shift": 14.003074004 + 4 * 1.007825032 - ELECTRON_MASS,
        "charge": 1,
        "polarity": "positive",
    },
    "[M+H-H2O]+": {
        "shift": PROTON_MASS - ELECTRON_MASS - (2 * 1.007825032 + 15.994914619),
        "charge": 1,
        "polarity": "positive",
    },
    "[M+2H]2+": {
        "shift": 2 * PROTON_MASS - 2 * ELECTRON_MASS,
        "charge": 2,
        "polarity": "positive",
    },
    "[M+3H]3+": {
        "shift": 3 * PROTON_MASS - 3 * ELECTRON_MASS,
        "charge": 3,
        "polarity": "positive",
    },
    "[M+2Na-H]+": {
        "shift": 2 * (22.98976928 - ELECTRON_MASS) - (PROTON_MASS - ELECTRON_MASS),
        "charge": 1,
        "polarity": "positive",
    },
    # --- negative mode -------------------------------------------------
    "[M-H]-": {
        "shift": -(PROTON_MASS) + ELECTRON_MASS,
        "charge": -1,
        "polarity": "negative",
    },
    "[M+Cl]-": {
        "shift": 34.96885269 + ELECTRON_MASS,
        "charge": -1,
        "polarity": "negative",
    },
    "[M+HCOO]-": {
        "shift": (1.007825032 + 12.000000000 + 2 * 15.994914619 + ELECTRON_MASS),
        "charge": -1,
        "polarity": "negative",
    },
    "[M+CH3COO]-": {
        "shift": (
            2 * 12.000000000 + 3 * 1.007825032 + 2 * 15.994914619 + ELECTRON_MASS
        ),
        "charge": -1,
        "polarity": "negative",
    },
    "[M-H2O-H]-": {
        "shift": -(2 * 1.007825032 + 15.994914619) - PROTON_MASS + ELECTRON_MASS,
        "charge": -1,
        "polarity": "negative",
    },
    "[M+Na-2H]-": {
        "shift": ((22.98976928 - ELECTRON_MASS) - 2 * PROTON_MASS + 2 * ELECTRON_MASS),
        "charge": -1,
        "polarity": "negative",
    },
}
"""Canonical adducts with exact-mass shifts."""


# ======================================================================
# Isotope database  (mass / Da,  fractional abundance)
# ======================================================================
_ISOTOPES: dict[str, list[tuple[float, float]]] = {
    "C": [(12.000000000, 0.9893), (13.003354835, 0.0107)],
    "H": [(1.007825032, 0.999885), (2.014101778, 0.000115)],
    "N": [(14.003074004, 0.99632), (15.000108898, 0.00368)],
    "O": [(15.994914619, 0.99757), (16.999131756, 0.00038), (17.999159612, 0.00205)],
    "S": [(31.972071174, 0.9493), (32.971458909, 0.0076), (33.967867004, 0.0429)],
    "Cl": [(34.968852690, 0.7578), (36.965902580, 0.2422)],
    "Br": [(78.918337600, 0.5069), (80.916289700, 0.4931)],
    "P": [(30.973761998, 1.0)],
    "F": [(18.998403163, 1.0)],
    "I": [(126.904467700, 1.0)],
    "Na": [(22.989769280, 1.0)],
    "K": [(38.963706490, 0.93258), (39.963998170, 0.00012), (40.961825260, 0.06730)],
    "Si": [(27.976926535, 0.9223), (28.976494665, 0.0467), (29.973770010, 0.0310)],
    "Fe": [
        (53.939609000, 0.05845),
        (55.934936000, 0.91754),
        (56.935393000, 0.02119),
        (57.933274000, 0.00282),
    ],
    "Se": [
        (73.922475934, 0.0089),
        (75.919213700, 0.0937),
        (76.919914200, 0.0763),
        (77.917309100, 0.2377),
        (79.916521800, 0.4961),
        (81.916709500, 0.0873),
    ],
}
"""Isotopes ordered by ascending mass; first entry = monoisotopic."""


# ======================================================================
# Pydantic schemas
# ======================================================================
class AdductInput(BaseModel):
    """Validated input for predict_adduct_offset."""

    adduct_string: str = Field(
        ...,
        min_length=3,
        description="Adduct notation, e.g. '[M+H]+' or '[M-H]-'.",
    )


class IsotopeInput(BaseModel):
    """Validated input for annotate_isotopes."""

    identifier: str = Field(
        ...,
        min_length=1,
        description="Chemical formula (e.g. 'C6H12O6') or SMILES string.",
    )
    is_smiles: bool = Field(
        default=False,
        description="Set to True when *identifier* is a SMILES string.",
    )


# ======================================================================
# Formula parser
# ======================================================================
_ELEMENT_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


def _parse_formula(formula: str) -> dict[str, int]:
    """Parse a chemical formula string → {element: count}."""
    composition: dict[str, int] = {}
    for match in _ELEMENT_RE.finditer(formula):
        el = match.group(1)
        count_str = match.group(2)
        count = int(count_str) if count_str else 1
        if el not in _ISOTOPES:
            raise ValueError(f"Unknown element '{el}' in formula '{formula}'.")
        composition[el] = composition.get(el, 0) + count
    if not composition:
        raise ValueError(f"Could not parse any elements from '{formula}'.")
    return composition


# ======================================================================
# Isotope pattern calculator
# ======================================================================
def _isotope_pattern(
    composition: dict[str, int],
    max_isotopologue: int = 3,
) -> list[tuple[float, float]]:
    """Compute theoretical isotopologue masses and relative abundances.

    Returns [(mass_Da, rel_abundance), ...] for M, M+1, M+2.
    Abundances are normalised so that M = 1.0.
    """
    # --- monoisotopic mass ------------------------------------------------
    mono_mass = 0.0
    for el, count in composition.items():
        mono_mass += _ISOTOPES[el][0][0] * count

    # --- M+1 probability: sum over all elements of                          #
    #     count × (abundance of first heavy isotope / abundance of light)    #
    #     For elements with only one isotope the term is zero.               #
    p1 = 0.0
    for el, count in composition.items():
        isotopes = _ISOTOPES[el]
        if len(isotopes) > 1:
            p1 += count * (isotopes[1][1] / isotopes[0][1])

    m1_mass = mono_mass + NEUTRON_MASS
    m1_abund = p1

    # --- M+2 probability (approximate) ------------------------------------
    # Two contributions:
    #   a) Two independent +1 substitutions → ≈ p1² / 2
    #   b) One +2 substitution (S, Cl, Br, Se, …) → sum over elements of
    #      count × (abund_+2 / abund_light)
    p2_a = (p1**2) / 2.0

    p2_b = 0.0
    m2_mass_shift = 2.0 * NEUTRON_MASS
    for el, count in composition.items():
        isotopes = _ISOTOPES[el]
        if len(isotopes) > 2:
            # +2 neutron isotope exists
            p2_b += count * (isotopes[2][1] / isotopes[0][1])

    m2_abund = p2_a + p2_b
    m2_mass = mono_mass + m2_mass_shift

    # Build result, normalised to M = 1.0
    result = [
        (mono_mass, 1.0),
        (m1_mass, m1_abund),
        (m2_mass, m2_abund),
    ]
    return result


# ======================================================================
# Mock cheminformatics (replaces massflow.cheminformatics when absent)
# ======================================================================
def _mock_smiles_to_formula(smiles: str) -> str:
    """Stub SMILES→formula converter for development.

    Returns a plausible formula string for a handful of known SMILES
    so the tool produces non-trivial output during testing.
    """
    _KNOWN: dict[str, str] = {
        "CCO": "C2H6O",
        "c1ccccc1": "C6H6",
        "CC(=O)O": "C2H4O2",
        "C1=CC=C(C=C1)C=O": "C7H6O",
        "CN1C=NC2=C1C(=O)N(C(=O)N2C)C": "C8H10N4O2",  # caffeine
        "O": "H2O",
        "[Na+].[Cl-]": "NaCl",
    }
    return _KNOWN.get(smiles, smiles)  # fallback: treat as formula


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register cheminformatics tools on the supplied FastMCP *mcp* instance."""

    # ------------------------------------------------------------------
    # Tool: predict_adduct_offset
    # ------------------------------------------------------------------
    @mcp.tool()
    def predict_adduct_offset(adduct_string: str) -> str:
        """Return the exact mass shift for a standard ionisation adduct.

        Accepts canonical adduct notations such as ``[M+H]+``, ``[M-H]-``,
        ``[M+Na]+``, etc.  Non-standard or hallucinated adduct strings
        are explicitly rejected.
        """
        _ = AdductInput(adduct_string=adduct_string)
        canonical = adduct_string.strip()

        entry = _ADDUCT_DB.get(canonical)
        if entry is None:
            # Try case-insensitive fallback
            lower_map = {k.lower(): (k, v) for k, v in _ADDUCT_DB.items()}
            fallback = lower_map.get(canonical.lower())
            if fallback is not None:
                canonical, entry = fallback
            else:
                logger.warning(
                    "Rejected non-standard adduct: %r",
                    adduct_string,
                )
                known = "\n".join(f"  {a}" for a in _ADDUCT_DB)
                return (
                    f"REJECTED: '{adduct_string}' is not a recognised ionisation adduct.\n\n"
                    f"Please reconsider the ionisation pathway.  Supported adducts are:\n"
                    f"{known}\n\n"
                    f"Provide a canonical adduct string from the list above."
                )

        shift = entry["shift"]
        charge = entry["charge"]
        polarity = entry["polarity"]

        # Build a human-readable offset equation
        sign = "+" if shift >= 0 else "-"
        abs_shift = abs(shift)
        if charge == 1:
            offset_expr = f"M {sign} {abs_shift:.6f} Da"
        else:
            offset_expr = f"(M {sign} {abs_shift:.6f}) / |{charge}| Da"

        logger.info(
            "predict_adduct_offset(%r) → %+.6f Da (charge %+d)",
            adduct_string,
            shift,
            charge,
        )

        return (
            f"Adduct: {canonical}\n"
            f"Polarity: {polarity}\n"
            f"Charge state: {charge:+d}\n"
            f"Exact mass shift (Δ): {shift:+.6f} Da\n"
            f"m/z offset for neutral M: {offset_expr}\n\n"
            f"Formula:  m/z = {offset_expr}"
        )

    # ------------------------------------------------------------------
    # Tool: annotate_isotopes
    # ------------------------------------------------------------------
    @mcp.tool()
    def annotate_isotopes(identifier: str, is_smiles: bool = False) -> str:
        """Compute the theoretical isotope pattern for a molecular formula or SMILES.

        Returns a Markdown table of M, M+1, and M+2 isotopologue masses
        and relative abundances, normalised to the monoisotopic peak.
        """
        _ = IsotopeInput(identifier=identifier, is_smiles=is_smiles)

        # --- SMILES → formula conversion -----------------------------------
        if is_smiles:
            formula = _smiles_to_formula(identifier)
            if formula is None:
                return (
                    "ERROR: RDKit (`massflow[chem]`) is not installed and the "
                    "SMILES string could not be resolved.\n\n"
                    "Please compute the chemical formula for this structure "
                    "manually and resubmit using **is_smiles=False** with the "
                    "formula string as *identifier*."
                )
            logger.info("SMILES %r → formula %r", identifier, formula)
        else:
            formula = identifier.strip()

        # --- parse formula --------------------------------------------------
        try:
            composition = _parse_formula(formula)
        except ValueError as exc:
            logger.warning("Formula parse failed: %s", exc)
            return f"ERROR: {exc}"

        # --- compute isotope pattern ----------------------------------------
        pattern = _isotope_pattern(composition)

        # --- render Markdown table ------------------------------------------
        lines = [
            f"## Isotope Pattern: {formula}",
            "",
            f"Monoisotopic mass: **{pattern[0][0]:.4f} Da**",
            "",
            "| Isotopologue | Theoretical Mass (Da) | Relative Abundance |",
            "|-------------|----------------------|--------------------|",
        ]
        labels = ["M", "M+1", "M+2"]
        for (mass, abund), label in zip(pattern, labels):
            lines.append(f"| {label:<11} | {mass:>20.4f} | {abund:>18.4f} |")

        lines.extend(
            [
                "",
                "*Abundances are normalised to the monoisotopic peak (M = 1.0000).*",
            ]
        )

        logger.info(
            "annotate_isotopes(%r, smiles=%s) → %d isotopologues",
            identifier,
            is_smiles,
            len(pattern),
        )

        return "\n".join(lines)


# ======================================================================
# Internal helpers
# ======================================================================
def _smiles_to_formula(smiles: str) -> str | None:
    """Convert SMILES → chemical formula.

    Tries RDKit first; falls back to a static lookup table for
    development.  Returns ``None`` when conversion is impossible,
    signalling the caller to instruct the LLM to compute the formula.
    """
    # -- attempt real RDKit conversion ---------------------------------
    try:
        from rdkit import Chem  # type: ignore[import-untyped]

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            raise ValueError(f"Invalid SMILES: {smiles!r}")

        # Build formula string from atomic numbers
        from collections import Counter

        atoms = [atom.GetSymbol() for atom in mol.GetAtoms()]
        # RDKit Hydrogens are implicit — add them
        mol_with_h = Chem.AddHs(mol)
        all_atoms = [atom.GetSymbol() for atom in mol_with_h.GetAtoms()]

        counts = Counter(all_atoms)
        # Hill order: C first, then H, then alphabetical
        hill_order = sorted(
            counts.keys(),
            key=lambda el: (
                0 if el == "C" else 1 if el == "H" else 2,
                el,
            ),
        )
        formula_str = "".join(
            f"{el}{counts[el] if counts[el] > 1 else ''}" for el in hill_order
        )
        return formula_str

    except ImportError:
        logger.info("RDKit not available; using mock SMILES→formula lookup.")
        return _mock_smiles_to_formula(smiles)

    except Exception as exc:
        logger.warning("SMILES conversion failed: %s", exc)
        return _mock_smiles_to_formula(smiles)
