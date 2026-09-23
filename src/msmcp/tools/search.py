"""Spectral library search, executed through the pluggable job executor.

``search_library`` submits the CPU-bound scan to a
:class:`~msmcp.execution.executor.JobExecutor` and returns immediately with a
job ID; ``check_search_status`` polls it and ``cancel_search`` stops it.  The
tools depend only on the executor interface, so the server does not need to
know whether execution is thread-based (the default), process-based or
delegated to an orchestrator.

Honesty about the data
----------------------
MSMCP has no spectral-library reader: MassFlow, the canonical MS I/O layer,
exposes none, and inventing a library format would be worse than admitting the
gap.  The **library side** of this search is therefore still synthetic - a
deterministic in-memory SQLite database seeded from the ``database_file``
string.  The scanning, scoring, FDR/p-value estimation and report formatting
are real.  Reports always carry a banner saying so, because a plausible-looking
hit table is the single most dangerous thing this server could emit.

The **query** spectrum is always real data: either read from disk through the
ingestion layer, or dereferenced from the server-side reference store.  There
is deliberately no synthetic-query mode - a pipeline demonstration that
generates its own input cannot tell you anything about real data.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import numpy as np
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator

from msmcp import ingest
from msmcp.errors import MsmcpError
from msmcp.execution import (
    JobStatusSnapshot,
    LocalAsyncExecutor,
    UnknownJobError,
)
from msmcp.models import get_embedder
from msmcp.models.backends import LSM_MS2_CKPT_ENV
from msmcp.provenance import ModelInfo, Provenance, SourceRef, provenance_for
from msmcp.state import store as reference_store
from msmcp.tools.similarity import _cosine as _vector_cosine

logger = logging.getLogger("msmcp.tools.search")

ScoringMethod = Literal["classical", "dreams", "lsm-ms2"]


# ======================================================================
# Pydantic schemas
#
# These models enforce argument constraints inside the tool bodies.  The
# human-readable parameter descriptions live on the tool *signatures*, which
# is what the MCP SDK turns into the wire schema.
# ======================================================================
class SearchInput(BaseModel):
    """Validated input for the search_library tool.

    The experimental spectrum comes from exactly one of ``experimental_file``
    (read from disk) or ``spectrum_reference`` (already registered server-side).
    """

    experimental_file: str | None = None
    spectrum_reference: str | None = None
    database_file: str = Field(..., min_length=1)
    scoring_method: ScoringMethod = "classical"
    chunk_size: int = Field(default=2000, ge=100, le=10000)

    @model_validator(mode="after")
    def _exactly_one_query_source(self) -> SearchInput:
        sources = {
            "experimental_file": self.experimental_file,
            "spectrum_reference": self.spectrum_reference,
        }
        supplied = {name: value for name, value in sources.items() if value is not None}
        empty = sorted(name for name, value in supplied.items() if not value.strip())
        if empty:
            raise ValueError(f"{', '.join(empty)} must not be empty")
        if len(supplied) != 1:
            raise ValueError(
                "provide exactly one of experimental_file (read from disk) or "
                "spectrum_reference (already loaded server-side); got "
                f"{sorted(supplied) or 'neither'}"
            )
        return self


class StatusInput(BaseModel):
    """Validated input for check_search_status and cancel_search."""

    job_id: str = Field(..., min_length=1)


# ======================================================================
# Deterministic synthetic spectral library (documented as synthetic)
# ======================================================================
# Number of decoy scores drawn per library spectrum for the null model.
#
# The empirical p-value cannot resolve anything below 1/(n_null + 1), and
# Benjamini-Hochberg charges the top-ranked hit a factor of m (the number of
# library spectra tested), so with one decoy per spectrum the smallest
# attainable q-value is m/(m + 1) — a library search could then never report a
# hit, however good the match.  40 decoys per spectrum put the smallest
# attainable q at ≈ 0.025, i.e. clear headroom under the 0.05 threshold rather
# than sitting exactly on it.  Each decoy is one extra scorer call against peaks
# already in memory, so the cost is linear in the library and paid once per
# search; raise this if you need to report q-values well below 0.05.
NULL_DECOY_MULTIPLIER = 40

_COMPOUNDS: list[tuple[str, str, float]] = [
    ("Caffeine", "C8H10N4O2", 194.0804),
    ("Theobromine", "C7H8N4O2", 180.0647),
    ("Theophylline", "C7H8N4O2", 180.0647),
    ("Paraxanthine", "C7H8N4O2", 180.0647),
    ("Glucose", "C6H12O6", 180.0634),
    ("Fructose", "C6H12O6", 180.0634),
    ("Sucrose", "C12H22O11", 342.1162),
    ("Lactose", "C12H22O11", 342.1162),
    ("Aspirin", "C9H8O4", 180.0423),
    ("Ibuprofen", "C13H18O2", 206.1307),
    ("Acetaminophen", "C8H9NO2", 151.0633),
    ("Diazepam", "C16H13ClN2O", 284.0716),
    ("Morphine", "C17H19NO3", 285.1365),
    ("Codeine", "C18H21NO3", 299.1521),
    ("Cocaine", "C17H21NO4", 303.1471),
    ("Nicotine", "C10H14N2", 162.1157),
    ("Serotonin", "C10H12N2O", 176.0950),
    ("Dopamine", "C8H11NO2", 153.0790),
    ("Epinephrine", "C9H13NO3", 183.0895),
    ("Histamine", "C5H9N3", 111.0796),
    ("Atropine", "C17H23NO3", 289.1678),
    ("Quinine", "C20H24N2O2", 324.1838),
    ("Reserpine", "C33H40N2O9", 608.2734),
    ("Penicillin G", "C16H18N2O4S", 334.0987),
    ("Tetracycline", "C22H24N2O8", 444.1533),
    ("Erythromycin", "C37H67NO13", 733.4612),
    ("Chloramphenicol", "C11H12Cl2N2O5", 322.0123),
    ("Warfarin", "C19H16O4", 308.1049),
    ("Testosterone", "C19H28O2", 288.2089),
    ("Estradiol", "C18H24O2", 272.1776),
    ("Cortisol", "C21H30O5", 362.2093),
    ("Cholesterol", "C27H46O", 386.3549),
    ("ATP", "C10H16N5O13P3", 506.9957),
    ("NADH", "C21H27N7O14P2", 663.1091),
    ("Glutathione", "C10H17N3O6S", 307.0838),
    ("Melatonin", "C13H16N2O2", 232.1212),
    ("Taxol", "C47H51NO14", 853.3310),
    ("Vancomycin", "C66H75Cl2N9O24", 1447.4300),
    ("Cyclosporin A", "C62H111N11O12", 1201.8410),
    ("Rapamycin", "C51H79NO13", 913.5551),
]


def _stable_seed(text: str) -> int:
    """Deterministic 31-bit seed derived from *text*.

    ``hash()`` is salted per process (``PYTHONHASHSEED``), so it must not be
    used for reproducible seeding; CRC32 is stable across runs.
    """
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def _generate_peak_list(
    precursor_mz: float,
    num_peaks: int,
    rng: random.Random,
) -> list[tuple[float, float]]:
    """Synthesize a realistic-looking MS/MS peak list."""
    peaks: list[tuple[float, float]] = []
    frag_masses: list[float] = []
    for _ in range(num_peaks):
        frag_masses.append(rng.uniform(50.0, precursor_mz * 0.95))

    frag_masses.sort()
    for fm in frag_masses:
        intensity = rng.expovariate(1.0 / 500.0) * rng.uniform(0.5, 2.0)
        peaks.append((round(fm, 4), round(intensity, 2)))

    peaks.append(
        (
            round(precursor_mz + rng.uniform(-0.1, 0.1), 4),
            round(rng.uniform(100, 1000), 2),
        )
    )
    return peaks


def _build_mock_database(
    n_spectra: int = 2500,
    seed: int = 42,
) -> sqlite3.Connection:
    """Create an in-memory SQLite spectral library with synthetic spectra."""
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=OFF")

    conn.execute(
        "CREATE TABLE spectra ("
        "  id INTEGER PRIMARY KEY,"
        "  compound_name TEXT NOT NULL,"
        "  formula TEXT NOT NULL,"
        "  precursor_mz REAL NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE peaks ("
        "  spectrum_id INTEGER NOT NULL,"
        "  mz REAL NOT NULL,"
        "  intensity REAL NOT NULL,"
        "  FOREIGN KEY(spectrum_id) REFERENCES spectra(id)"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_peaks_spec ON peaks(spectrum_id)")

    for spec_id in range(1, n_spectra + 1):
        compound_idx = rng.randrange(len(_COMPOUNDS))
        name, formula, base_mass = _COMPOUNDS[compound_idx]
        precursor_mz = round(base_mass + rng.gauss(0, 0.05), 4)

        conn.execute(
            "INSERT INTO spectra VALUES (?, ?, ?, ?)",
            (spec_id, name, formula, precursor_mz),
        )

        n_peaks = rng.randint(8, 40)
        for mz_val, int_val in _generate_peak_list(precursor_mz, n_peaks, rng):
            conn.execute(
                "INSERT INTO peaks VALUES (?, ?, ?)",
                (spec_id, mz_val, int_val),
            )

    conn.commit()
    logger.info("Built synthetic library: %d spectra", n_spectra)
    return conn


# ======================================================================
# Chunked iterator (memory-safe scan)
# ======================================================================
def _iter_spectra_chunked(
    conn: sqlite3.Connection,
    chunk_size: int = 2000,
) -> Any:
    """Yield (chunk_id, list_of_spectrum_dicts) from the database."""
    total = conn.execute("SELECT COUNT(*) FROM spectra").fetchone()[0]
    offset = 0
    chunk_id = 0

    while offset < total:
        rows = conn.execute(
            "SELECT id, compound_name, formula, precursor_mz "
            "FROM spectra ORDER BY id LIMIT ? OFFSET ?",
            (chunk_size, offset),
        ).fetchall()

        spectra: list[dict[str, Any]] = []
        for row in rows:
            spec_id, name, formula, precursor_mz = row
            peak_rows = conn.execute(
                "SELECT mz, intensity FROM peaks WHERE spectrum_id=? ORDER BY mz",
                (spec_id,),
            ).fetchall()
            spectra.append(
                {
                    "id": spec_id,
                    "compound_name": name,
                    "formula": formula,
                    "precursor_mz": precursor_mz,
                    "peaks": peak_rows,
                }
            )

        yield (chunk_id, spectra)
        chunk_id += 1
        offset += chunk_size


# ======================================================================
# Cosine similarity
# ======================================================================
def _cosine(
    peaks_a: list[tuple[float, float]],
    peaks_b: list[tuple[float, float]],
    tolerance: float = 0.02,
) -> float:
    """Cosine similarity between two peak lists with m/z tolerance.

    Matching is greedy and one-to-one, so the numerator is the dot product of
    the matched intensities.  The normalisation uses **every** peak in both
    spectra: unmatched intensity is evidence the match failed to explain and
    must lower the score.  Normalising over the matched peaks alone would return
    exactly 1.0 for a spectrum that shares one peak by coincidence.
    """
    if not peaks_a or not peaks_b:
        return 0.0

    b_sorted = sorted(peaks_b, key=lambda p: p[0])
    b_mz = np.array([p[0] for p in b_sorted], dtype=np.float64)
    b_int = np.array([p[1] for p in b_sorted], dtype=np.float64)

    a_int_full = np.array([p[1] for p in peaks_a], dtype=np.float64)
    norm_a = float(np.linalg.norm(a_int_full))
    norm_b = float(np.linalg.norm(b_int))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    matched_a: list[float] = []
    matched_b: list[float] = []
    used = np.zeros(len(b_sorted), dtype=bool)

    for amz, aint in peaks_a:
        lo = np.searchsorted(b_mz, amz - tolerance, side="left")
        hi = np.searchsorted(b_mz, amz + tolerance, side="right")
        if lo >= hi:
            continue
        best_dist = float("inf")
        best_j = -1
        for j in range(lo, hi):
            if used[j]:
                continue
            d = abs(b_mz[j] - amz)
            if d < best_dist:
                best_dist = d
                best_j = j
        if best_j >= 0:
            used[best_j] = True
            matched_a.append(aint)
            matched_b.append(b_int[best_j])

    if not matched_a:
        return 0.0

    a = np.array(matched_a, dtype=np.float64)
    b = np.array(matched_b, dtype=np.float64)
    return float(np.dot(a, b) / (norm_a * norm_b))


# ======================================================================
# Scorer routing — classical peak matching vs. foundation-model embeddings
# ======================================================================
PeakPairs = list[tuple[float, float]]


def _build_scorer(
    scoring_method: str,
) -> Callable[[PeakPairs, PeakPairs], float]:
    """Return the pairwise spectrum scorer for *scoring_method*.

    ``classical`` scores matched peak intensities with greedy m/z alignment;
    the foundation-model methods score whole-spectrum embeddings produced by
    the corresponding real ``SpectralEmbedder`` adapter.
    """
    if scoring_method == "classical":
        return lambda peaks_a, peaks_b: _cosine(peaks_a, peaks_b, tolerance=0.02)

    embedder = get_embedder(scoring_method)

    def embedding_score(
        peaks_a: PeakPairs,
        peaks_b: PeakPairs,
    ) -> float:
        if not peaks_a or not peaks_b:
            return 0.0
        emb_a = embedder.embed_spectrum(np.asarray(peaks_a, dtype=np.float64))
        emb_b = embedder.embed_spectrum(np.asarray(peaks_b, dtype=np.float64))
        return _vector_cosine(emb_a, emb_b)

    return embedding_score


def _scoring_label(scoring_method: str) -> str:
    """Human-readable scoring description for report headers."""
    if scoring_method == "classical":
        return "classical (greedy peak matching, ±0.02 Da)"
    embedder = get_embedder(scoring_method)
    return (
        f"{embedder.name} deep embedding ({embedder.embedding_dim}-d, "
        f"{embedder.backend_label})"
    )


def _model_info(scoring_method: str) -> ModelInfo | None:
    """Describe the model behind an embedding-scored search, if any."""
    if scoring_method == "classical":
        return None
    embedder = get_embedder(scoring_method)
    return ModelInfo(
        name=embedder.name,
        backend=embedder.backend,
        embedding_dim=embedder.embedding_dim,
        checkpoint=os.environ.get(LSM_MS2_CKPT_ENV),
    )


def _decoy_spectrum(
    peaks: list[tuple[float, float]],
    rng: random.Random,
) -> list[tuple[float, float]]:
    """Return a decoy of *peaks* for the null score distribution.

    The decoy keeps the acquired m/z values and the intensity *distribution* but
    permutes the intensities across the m/z positions, so the fragment masses no
    longer explain the intensities.  That is the property the score is supposed
    to measure, and it is what makes the null a representable "this spectrum is
    not the right one" case.

    The previous implementation shuffled the ``(mz, intensity)`` pair list, which
    is a no-op for every scorer in this module: the classical scorer sorts the
    reference by m/z before matching, the mock embedders hash peaks
    order-independently by design, and the real DreaMS preprocessor sorts by m/z.
    The "decoy" scores were therefore bit-identical to the target scores and the
    FDR/p-value column was calibrated against a copy of the target distribution.

    A spectrum with fewer than two peaks, or with all-equal intensities, carries
    no intensity pattern to permute; callers should skip such spectra rather than
    count a decoy that equals its target.
    """
    if len(peaks) < 2:
        raise ValueError("a decoy needs at least two peaks")
    intensities = [p[1] for p in peaks]
    order = sorted(range(len(peaks)), key=lambda i: peaks[i][0])
    permuted = list(intensities)
    for _ in range(4):
        rng.shuffle(permuted)
        if permuted != intensities:
            break
    else:  # all intensities equal: rotate, which is still a no-op numerically
        return [(peaks[i][0], intensities[i]) for i in order]
    return [
        (peaks[i][0], permuted[j]) for j, i in enumerate(order)
    ]


def _permutable(peaks: list[tuple[float, float]]) -> bool:
    """True when *peaks* has an intensity pattern a decoy can actually break."""
    return len(peaks) >= 2 and len({p[1] for p in peaks}) >= 2


# ======================================================================
# FDR / p-value calculations
# ======================================================================
def _benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Return q-values via the Benjamini-Hochberg procedure."""
    n = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    q_values = [0.0] * n
    for rank, (orig_idx, p) in enumerate(indexed, start=1):
        q = min(p * n / rank, 1.0)
        q_values[orig_idx] = q
    for i in range(n - 2, -1, -1):
        q_values[indexed[i][0]] = min(
            q_values[indexed[i][0]], q_values[indexed[i + 1][0]]
        )
    return q_values


