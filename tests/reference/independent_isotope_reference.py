"""Independent isotope reference generator (not used by the test suite).

This script re-derives, from first principles and *without importing msmcp*, the
reference monoisotopic masses and exact M / M+1 / M+2 abundances that
``tests/test_scientific_validation.py`` asserts against.  It exists so those
reference values are reproducible and auditable rather than taken on trust.

Two independent choices distinguish it from ``msmcp.tools.chem``:

* **Source data** - a separate transcription of AME2020 atomic masses and CIAAW
  representative isotopic abundances (the library's table is its own
  transcription; the ~0.1 % spread between published ³⁷Cl/³⁵Cl ratios is the
  reason an abundance tolerance is needed at all).
* **Algorithm** - the exact polynomial/multinomial expansion of each element's
  isotopologue distribution, convolved across elements in ``decimal`` arithmetic
  at 60-digit precision.  The library uses the same physics but its own
  formulation, so agreement is evidence, not tautology.

Run it to regenerate the reference table::

    uv run python tests/reference/independent_isotope_reference.py

Output (mass in Da, abundances relative to M = 1):

    formula            mono (Da)         M+1         M+2
    CH4                16.031300    0.011276    0.000005
    H2O                18.010565    0.000611    0.002055
    C2H6O              46.041865    0.022702    0.002196
    C6H12O6           180.063388    0.068560    0.014329
    C7H6O             106.041865    0.076781    0.004593
    C8H10N4O2         194.080376    0.103212    0.008941
    CHCl3             117.914383    0.010931    0.959875
    C6H5Cl            112.007978    0.065469    0.321750
    Cl2                69.937705    0.000000    0.639916
    Br2               157.836675    0.000000    1.945551

See SCIENTIFIC_VALIDATION.md for the tolerances derived from this table.
"""

from __future__ import annotations

from decimal import Decimal, getcontext

# mass / Da, representative abundance. Lightest isotope first.
ISOTOPES: dict[str, list[tuple[str, float]]] = {
    "C": [("12.0000000", 0.9893), ("13.0033548378", 0.0107)],
    "H": [("1.00782503207", 0.999885), ("2.0141017778", 0.000115)],
    "N": [("14.0030740048", 0.99632), ("15.0001088982", 0.00368)],
    "O": [
        ("15.9949146196", 0.99757),
        ("16.99913150", 0.00038),
        ("17.99916100", 0.00205),
    ],
    "S": [
        ("31.9720711744", 0.9499),
        ("32.9714589098", 0.0075),
        ("33.967867004", 0.0425),
    ],
    "Cl": [("34.968852682", 0.7576), ("36.965902602", 0.2424)],
    "Br": [("78.9183376", 0.5069), ("80.9162897", 0.4931)],
    "P": [("30.973761998", 1.0)],
    "F": [("18.998403163", 1.0)],
    "I": [("126.904468", 1.0)],
    "Na": [("22.989769282", 1.0)],
    "K": [("38.963706486", 0.93258), ("40.961825258", 0.06730)],
    "Si": [
        ("27.976926535", 0.9223),
        ("28.976494665", 0.0467),
        ("29.973770010", 0.0310),
    ],
}

MOLECULES: dict[str, dict[str, int]] = {
    "CH4": {"C": 1, "H": 4},
    "H2O": {"H": 2, "O": 1},
    "C2H6O": {"C": 2, "H": 6, "O": 1},
    "C6H12O6": {"C": 6, "H": 12, "O": 6},
    "C7H6O": {"C": 7, "H": 6, "O": 1},
    "C8H10N4O2": {"C": 8, "H": 10, "N": 4, "O": 2},
    "CHCl3": {"C": 1, "H": 1, "Cl": 3},
    "C6H5Cl": {"C": 6, "H": 5, "Cl": 1},
    "Cl2": {"Cl": 2},
    "Br2": {"Br": 2},
}


def monoisotopic_mass(composition: dict[str, int]) -> float:
    """Sum of the lightest-isotope masses (Da)."""
    return sum(float(ISOTOPES[el][0][0]) * n for el, n in composition.items())


def _nominal_index(mass: str, base: str) -> int:
    """Nominal neutron-count offset of *mass* relative to *base*."""
    return round(float(mass) - float(base))


def _element_polynomial(el: str, count: int) -> list[float]:
    """Exact nominal-shift distribution for ``count`` atoms of element *el*.

    Expands ``(sum_j a_j x^{n_j}) ** count`` over the element's isotopes.
    """
    masses = ISOTOPES[el]
    base = masses[0][0]
    poly: dict[int, float] = {0: 1.0}
    for _ in range(count):
        nxt: dict[int, float] = {}
        for shift, coeff in poly.items():
            for mass, abundance in masses:
                ns = shift + _nominal_index(mass, base)
                nxt[ns] = nxt.get(ns, 0.0) + coeff * abundance
        poly = nxt
    return [poly.get(k, 0.0) for k in range(max(poly) + 1)]


def exact_envelope(composition: dict[str, int], max_shift: int = 2) -> list[float]:
    """Exact M, M+1 ... M+max_shift relative abundances (M == 1.0)."""
    getcontext().prec = 60
    combined: list[Decimal] = [Decimal(1)]
    for el, n in composition.items():
        dist = [Decimal(repr(c)) for c in _element_polynomial(el, n)]
        new = [Decimal(0)] * (len(combined) + len(dist) - 1)
        for i, a in enumerate(combined):
            for j, b in enumerate(dist):
                new[i + j] += a * b
        combined = new
    mono = combined[0]
    limit = min(max_shift, len(combined) - 1)
    return [float(combined[k] / mono) for k in range(limit + 1)]


def main() -> None:
    print(f"{'formula':<12}{'mono (Da)':>16}{'M+1':>12}{'M+2':>12}")
    for name, comp in MOLECULES.items():
        env = exact_envelope(comp, max_shift=2)
        m1 = env[1] if len(env) > 1 else 0.0
        m2 = env[2] if len(env) > 2 else 0.0
        print(f"{name:<12}{monoisotopic_mass(comp):>16.6f}{m1:>12.6f}{m2:>12.6f}")


if __name__ == "__main__":
    main()
