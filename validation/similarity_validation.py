"""Independent similarity / library-search validation for MSMCP (issue #54).

One bounded, reproducible experiment.  It answers two *separate* questions and
keeps their answers apart:

1. **Implementation agreement** — does ``msmcp.models.scoring.ClassicalScorer``
   compute the same cosine as the established, independent ``matchms``
   ``CosineGreedy`` on identical peak lists at the same m/z window?
2. **Identification recovery** — on held-out spectra, does MSMCP's ranking
   recover the correct compound (top-1 / top-k), with self-matches and duplicate
   leakage removed?

Agreement is *not* accuracy: two implementations agreeing says nothing about
whether either identifies the right compound.  Reporting them as one number
would be the mistake this experiment exists to avoid.

Dataset (external; never committed)
-----------------------------------
MassBank-data release **2026.03**, asset ``MassBank_NISTformat.msp``.
Licence **CC-BY-4.0** (MassBank consortium; see ``CITATION.cff`` in MassBank-data).
DOI ``10.5281/zenodo.3378723``.
URL ``https://github.com/MassBank/MassBank-data/releases/download/2026.03/MassBank_NISTformat.msp``
SHA-256 ``cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269``

Run::

    MSMCP_VALIDATION_DATA=/path/to/MassBank_NISTformat.msp \\
        uv run --with "matchms==0.33.1" python validation/similarity_validation.py

The file is verified against the SHA-256 above before anything is scored; a
changed file stops the run.  ``matchms`` is deliberately *not* a project
dependency — it is supplied for this command only, so the runtime and the fast
CI suite never see it.

Pre-registered tolerances (fixed before results were inspected)
--------------------------------------------------------------
* Uncontested pairs (no reference peak within the window of two query peaks, and
  no query peak within the window of two reference peaks) must agree to
  ``<= 1e-9``.
* Overall: median ``|delta|`` ``<= 0.01`` and 95th percentile ``<= 0.10``;
  Spearman rho ``>= 0.99`` (computed **tie-aware**, average ranks — spectral
  scores are frequently tied at exactly 0, and arbitrary distinct ranks would
  inflate the statistic).
* These tolerate the *known* algorithmic difference below, not arbitrary
  divergence.

Reported quantities are kept distinct
-------------------------------------
* **Agreement** (implementation consistency) and **recovery** (identification
  correctness) are separate questions and are never combined.
* Recovery is reported with **two denominators**: *conditional* (answerable
  queries only) and *original denominator* (all queries; an excluded query
  counts as a miss).  The original-denominator number is the population-stable
  one to compare between variants.
* ``n_pairs`` agreement pairs are drawn from a 250 x 3499 grid and are **not**
  independent observations.

Expected discrepancy (why agreement is not exact)
--------------------------------------------------
``ClassicalScorer`` matches query peaks **in order**, each to the **nearest**
free reference peak.  ``CosineGreedy`` matches the **highest intensity product
first**.  When a reference peak is within the window of several query peaks, or
a query peak is within the window of several reference peaks (both are
"contests"), the two pick differently and the scores diverge — by up to ~0.42 on
a single pair in this data.  The experiment counts contests and reports them
rather than hiding them in an average.

This is an intentional design choice, **not** a harmless one: a twofold score
difference can change candidate ordering, any score threshold applied
(including the near-duplicate cutoff used here), and downstream interpretation.
Quantifying how often it changes a ranking on realistic spectra is a separate,
not-yet-scoped piece of work.
"""

# The optional validation dependency (matchms) is not installed in the runtime or
# the fast CI suite, so its imports must not fail the static checks there.
# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np

from msmcp.library import get_library_provider
from msmcp.models.scoring import ClassicalScorer
from msmcp.security import SecurityPolicy

# --- pinned dataset -------------------------------------------------------
MASSBANK_SHA256 = "cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269"
MASSBANK_URL = (
    "https://github.com/MassBank/MassBank-data/releases/download/2026.03/"
    "MassBank_NISTformat.msp"
)
MASSBANK_TAG = "2026.03"
MASSBANK_LICENCE = "CC-BY-4.0"
MASSBANK_DOI = "10.5281/zenodo.3378723"