def _estimate_empirical_p(
    target_scores: list[float],
    null_scores: list[float],
) -> list[float]:
    """Estimate empirical p-values from a null score distribution.

    p = (1 + #null_scores >= target_score) / (1 + #null_scores)

    The comparison is inclusive.  Counting only *strictly greater* null scores
    (``side="right"``) makes every tied score look significant, and ties are not
    an edge case: a real library's null distribution is dominated by scores of
    exactly zero, so the strict comparison handed a passing p-value to spectra
    with no shared fragments at all.
    """
    null_arr = np.sort(np.asarray(null_scores, dtype=np.float64))
    n_null = len(null_arr)
    p_vals: list[float] = []
    for s in target_scores:
        below = np.searchsorted(null_arr, s, side="left")
        count_ge = n_null - below
        p = (1.0 + count_ge) / (1.0 + n_null)
        p_vals.append(p)
    return p_vals


# ======================================================================
# Experimental spectrum sources
# ======================================================================
def _peaks_from_reference(reference: str) -> list[tuple[float, float]]:
    """Resolve a registered spectrum or peak list into ``(mz, intensity)`` pairs.

    Resolution happens on the event loop, before the job is submitted, so a
    stale or mistyped reference fails immediately and loudly instead of
    surfacing later as a failed background job.
    """
    peaks = np.asarray(reference_store.peak_list_of(reference), dtype=np.float64)
    if peaks.ndim != 2 or peaks.shape[1] != 2 or peaks.shape[0] == 0:
        raise MsmcpError(
            f"Reference {reference!r} does not hold a usable peak list; got "
            f"shape {peaks.shape}."
        )
    return [(float(mz), float(intensity)) for mz, intensity in peaks]


