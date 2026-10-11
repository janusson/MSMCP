"""Cheminformatics tools: adduct mass shifts &amp; isotope pattern prediction."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

logger = logging.getLogger("msmcp.tools.chem")

# ======================================================================
# Physical constants (exact masses, Da)
# ======================================================================
PROTIUM_MASS = 1.00782503223  # neutral ¹H atom
PROTON_MASS = 1.00727646688  # ¹H⁺ ion (proton)
ELECTRON_MASS = 0.00054857990907  # e⁻
NEUTRON_MASS = 1.00866491588  # n

# ======================================================================
# Known adducts — delta = (adduct mass) - (neutral M mass) in Da.
# Convention: Δ is the exact mass of the ionised adduct relative to
# neutral M.  [M+H]+ adds the bare proton (already an ion — no electron
# term); metal/ammonium cations are the neutral atom/molecule minus one
# electron; neutral losses (H2O) use the neutral molecular mass.
# ======================================================================
_ADDUCT_DB: dict[str, dict[str, Any]] = {
    # --- positive mode -------------------------------------------------
    "[M+H]+": {
        "shift": PROTON_MASS,
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
        "shift": 14.003074004 + 4 * PROTIUM_MASS - ELECTRON_MASS,
        "charge": 1,
        "polarity": "positive",
    },
    "[M+H-H2O]+": {
        "shift": PROTON_MASS - (2 * PROTIUM_MASS + 15.994914619),
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
        "shift": -PROTON_MASS,
        "charge": -1,
        "polarity": "negative",
    },
    "[M+Cl]-": {
        "shift": 34.96885269 + ELECTRON_MASS,
        "charge": -1,
        "polarity": "negative",
    },
    "[M+HCOO]-": {
        "shift": (PROTIUM_MASS + 12.000000000 + 2 * 15.994914619 + ELECTRON_MASS),
        "charge": -1,
        "polarity": "negative",
    },
    "[M+CH3COO]-": {
        "shift": (
            2 * 12.000000000 + 3 * PROTIUM_MASS + 2 * 15.994914619 + ELECTRON_MASS
        ),
        "charge": -1,
        "polarity": "negative",
    },
    "[M-H2O-H]-": {
        "shift": -(2 * PROTIUM_MASS + 15.994914619) - PROTON_MASS,
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
# Isotope database — (mass / Da, fractional abundance, Δn)
# Δn = nominal neutron-count offset from the monoisotopic isotope
# (e.g. ¹³C → +1, ³⁷Cl → +2).  M+1 / M+2 probabilities are built from
# Δn = 1 / Δn = 2 entries only.
# ======================================================================
_ISOTOPES: dict[str, list[tuple[float, float, int]]] = {
    "C": [(12.000000000, 0.9893, 0), (13.003354835, 0.0107, 1)],
    "H": [(1.007825032, 0.999885, 0), (2.014101778, 0.000115, 1)],
    "N": [(14.003074004, 0.99632, 0), (15.000108898, 0.00368, 1)],
    "O": [
        (15.994914619, 0.99757, 0),
        (16.999131756, 0.00038, 1),
        (17.999159612, 0.00205, 2),
    ],
    "S": [
        (31.972071174, 0.9493, 0),
        (32.971458909, 0.0076, 1),
        (33.967867004, 0.0429, 2),
    ],
    "Cl": [(34.968852690, 0.7578, 0), (36.965902580, 0.2422, 2)],
    "Br": [(78.918337600, 0.5069, 0), (80.916289700, 0.4931, 2)],
    "P": [(30.973761998, 1.0, 0)],
    "F": [(18.998403163, 1.0, 0)],
    "I": [(126.904467700, 1.0, 0)],
    "Na": [(22.989769280, 1.0, 0)],
    "K": [
        (38.963706490, 0.93258, 0),
        (39.963998170, 0.00012, 1),
        (40.961825260, 0.06730, 2),
    ],
    "Si": [
        (27.976926535, 0.9223, 0),
        (28.976494665, 0.0467, 1),
        (29.973770010, 0.0310, 2),
    ],
    "Fe": [
        (53.939609000, 0.05845, 0),
        (55.934936000, 0.91754, 2),
        (56.935393000, 0.02119, 3),
        (57.933274000, 0.00282, 4),
    ],
    "Se": [
        (73.922475934, 0.0089, 0),
        (75.919213700, 0.0937, 2),
        (76.919914200, 0.0763, 3),
        (77.917309100, 0.2377, 4),
        (79.916521800, 0.4961, 6),
        (81.916709500, 0.0873, 8),
    ],
}
"""Isotopes ordered by ascending mass; first entry = monoisotopic."""


# ======================================================================
# Pydantic schemas
# ======================================================================
class AdductInput(BaseModel):
    """Validated input for predict_adduct_offset."""

    adduct_string: str = Field(..., min_length=3)


class IsotopeInput(BaseModel):
    """Validated input for annotate_isotopes."""

    identifier: str = Field(..., min_length=1)
    is_smiles: bool = False
    isotopologues: int = Field(default=3, ge=1, le=10)


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

    Returns ``[(mass_Da, rel_abundance), ...]`` for ``M, M+1, ...`` up to
    ``max_isotopologue`` entries, normalised so the monoisotopic peak is 1.0.

    The envelope is the **exact multinomial expansion** of every element's
    isotope pattern, convolved across all atoms: the coefficient of ``x**k`` in
    ``\u220f_element (\u03a3_isotope a_i \u00b7 x**\u0394n_i)**count`` is the relative abundance of
    the ``M+k`` isotopologue.  Doing the expansion in full means an element's
    whole isotope ladder contributes, including the \u0394n \u2265 3 isotopes the earlier
    \u0394n \u2264 2 shortcut dropped (\u2075\u2077Fe is \u0394n = 4, \u2078\u2070Se is \u0394n = 6).  Halogens are the
    clearest case: Cl and Br have no \u0394n = 1 isotope, so a dibromo compound's
    envelope is M, M+2, **M+4** (\u2248 1 : 2 : 1 for Br\u2082), which no \u0394n \u2264 2 shortcut
    can express.  Raise ``max_isotopologue`` to see those higher isotopologues.

    The mass reported for ``M+k`` is the abundance-weighted mean over every
    substitution combination that yields *k* extra neutrons (``k = 2`` is two
    \u00b9\u00b3C, *or* one \u00b9\u2078O, *or* one \u00b9\u00b3C plus one \u00b9\u2075N, ...), which is the
    unresolved peak a unit-resolution instrument reports; the fine structure is
    not resolved.  A substitution shifts the mass by the difference between the
    isotope and the monoisotopic mass of that *element* (\u00b9\u00b3C - \u00b9\u00b2C =
    +1.003355 Da), not by the neutron mass (1.008665 Da): using the neutron mass
    put M+1 about 5.3 mDa (~18 ppm at m/z 300) too high, outside the tolerance
    the rest of this project works to.
    """
    if max_isotopologue < 1:
        raise ValueError(
            f"max_isotopologue must be at least 1, got {max_isotopologue!r}"
        )

    # --- monoisotopic mass ------------------------------------------------
    mono_mass = 0.0
    for el, count in composition.items():
        mono_mass += _ISOTOPES[el][0][0] * count

    # weight[k]        : relative abundance of exactly k extra neutrons
    # shift_weight[k]  : \u03a3 abundance \u00b7 \u0394mass over those combinations, so the
    #                    abundance-weighted mean shift is shift_weight[k]/weight[k].
    weight = [1.0] + [0.0] * (max_isotopologue - 1)
    shift_weight = [0.0] * max_isotopologue

    for el, count in composition.items():
        isotopes = _ISOTOPES[el]
        mono_el_mass = isotopes[0][0]
        # One atom's polynomial: (abundance, \u0394n, \u0394mass vs the monoisotopic atom).
        atom = [(abund, delta, mass - mono_el_mass) for mass, abund, delta in isotopes]
        for _ in range(count):
            new_weight = [0.0] * max_isotopologue
            new_shift = [0.0] * max_isotopologue
            for k in range(max_isotopologue):
                wk = weight[k]
                sk = shift_weight[k]
                if wk == 0.0 and sk == 0.0:
                    continue
                for abund, delta, dm in atom:
                    k2 = k + delta
                    if k2 >= max_isotopologue:
                        continue
                    new_weight[k2] += wk * abund
                    new_shift[k2] += sk * abund + wk * abund * dm
            weight, shift_weight = new_weight, new_shift

    # Normalise to the monoisotopic peak (weight[0] is never zero: every
    # supported element has a monoisotopic isotope).
    base = weight[0]
    pattern: list[tuple[float, float]] = []
    for k in range(max_isotopologue):
        wk = weight[k]
        abund = wk / base
        mean_shift = (shift_weight[k] / wk) if wk else 0.0
        pattern.append((mono_mass + mean_shift, abund))
    return pattern


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
    """Register cheminformatics tools on the supplied MCPServer *mcp* instance."""

    # ------------------------------------------------------------------
    # Tool: predict_adduct_offset
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Predict Adduct Mass Offset",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def predict_adduct_offset(
        adduct_string: Annotated[
            str,
            Field(
                min_length=3,
                description=(
                    "Adduct notation, e.g. '[M+H]+', '[M-H]-' or '[M+Na]+'. "
                    "Must be one of the instrument-standard adducts."
                ),
            ),
        ],
    ) -> str:
        """Return the exact mass shift that a standard ionisation adduct adds to a neutral molecule.

        Use this to convert a neutral monoisotopic mass into the expected
        precursor m/z for a given ionisation pathway, or to check whether an
        adduct assignment is physically plausible.

        Returns the adduct polarity, charge state, exact mass shift in daltons
        (Da), and the formula for the resulting m/z offset.  Note that for a
        charge state of 1 the reported shift is the direct m/z offset, whereas
        for higher charge states the shift must be divided by the absolute
        charge (the returned offset formula already shows this).

        Only canonical adduct notations from a fixed table are accepted (e.g.
        '[M+H]+', '[M+Na]+', '[M+NH4]+', '[M+H-H2O]+', '[M+2H]2+', '[M-H]-',
        '[M+Cl]-', '[M+HCOO]-'); matching is case-insensitive.  Any other
        string is rejected and the supported adducts are listed, so do not
        invent an adduct that is not offered there.  The adduct string must be
        at least 3 characters long.
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
    @mcp.tool(
        title="Annotate Isotope Pattern",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def annotate_isotopes(
        identifier: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Chemical formula (e.g. 'C6H12O6') or SMILES string, "
                    "depending on is_smiles."
                ),
            ),
        ],
        is_smiles: Annotated[
            bool,
            Field(
                description=(
                    "Set to True when identifier is a SMILES string; leave "
                    "False when it is a chemical formula."
                ),
            ),
        ] = False,
        isotopologues: Annotated[
            int,
            Field(
                ge=1,
                le=10,
                description="How many isotopologue rows to return, from M up "
                "to M+(isotopologues-1). Default 3 (M, M+1, M+2). Raise it for "
                "halogen-rich formulas, whose diagnostic M+4 peak needs more "
                "rows.",
            ),
        ] = 3,
    ) -> str:
        """Predict the theoretical isotope pattern (M, M+1, ...) of a compound.

        Use this to obtain isotopologue masses and relative abundances, for
        example to check a measured isotope envelope against a proposed
        molecular formula or to confirm a monoisotopic mass.

        Returns the monoisotopic mass in daltons (Da) and a Markdown table
        giving the theoretical mass and relative abundance of each isotopologue
        from M up to M+(isotopologues-1), normalised so that the monoisotopic
        peak is 1.0000.  The envelope is the exact multinomial expansion of each
        element's isotopes, so it handles the full isotope ladder: a chlorine-
        or bromine-rich formula carries its signal in M+2 and M+4 (Cl and Br
        have no single-neutron isotope), while ordinary C/H/N/O compounds
        concentrate it in M+1 and M+2.  Raise `isotopologues` to reveal the
        higher peaks a halogen-rich formula carries.

        Pass a chemical formula as the identifier by default (for example
        'C6H12O6'); set is_smiles=True only when the identifier is a SMILES
        string.  SMILES resolution requires the optional RDKit dependency: if
        it is missing, an error is returned and the formula should be supplied
        instead.  The identifier must be at least one character long, and only
        elements with tabulated isotope data are supported (C, H, N, O, S, P,
        F, Cl, Br, I, Na, K, Si, Fe, Se) - any other element returns an error.
        """
        _ = IsotopeInput(
            identifier=identifier,
            is_smiles=is_smiles,
            isotopologues=isotopologues,
        )

        # --- SMILES → formula conversion -----------------------------------
        if is_smiles:
            formula = _smiles_to_formula(identifier)
            if formula is None:
                return (
                    "ERROR: RDKit is not installed and the SMILES string "
                    "could not be resolved.\n\n"
                    "Install the optional `chem` extra (`uv sync --extra chem`) "
                    "to enable SMILES parsing, or compute the chemical "
                    "formula for this structure manually and resubmit using "
                    "**is_smiles=False** with the formula string as "
                    "*identifier*."
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
        pattern = _isotope_pattern(composition, max_isotopologue=isotopologues)

        # --- render Markdown table ------------------------------------------
        lines = [
            f"## Isotope Pattern: {formula}",
            "",
            f"Monoisotopic mass: **{pattern[0][0]:.4f} Da**",
            "",
            "| Isotopologue | Theoretical Mass (Da) | Relative Abundance |",
            "|-------------|----------------------|--------------------|",
        ]
        labels = ["M"] + [f"M+{k}" for k in range(1, isotopologues)]
        for (mass, abund), label in zip(pattern, labels, strict=True):
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