# --- experiment parameters (pre-registered) -------------------------------
SEED = 20261014
TOLERANCE_DA = 0.02  # MSMCP's generic MS2 window; the same for matchms
MIN_PEAKS = 5
MAX_PEAKS = 300
PRECURSOR_MIN = 100.0
PRECURSOR_MAX = 1200.0
N_QUERY_COMPOUNDS = 250
N_DB_TARGET = 3500
N_AGREEMENT_PAIRS = 20000
NEAR_DUPLICATE_COSINE = 0.995
TOP_K = (1, 5, 10)

AGREE_EXACT = 1e-9
MEDIAN_ABS_TOL = 0.01
P95_ABS_TOL = 0.10
SPEARMAN_TOL = 0.99


@dataclass(slots=True)
class Record:
    """One library spectrum with the fields this experiment needs."""

    uid: str
    inchikey: str
    name: str
    peaks: tuple[tuple[float, float], ...]


def _matchms_pair_score(greedy: Any, reference: Any, query: Any) -> float:
    """Cosine from a matchms ``CosineGreedy.pair`` result.

    ``pair`` returns a 0-d structured array whose first field is the score;
    ``.item()`` exposes it as a plain tuple, which is the one access form the
    type checkers accept for a numpy structured scalar.
    """
    return float(np.asarray(greedy.pair(reference, query)).item()[0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_records(path: Path) -> list[Record]:
    """Read the pinned MSP, applying the documented preprocessing filters."""
    policy = SecurityPolicy(
        allowed_root=path.parent, max_file_size_bytes=1024 * 1024 * 1024
    )
    provider = get_library_provider(path, policy)
    records: list[Record] = []
    for chunk in provider.iter_spectra(5000):
        for s in chunk:
            key = s.metadata.get("INCHIKEY", "")
            spectrum_type = s.metadata.get("SPECTRUMTYPE", "")
            if not key or s.polarity != "positive":
                continue
            if spectrum_type and spectrum_type != "MS2":
                continue
            if not (MIN_PEAKS <= len(s.peaks) <= MAX_PEAKS):
                continue
            if s.precursor_mz is None or not (
                PRECURSOR_MIN <= s.precursor_mz <= PRECURSOR_MAX
            ):
                continue
            records.append(
                Record(
                    uid=s.metadata.get("DB", f"idx{s.index}"),
                    inchikey=key,
                    name=s.compound_name,
                    peaks=s.peaks,
                )
            )
    return records


def _distinct(records: list[Record]) -> list[Record]:
    """Drop records whose peak list is identical to an earlier one (duplicates)."""
    seen: set[tuple[tuple[float, float], ...]] = set()
    out: list[Record] = []
    for r in records:
        if r.peaks in seen:
            continue
        seen.add(r.peaks)
        out.append(r)
    return out


def build_sets(
    records: list[Record], rng: random.Random
) -> tuple[list[Record], list[Record], dict[str, str]]:
    """Split into (queries, database, query_uid -> compound).

    A query compound is one with >= 2 distinct records.  One record is the query;
    the compound's other records stay in the database, so a correct answer
    requires a genuinely *different* measurement, not the query itself.
    """
    by_compound: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_compound[r.inchikey].append(r)

    eligible = [k for k, v in by_compound.items() if len(_distinct(v)) >= 2]
    rng.shuffle(eligible)
    chosen = eligible[:N_QUERY_COMPOUNDS]

    queries: list[Record] = []
    truth: dict[str, str] = {}
    in_group_db: list[Record] = []
    query_peak_sets: set[tuple[tuple[float, float], ...]] = set()
    for key in chosen:
        group = sorted(
            _distinct(by_compound[key]), key=lambda r: (-len(r.peaks), r.uid)
        )
        query = group[0]
        queries.append(query)
        truth[query.uid] = key
        query_peak_sets.add(query.peaks)
        in_group_db.extend(group[1:])

    chosen_set = set(chosen)
    others = [r for r in records if r.inchikey not in chosen_set]
    rng.shuffle(others)
    n_fill = max(0, N_DB_TARGET - len(in_group_db))
    database = in_group_db + others[:n_fill]

    # Leakage guard: a database entry identical to a query is a self-match.
    database = [r for r in database if r.peaks not in query_peak_sets]
    return queries, database, truth


def _contested(query: Record, reference: Record, tol: float) -> bool:
    """True if the matching is ambiguous in either direction.

    A contest is where the two greedy rules can legitimately choose differently:
    a reference peak within *tol* of two or more query peaks, or a query peak
    within *tol* of two or more reference peaks.
    """
    ref_mz = [mz for mz, _ in reference.peaks]
    qry_mz = [mz for mz, _ in query.peaks]

    def ambiguous(source: list[float], target: list[float]) -> bool:
        return any(sum(1 for t in target if abs(s - t) <= tol) >= 2 for s in source)

    return ambiguous(ref_mz, qry_mz) or ambiguous(qry_mz, ref_mz)


def _average_ranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks with tied values assigned their average rank.

    Ties matter here: similarity scores are frequently exactly equal (many zero
    similarities), and giving them arbitrary distinct ranks inflates the
    correlation.  This is the rank convention Spearman's rho is defined with.
    """
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(values.size, dtype=float)
    i = 0
    n = values.size
    while i < n:
        j = i
        while j + 1 < n and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman_correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Tie-aware Spearman rho = Pearson correlation of *average* ranks.

    No scipy dependency; cross-checked against ``scipy.stats.spearmanr`` in the
    test suite.  A constant input has no rank variance, so the coefficient is
    undefined and reported as 0.0 rather than a misleading 1.0.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.size != y.size or x.size < 2:
        raise ValueError("spearman_correlation needs equal-length arrays of >= 2")
    rx = _average_ranks(x)
    ry = _average_ranks(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = float(np.sqrt((rx**2).sum() * (ry**2).sum()))
    if denom == 0.0:
        return 0.0
    return float((rx * ry).sum() / denom)


def _recovery_from_matrix(
    matrix: np.ndarray,
    ref_key: list[str],
    queries: list[Record],
    truth: dict[str, str],
    *,
    strict: bool,
) -> dict:
    """Top-k recovery from a ``(n_queries, n_db)`` score matrix.

    Two denominators are reported, because ``strict`` changes the population:

    * ``conditional`` - among queries whose compound still has a database entry
      after filtering (*answerable*);
    * ``original_denominator`` - over **all** original queries, so an excluded
      query counts as a miss.  This population-stable number is the one to
      compare between variants.

    ``strict`` removes database entries scoring >= :data:`NEAR_DUPLICATE_COSINE`
    to the query before ranking (near-duplicate leakage).  Ties are broken by
    database index (stable sort) so the ranking is deterministic.
    """
    keys = np.asarray(ref_key)
    n_total = len(queries)
    hits = {k: 0 for k in TOP_K}
    answerable = 0
    for qi, q in enumerate(queries):
        same = keys == truth[q.uid]
        scores = matrix[qi].astype(float).copy()
        if strict:
            scores = np.where(scores < NEAR_DUPLICATE_COSINE, scores, -1.0)
        if not bool(np.any(same & (scores >= 0.0))):
            continue
        answerable += 1
        order = np.argsort(-scores, kind="mergesort")
        for k in TOP_K:
            if bool(np.any(same[order[:k]])):
                hits[k] += 1

    def _frac(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    return {
        "n_queries_total": n_total,
        "answerable_queries": answerable,
        "excluded_queries": n_total - answerable,
        "hits": {str(k): hits[k] for k in TOP_K},
        "conditional": {f"top{k}": _frac(hits[k], answerable) for k in TOP_K},
        "original_denominator": {f"top{k}": _frac(hits[k], n_total) for k in TOP_K},
    }


def run(*, data_path: Path, out_path: Path | None) -> dict:
    import matchms
    from matchms import Spectrum, calculate_scores
    from matchms.similarity import CosineGreedy

    digest = _sha256(data_path)
    if digest != MASSBANK_SHA256:
        print(
            "ERROR: dataset SHA-256 mismatch (file is corrupted or the wrong "
            f"release).\n  expected {MASSBANK_SHA256}\n  got      {digest}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    records = load_records(data_path)
    rng = random.Random(SEED)
    queries, database, truth = build_sets(records, rng)

    scorer = ClassicalScorer(tolerance=TOLERANCE_DA)
    greedy = CosineGreedy(tolerance=TOLERANCE_DA)

    def ms(peaks: tuple[tuple[float, float], ...]) -> Spectrum:
        arr = np.asarray(peaks, dtype=float)
        return Spectrum(
            mz=arr[:, 0], intensities=arr[:, 1], metadata={"precursor_mz": 0.0}
        )

    # ---- 1. implementation agreement on sampled pairs --------------------
    pair_indices = [
        (rng.randrange(len(queries)), rng.randrange(len(database)))
        for _ in range(N_AGREEMENT_PAIRS)
    ]
    deltas: list[float] = []
    msmcp_scores: list[float] = []
    matchms_scores: list[float] = []
    exact_agree = 0
    contest_total = 0
    contest_differ = 0
    noncontest_differ = 0
    worst: tuple[float, dict[str, object] | None] = (0.0, None)
    for qi, di in pair_indices:
        q, r = queries[qi], database[di]
        a = scorer.score(q.peaks, r.peaks)
        b = _matchms_pair_score(greedy, ms(r.peaks), ms(q.peaks))
        d = abs(a - b)
        deltas.append(d)
        msmcp_scores.append(a)
        matchms_scores.append(b)
        if d <= AGREE_EXACT:
            exact_agree += 1
        contested = _contested(q, r, TOLERANCE_DA)
        if contested:
            contest_total += 1
            if d > AGREE_EXACT:
                contest_differ += 1
        elif d > AGREE_EXACT:
            noncontest_differ += 1
        if d > worst[0]:
            worst = (d, {"query": q.uid, "db": r.uid, "msmcp": a, "matchms": b})

    deltas_arr = np.asarray(deltas)
    rho = spearman_correlation(np.asarray(msmcp_scores), np.asarray(matchms_scores))
    agreement = {
        "n_pairs": len(pair_indices),
        "sampling": {
            "distinct_query_spectra": len({qi for qi, _ in pair_indices}),
            "distinct_reference_spectra": len({_di for _, _di in pair_indices}),
            "note": (
                "pairs are drawn from a 250-query x 3499-reference grid with "
                "replacement and are NOT independent observations"
            ),
        },
        "median_abs_delta": float(np.median(deltas_arr)),
        "p95_abs_delta": float(np.percentile(deltas_arr, 95)),
        "max_abs_delta": float(deltas_arr.max()),
        "exact_agreement_frac": exact_agree / len(pair_indices),
        "contested_pairs": contest_total,
        "contested_pairs_differing": contest_differ,
        "uncontested_pairs_differing": noncontest_differ,
        "spearman_rho": rho,
        "worst_pair": worst,
        "tolerances": {
            "exact": AGREE_EXACT,
            "median_abs": MEDIAN_ABS_TOL,
            "p95_abs": P95_ABS_TOL,
            "spearman": SPEARMAN_TOL,
        },
        "verdict": {
            "median_ok": float(np.median(deltas_arr)) <= MEDIAN_ABS_TOL,
            "p95_ok": float(np.percentile(deltas_arr, 95)) <= P95_ABS_TOL,
            "spearman_ok": rho >= SPEARMAN_TOL,
            "uncontested_exact_ok": noncontest_differ == 0,
        },
    }

    # ---- 2. identification recovery for both scorers ---------------------
    matrix_msmcp = np.vstack(
        [
            np.asarray(
                scorer.score_many(q.peaks, [r.peaks for r in database]), dtype=float
            )
            for q in queries
        ]
    )
    query_specs = [ms(q.peaks) for q in queries]
    db_specs = [ms(r.peaks) for r in database]
    full = calculate_scores(cast(Any, db_specs), cast(Any, query_specs), greedy)
    matrix = full.scores.to_array()
    assert matrix is not None
    score_field = next(
        name for name in matrix.dtype.names or () if name.endswith("_score")
    )
    matrix_matchms = np.asarray(matrix[score_field], dtype=float).T
    ref_key = [r.inchikey for r in database]

    recovery = {
        "msmcp": {
            "naive": _recovery_from_matrix(
                matrix_msmcp, ref_key, queries, truth, strict=False
            ),
            "strict": _recovery_from_matrix(
                matrix_msmcp, ref_key, queries, truth, strict=True
            ),
        },
        "matchms": {
            "naive": _recovery_from_matrix(
                matrix_matchms, ref_key, queries, truth, strict=False
            ),
            "strict": _recovery_from_matrix(
                matrix_matchms, ref_key, queries, truth, strict=True
            ),
        },
    }

    result = {
        "dataset": {
            "name": "MassBank",
            "release": MASSBANK_TAG,
            "asset": "MassBank_NISTformat.msp",
            "url": MASSBANK_URL,
            "licence": MASSBANK_LICENCE,
            "doi": MASSBANK_DOI,
            "sha256": digest,
        },
        "matchms_version": getattr(matchms, "__version__", "unknown"),
        "parameters": {
            "seed": SEED,
            "tolerance_da": TOLERANCE_DA,
            "min_peaks": MIN_PEAKS,
            "max_peaks": MAX_PEAKS,
            "precursor_range": [PRECURSOR_MIN, PRECURSOR_MAX],
            "n_query_compounds": N_QUERY_COMPOUNDS,
            "n_db_target": N_DB_TARGET,
            "n_agreement_pairs": N_AGREEMENT_PAIRS,
            "near_duplicate_cosine": NEAR_DUPLICATE_COSINE,
        },
        "counts": {
            "filtered_records": len(records),
            "queries": len(queries),
            "database": len(database),
        },
        "implementation_agreement": agreement,
        "identification_recovery": recovery,
    }
    if out_path is not None:
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _print_report(result: dict) -> None:
    d = result["dataset"]
    a = result["implementation_agreement"]
    rec = result["identification_recovery"]
    print("=" * 74)
    print("MSMCP similarity / library-search validation (issue #54)")
    print("=" * 74)
    print(f"Dataset : {d['name']} {d['release']} ({d['asset']})  {d['licence']}")
    print(f"          doi {d['doi']}  sha256 {d['sha256'][:16]}…")
    print(f"matchms : {result['matchms_version']}   counts: {result['counts']}")
    print("-" * 74)
    print("1. IMPLEMENTATION AGREEMENT (MSMCP ClassicalScorer vs matchms CosineGreedy)")
    print(f"   pairs                 : {a['n_pairs']}")
    print(
        f"   median |delta|        : {a['median_abs_delta']:.3e}  (tol {a['tolerances']['median_abs']})"
    )
    print(
        f"   p95    |delta|        : {a['p95_abs_delta']:.3e}  (tol {a['tolerances']['p95_abs']})"
    )
    print(f"   max    |delta|        : {a['max_abs_delta']:.3e}")
    print(f"   exact agreement       : {a['exact_agreement_frac']:.4f}")
    print(
        f"   Spearman rho          : {a['spearman_rho']:.6f}  (tol {a['tolerances']['spearman']})"
    )
    print(
        f"   contested pairs       : {a['contested_pairs']} (differing {a['contested_pairs_differing']})"
    )
    print(f"   uncontested differing : {a['uncontested_pairs_differing']}")
    print(f"   verdict               : {a['verdict']}")
    print("-" * 74)
    print("2. IDENTIFICATION RECOVERY (self-matches + identical duplicates removed)")
    print(
        f"   queries: {rec['msmcp']['naive']['n_queries_total']} total; "
        f"strict excludes those whose compound has no remaining reference"
    )
    for who in ("msmcp", "matchms"):
        for variant in ("naive", "strict"):
            r = rec[who][variant]
            c = r["conditional"]
            o = r["original_denominator"]
            print(
                f"   {who:8s} {variant:6s}: answerable {r['answerable_queries']}"
                f"/{r['n_queries_total']} (excluded {r['excluded_queries']})"
            )
            print(
                f"      conditional        top1={c['top1']:.4f} top5={c['top5']:.4f} "
                f"top10={c['top10']:.4f}"
            )
            print(
                f"      original-denominator top1={o['top1']:.4f} top5={o['top5']:.4f} "
                f"top10={o['top10']:.4f}"
            )
    print("=" * 74)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MSMCP similarity validation (#54)")
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Path to MassBank_NISTformat.msp (or set MSMCP_VALIDATION_DATA).",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="Write results JSON here."
    )
    args = parser.parse_args(argv)

    if args.data is not None:
        path = args.data
    else:
        env_value = os.environ.get("MSMCP_VALIDATION_DATA", "").strip()
        path = Path(env_value) if env_value else None

    if path is None:
        # Intentionally absent dataset: skip cleanly, download nothing.
        print(
            "similarity validation: skipped - no dataset supplied.\n"
            "  Set MSMCP_VALIDATION_DATA or pass --data to run (nothing was "
            "downloaded).\n"
            f"  fetch:  {MASSBANK_URL}\n  sha256: {MASSBANK_SHA256}",
            file=sys.stderr,
        )
        return 0

    if not path.is_file():
        # Explicitly provided but missing: fail loudly, do not silently skip.
        print(
            f"ERROR: validation dataset not found: {path}\n"
            f"  fetch:  {MASSBANK_URL}\n  sha256: {MASSBANK_SHA256}",
            file=sys.stderr,
        )
        return 2

    result = run(data_path=path, out_path=args.out)
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