def _peaks_from_file(file_path: str) -> list[tuple[float, float]]:
    """Read the first spectrum of *file_path* as a real experimental peak list.

        The file is opened through the ingestion layer, so the query of a search is
    always genuine data and a missing or malformed file fails loudly instead of
    quietly producing a synthetic stand-in.
    """
    source = ingest.resolve_source(file_path)
    for spectrum in source.iter_spectra():
        return [
            (float(mz), float(intensity))
            for mz, intensity in zip(spectrum.mz, spectrum.intensity, strict=True)
        ]
    raise MsmcpError(f"'{file_path}' contains no spectra to use as a query.")


# ======================================================================
# The search itself — CPU-bound, run on an executor worker thread
# ======================================================================
@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Everything the CPU-bound scan needs, resolved before dispatch.

    ``experimental_peaks`` is real data: the tool resolves either a file or a
    server-side reference into peaks *before* submitting the job, so the scan
    itself never touches the filesystem.
    """

    database_file: str
    experimental_peaks: tuple[tuple[float, float], ...]
    scoring_method: ScoringMethod = "classical"
    chunk_size: int = 2000
    experimental_file: str | None = None
    spectrum_reference: str | None = None


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """The report plus the provenance of the search that produced it."""

    report: str
    provenance: Any


def _library_spec(database_file: str) -> tuple[int, int]:
    """Deterministic ``(n_spectra, seed)`` for the synthetic library of a path.

    Kept as its own function so tests can rebuild the exact library a scan uses
    (e.g. to take a real library spectrum as a known true-positive query)
    without duplicating the seeding order.
    """
    rng = random.Random(_stable_seed(database_file))
    return rng.randint(500, 5000), rng.randint(0, 2**31)


def _run_scan(request: SearchRequest) -> SearchOutcome:
    """Run the full library search and return the report with its provenance.

    The library comes from :func:`_build_mock_database` (seeded from
    ``database_file``) and is *never* read from disk.  The query spectrum is
    real data resolved before dispatch.  The chunked scan, scoring,
    FDR/p-value estimation and report formatting below are all real, so the
    report is a truthful demonstration of the pipeline and is labelled as
    such.

    This function is CPU-bound; the executor runs it on a worker thread so the
    MCP event loop stays responsive.
    """
    scoring_method = request.scoring_method
    database_file = request.database_file
    chunk_size = request.chunk_size

    scorer = _build_scorer(scoring_method)
    n_spectra, library_seed = _library_spec(database_file)
    # The null draw is deterministic too, so a report is reproducible: same
    # library path in, same p-values out.
    rng = random.Random(_stable_seed(database_file) + 1)
    conn = _build_mock_database(
        n_spectra=n_spectra,
        seed=library_seed,
    )
    try:
        exp_peaks = list(request.experimental_peaks)
        if not exp_peaks:
            raise MsmcpError(
                "the search query has no peaks; a search cannot run against an "
                "empty spectrum."
            )
        logger.info("Scoring %d real query peaks", len(exp_peaks))

        small_library_threshold = 2000
        use_fdr = n_spectra >= small_library_threshold

        small_lib_warning = ""
        if not use_fdr:
            small_lib_warning = (
                f"⚠️  **SCIENTIFIC WARNING**\n"
                f"The spectral library contains only **{n_spectra}** spectra "
                f"(< {small_library_threshold} threshold).\n"
                f"Target-Decoy FDR estimation is unreliable with small "
                f"libraries.\n"
                f"→ Automatically switching to **empirical p-value** "
                f"calculation instead.\n\n"
            )

        target_scores: list[float] = []
        target_meta: list[dict[str, Any]] = []
        # Peaks of permutable library spectra, kept for the null model below so
        # the decoy draws cost no further SQL.
        peaks_cache: dict[Any, list[tuple[float, float]]] = {}

        logger.info(
            "Scanning %d spectra in chunks of %d (%s mode)",
            n_spectra,
            chunk_size,
            "FDR" if use_fdr else "p-value",
        )

        for chunk_id, chunk in _iter_spectra_chunked(conn, chunk_size):
            for spec in chunk:
                score = scorer(exp_peaks, spec["peaks"])
                target_scores.append(score)
                if _permutable(spec["peaks"]):
                    peaks_cache[spec["id"]] = list(spec["peaks"])
                target_meta.append(
                    {
                        "id": spec["id"],
                        "compound_name": spec["compound_name"],
                        "formula": spec["formula"],
                        "precursor_mz": spec["precursor_mz"],
                        "score": score,
                    }
                )
            logger.debug("Chunk %d: processed %d spectra", chunk_id, len(chunk))

        n_null = n_spectra * NULL_DECOY_MULTIPLIER
        null_scores: list[float] = []
        unpermutable = 0
        # Draw from the spectra already scored above — no extra SQL, and the
        # same library the target scores came from.
        pool = [meta["id"] for meta in target_meta if peaks_cache.get(meta["id"])]
        if pool:
            logger.info(
                "Null model: %d decoy scores (%dx the library) from %d permutable "
                "spectra",
                n_null,
                NULL_DECOY_MULTIPLIER,
                len(pool),
            )
            for _ in range(n_null):
                decoy_peaks = peaks_cache[pool[rng.randrange(len(pool))]]
                null_scores.append(scorer(exp_peaks, _decoy_spectrum(decoy_peaks, rng)))
        unpermutable = n_spectra - len(pool)
        if unpermutable:
            logger.debug(
                "Null model: %d spectra have no permutable intensity pattern",
                unpermutable,
            )
        if not null_scores:
            logger.warning(
                "Null model: no permutable spectra in a library of %d — "
                "p-values cannot be calibrated and no hit can pass",
                n_spectra,
            )

        n_null_actual = len(null_scores)
        report_threshold = 0.05

        if use_fdr:
            p_values = _estimate_empirical_p(target_scores, null_scores)
            q_values = _benjamini_hochberg(p_values)
            # The smallest q this design can express: p is floored at
            # 1/(n_null + 1) and the strongest hit is charged a factor of m.
            q_floor = (
                n_spectra / (n_null_actual + 1) if n_null_actual else 1.0
            )
            null_line = (
                f"Null model: {n_null_actual:,} decoy scores from permuted-intensity "
                f"library spectra; smallest attainable q-value **{q_floor:.2e}**."
            )
            hits = [
                {**meta, "q_value": qv}
                for meta, qv in zip(target_meta, q_values, strict=True)
                if qv <= report_threshold
            ]
            hits.sort(key=lambda h: h["score"], reverse=True)
            method_line = f"FDR threshold (Benjamini-Hochberg): {report_threshold}"
        else:
            p_values = _estimate_empirical_p(target_scores, null_scores)
            hits = [
                {**meta, "p_value": pv}
                for meta, pv in zip(target_meta, p_values, strict=True)
                if pv <= report_threshold
            ]
            hits.sort(key=lambda h: h["score"], reverse=True)
            method_line = f"Empirical p-value threshold: {report_threshold}"
            p_floor = 1.0 / (n_null_actual + 1) if n_null_actual else 1.0
            null_line = (
                f"Null model: {n_null_actual:,} decoy scores from permuted-intensity "
                f"library spectra; smallest attainable p-value **{p_floor:.2e}** "
                f"(no multiplicity correction is applied in this mode)."
            )

        top_n = min(len(hits), 20)
        lines = _report_banner(request, database_file, n_spectra)

        if request.spectrum_reference:
            lines.append(
                f"Query spectrum: reference `{request.spectrum_reference}` "
                f"({len(exp_peaks)} real peaks, held server-side)"
            )
        else:
            lines.append(
                f"Query spectrum: `{request.experimental_file}` "
                f"({len(exp_peaks)} real peaks, read from disk)"
            )
        lines.append(f"Scoring method: {_scoring_label(scoring_method)}")
        lines.append("")

        if small_lib_warning:
            lines.append(small_lib_warning)

        lines.append(method_line)
        lines.append(null_line)
        lines.append("")

        if not hits:
            lines.append(
                "**No hits passed the significance threshold.**\n\n"
                "Consider widening the precursor mass tolerance or "
                "re-acquiring the spectrum with higher signal-to-noise."
            )
        else:
            lines.append(f"Showing the top {top_n} of {len(hits)} hit(s):")
            lines.append("")
            if use_fdr:
                lines.append(
                    "| Rank | Compound         | Score  | FDR (q-value) | Precursor m/z | Formula    |"
                )
                lines.append(
                    "|------|-----------------|--------|---------------|---------------|------------|"
                )
                for i, h in enumerate(hits[:top_n], start=1):
                    lines.append(
                        f"| {i:<4} | {h['compound_name']:<15} | {h['score']:.4f} | "
                        f"{h['q_value']:.4f}       | {h['precursor_mz']:>13.4f} | "
                        f"{h['formula']:<10} |"
                    )
            else:
                lines.append(
                    "| Rank | Compound         | Score  | p-value   | Precursor m/z | Formula    |"
                )
                lines.append(
                    "|------|-----------------|--------|-----------|---------------|------------|"
                )
                for i, h in enumerate(hits[:top_n], start=1):
                    lines.append(
                        f"| {i:<4} | {h['compound_name']:<15} | {h['score']:.4f} | "
                        f"{h['p_value']:.4f}   | {h['precursor_mz']:>13.4f} | "
                        f"{h['formula']:<10} |"
                    )

            lines.append("")
            total_passing = len(hits)
            if total_passing > top_n:
                lines.append(
                    f"{total_passing} hits passed the threshold ({top_n} shown above)."
                )
            else:
                lines.append(f"{total_passing} hit(s) passed the threshold.")

        provenance = _search_provenance(request, n_spectra, len(exp_peaks))
        lines.append("")
        lines.append("---")
        lines.append(
            f"*Provenance: {provenance.operation}, method "
            f"`{scoring_method}`; query source "
            f"`{request.spectrum_reference or request.experimental_file}`.  "
            f"Library: synthetic ({n_spectra:,} spectra), not read from "
            f"`{database_file}`.*"
        )

        logger.info(
            "_run_scan(db=%r, n=%d, mode=%s) -> %d hits (top %.4f)",
            database_file,
            n_spectra,
            "FDR" if use_fdr else "p-value",
            len(hits),
            hits[0]["score"] if hits else 0.0,
        )
        return SearchOutcome(report="\n".join(lines), provenance=provenance)

    finally:
        conn.close()


def _report_banner(
    request: SearchRequest,
    database_file: str,
    n_spectra: int,
) -> list[str]:
    """Return the mandatory truthfulness banner for a search report.

    The banner precedes the hit table so it survives a host that only forwards
    the head of a tool result, and it is always phrased in terms of what is
    actually synthetic: the library, never the query.
    """
    return [
        "> ⚠️  **SYNTHETIC LIBRARY — NOT A COMPOUND IDENTIFICATION.**",
        ">",
        "> The query spectrum is real data, but the spectral library it was "
        "searched against is synthetic: MSMCP has no spectral-library reader "
        "yet, so the database path below was not opened and the library was "
        "generated in memory.",
        ">",
        "> Treat this as a demonstration of the scanning, scoring and FDR "
        "machinery applied to a real query.  Do not report these compounds "
        "as findings.",
        "",
        "## Spectral Library Search Results",
        "",
        f"Requested database: `{database_file}` — not opened "
        f"(substituted a synthetic library of {n_spectra:,} spectra)",
    ]


def _search_provenance(
    request: SearchRequest, n_spectra: int, n_query_peaks: int
) -> Provenance:
    """Build the provenance record for one search."""
    sources = []
    if request.experimental_file:
        sources.append(
            SourceRef.from_path(
                request.experimental_file,
                format=None,
                backend="msmcp.ingest",
            )
        )
    return provenance_for(
        "search_library",
        parameters={
            "scoring_method": request.scoring_method,
            "chunk_size": request.chunk_size,
            "library_synthetic": True,
            "library_spectra": n_spectra,
            "query_peaks": n_query_peaks,
            "query_is_real_data": True,
            "database_file_not_opened": request.database_file,
        },
        sources=tuple(sources),
        parents=(request.spectrum_reference,) if request.spectrum_reference else (),
        model=_model_info(request.scoring_method),
    )


# ======================================================================
# Execution
# ======================================================================
_EXECUTOR = LocalAsyncExecutor()
"""The process-wide executor that runs library searches.

Only the :class:`~msmcp.execution.executor.JobExecutor` interface is used, so
this can be swapped for a process-based or orchestrated implementation without
touching the tools below.
"""

_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def _status_lines(snapshot: JobStatusSnapshot, job_id: str) -> str:
    """Render a non-terminal or unusual job state as text for the poller."""
    if snapshot.phase == "queued":
        return (
            f"⏳ **Pending** — search job `{job_id}` has been queued and "
            f"will start shortly.  Poll again in a moment."
        )
    if snapshot.phase == "running":
        return (
            f"🔄 **Running** — search job `{job_id}` is scanning the "
            f"spectral library and computing statistics.  Poll again "
            f"shortly."
        )
    return (
        f"⚠️  **Unexpected state** — search job `{job_id}` is in status "
        f"`{snapshot.status}`.  This may indicate an internal error."
    )


def _valid_job_id(job_id: str) -> bool:
    """Whether *job_id* is syntactically a job identifier this server issues."""
    try:
        uuid.UUID(job_id)
    except ValueError:
        return False
    return True


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register the async library-search tools on the MCPServer instance."""

    # ------------------------------------------------------------------
    # Tool 1 — Dispatcher
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Search a spectral library (async)",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def search_library(
        database_file: Annotated[
            str,
            Field(
                min_length=1,
                description="Path identifying the spectral library.  Not read "
                "yet: the path only seeds the synthetic library.",
            ),
        ],
        experimental_file: Annotated[
            str | None,
            Field(
                description="Path to the experimental spectrum file, read for "
                "real through the ingestion layer.  Provide this or "
                "spectrum_reference, not both.",
            ),
        ] = None,
        spectrum_reference: Annotated[
            str | None,
            Field(
                description="A spectrum reference from load_spectrum, used as "
                "the query instead of a file path.  Provide this or "
                "experimental_file, not both.",
            ),
        ] = None,
        scoring_method: Annotated[
            ScoringMethod,
            Field(
                description="'classical' is greedy peak matching; 'dreams' and "
                "'lsm-ms2' are real foundation-model embeddings, which need the "
                "optional ML backend installed or the job fails.",
            ),
        ] = "classical",
        chunk_size: Annotated[
            int,
            Field(
                ge=100,
                le=10000,
                description="Spectra read per database batch.  Affects only "
                "memory and I/O batching, not the result.",
            ),
        ] = 2000,
    ) -> str:
        """Start a background spectral-library search and return a job ID.

        IMPORTANT: the **library** is synthetic.  MSMCP has no spectral-library
        reader, so `database_file` is not opened; a deterministic in-memory
        library is generated from that string instead.  The report repeats this
        warning - never treat its hits as compound identifications.

        The **query** spectrum can be real: pass `spectrum_reference` from
        `load_spectrum` (peaks already server-side) or `experimental_file` to
        read one from disk.  The chunked scan, scoring and FDR estimation are
        real in every case.

        Returns immediately with a job ID, because the scan takes far longer
        than a request timeout.  Poll `check_search_status` with that ID to
        collect the report.  This tool does not return results directly.
        """
        validated = SearchInput(
            experimental_file=experimental_file,
            spectrum_reference=spectrum_reference,
            database_file=database_file,
            scoring_method=scoring_method,
            chunk_size=chunk_size,
        )

        # Resolve real query data *before* dispatch, so a bad reference or an
        # unreadable file fails here and now rather than inside a job.
        exp_peaks: tuple[tuple[float, float], ...] = ()
        if validated.spectrum_reference is not None:
            exp_peaks = tuple(_peaks_from_reference(validated.spectrum_reference))
        elif validated.experimental_file is not None:
            exp_peaks = tuple(_peaks_from_file(validated.experimental_file))

        request = SearchRequest(
            database_file=validated.database_file,
            experimental_peaks=exp_peaks,
            scoring_method=validated.scoring_method,
            chunk_size=validated.chunk_size,
            experimental_file=validated.experimental_file,
            spectrum_reference=validated.spectrum_reference,
        )

        handle = _EXECUTOR.submit(
            "search_library",
            lambda: _run_scan(request),
            parameters={
                "database_file": validated.database_file,
                "scoring_method": validated.scoring_method,
                "chunk_size": validated.chunk_size,
                "spectrum_reference": validated.spectrum_reference,
                "experimental_file": validated.experimental_file,
            },
        )

        logger.info(
            "Dispatched search job %s (db=%r, method=%s, query_peaks=%d)",
            handle.job_id,
            validated.database_file,
            validated.scoring_method,
            len(exp_peaks),
        )

        return (
            f"## Search Dispatched\n\n"
            f"**Job ID:** `{handle.job_id}`\n\n"
            f"**Query:** {validated.spectrum_reference or validated.experimental_file}"
            f" ({len(exp_peaks)} real peaks)\n\n"
            f"⚠️  The **library** is **synthetic** — `{validated.database_file}` "
            f"is not read.  The final report repeats this warning — do not "
            f"treat its hits as compound identifications.\n\n"
            f"Poll for the report with:\n\n"
            f'    check_search_status(job_id="{handle.job_id}")\n'
            f'    cancel_search(job_id="{handle.job_id}")\n'
        )

    # ------------------------------------------------------------------
    # Tool 2 — Poller
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Check search status",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def check_search_status(
        job_id: Annotated[
            str,
            Field(
                min_length=1,
                description="The job ID returned by search_library.",
            ),
        ],
    ) -> str:
        """Fetch the current state, or the final report, of a background search.

        Pass the job ID returned by `search_library`.  While the job is queued
        or scanning this returns a short status line, so poll again after a
        pause.  Once the job finishes it returns the full Markdown report, or
        a description of the failure.

        A job the server no longer knows about (for example after a restart)
        is reported as failed rather than as pending, so it is always safe to
        keep polling.
        """
        _ = StatusInput(job_id=job_id)

        if not _valid_job_id(job_id):
            return (
                f"❓ **Unknown Job**\n\n"
                f"`{job_id}` is not a valid job ID (expected a 32-character "
                f"hex UUID).  Double-check the job ID returned by "
                f"`search_library`."
            )

        try:
            snapshot = _EXECUTOR.status(job_id)
        except UnknownJobError:
            return (
                f"❌ **Failed (not found)** — no search job `{job_id}` exists "
                f"in this server process.\n\n"
                f"Job state is held in memory and does **not** survive a server "
                f"restart.  If the server restarted after this job ID was "
                f"returned, the job has been lost and must be treated as "
                f"failed — re-dispatch it with `search_library`."
            )

        if snapshot.status not in _TERMINAL:
            return _status_lines(snapshot, job_id)

        outcome = _EXECUTOR.result(job_id)

        if outcome.status == "completed":
            logger.info("check_search_status(%s): returning completed report", job_id)
            report = outcome.value.report if outcome.value is not None else None
            return report or "ERROR: job completed without a report."

        if outcome.status == "failed":
            logger.info("check_search_status(%s): reporting failure", job_id)
            return (
                f"❌ **Failed** — search job `{job_id}` failed:\n\n"
                f"```\n{outcome.traceback}\n```"
            )

        logger.info("check_search_status(%s): reporting cancellation", job_id)
        return (
            f"🚫 **Cancelled** — search job `{job_id}` was cancelled by the "
            f"client.\n\n"
            f"Any partial work was discarded; dispatch a new search if "
            f"needed."
        )

    # ------------------------------------------------------------------
    # Tool 3 — Cancellation
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Cancel a search",
        annotations=ToolAnnotations(
            read_only_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def cancel_search(
        job_id: Annotated[
            str,
            Field(
                min_length=1,
                description="The job ID returned by search_library.",
            ),
        ],
    ) -> str:
        """Abort a queued or running background search.

        Pass the job ID returned by `search_library`.  A job that has already
        finished is reported as such rather than cancelled.  A scan that has
        already started may not stop instantly, but its partial work is
        discarded instead of being returned.

        Poll `check_search_status` afterwards to confirm the job reached the
        cancelled state.
        """
        _ = StatusInput(job_id=job_id)

        if not _valid_job_id(job_id):
            return (
                f"❓ **Unknown Job**\n\n"
                f"`{job_id}` is not a valid job ID (expected a 32-character "
                f"hex UUID)."
            )

        try:
            before = _EXECUTOR.status(job_id)
        except UnknownJobError:
            return (
                f"❓ **Unknown Job** — no search job found with ID `{job_id}`.  "
                f"Double-check the job ID or dispatch a new search."
            )

        if before.is_terminal:
            return (
                f"⚠️ **Not Cancelled** — search job `{job_id}` is already "
                f"`{before.status}` and cannot be cancelled."
            )

        _EXECUTOR.cancel(job_id)
        logger.info("Cancellation requested for search job %s", job_id)
        return (
            f"## Search Cancelled\n\n"
            f"**Job ID:** `{job_id}`\n\n"
            f"Cancellation requested.  Poll `check_search_status` to confirm "
            f"the job reaches the `cancelled` state."
        )
