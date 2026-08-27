"""Spectral library search orchestrated with an in-process async job store.

``search_library`` records each job in ``_JOB_STORE`` and executes the
CPU-bound scan off the event loop with ``asyncio.to_thread``; the job runs
as an ``asyncio`` task spawned via ``asyncio.create_task``, and
``check_search_status`` polls the job store until the job completes.

The dispatcher/poller contract is thus fully in-process: no external
orchestrator is required, and the MCP event loop stays responsive while
searches execute on a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import traceback
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from msmcp.models import get_embedder
from msmcp.tools.similarity import _cosine as _vector_cosine

logger = logging.getLogger("msmcp.tools.search")


# ======================================================================
# Pydantic schemas
# ======================================================================
class SearchInput(BaseModel):
    """Validated input for the search_library tool."""

    experimental_file: str = Field(
        ...,
        min_length=1,
        description="Path to the experimental spectrum file (.mzML, .mgf).",
    )
    database_file: str = Field(
        ...,
        min_length=1,
        description="Path to the SQLite-backed spectral library (.db).",
    )
    scoring_method: Literal["classical", "dreams", "lsm-ms2"] = Field(
        default="classical",
        description=(
            "Scoring method: 'classical' greedy peak matching, or deep "
            "foundation-model embeddings ('dreams' / 'lsm-ms2')."
        ),
    )
    chunk_size: int = Field(
        default=2000,
        ge=100,
        le=10000,
        description="Number of spectra to read per database I/O batch.",
    )


class StatusInput(BaseModel):
    """Validated input for the check_search_status tool."""

    job_id: str = Field(
        ...,
        min_length=1,
        description="The job_id (uuid4 hex) returned by a previous "
        "search_library call.",
    )


# ======================================================================
# Job store — in-process state for dispatched searches
# ======================================================================
@dataclass
class SearchJob:
    """Mutable state for a single dispatched search job.

    Status lifecycle: ``pending`` → ``running`` → ``completed`` | ``failed``.
    """

    job_id: str
    experimental_file: str
    database_file: str
    scoring_method: str
    status: str = "pending"
    result: str | None = None  # final Markdown report once completed
    error: str | None = None  # formatted traceback once failed
    task: asyncio.Task[Any] | None = None  # strong ref — prevents GC mid-run


_JOB_STORE: dict[str, SearchJob] = {}
"""In-process job registry keyed by ``uuid.uuid4().hex`` job ID."""


def _stable_seed(text: str) -> int:
    """Deterministic 31-bit seed derived from *text*.

    ``hash()`` is salted per process (``PYTHONHASHSEED``), so it must not
    be used for reproducible seeding; CRC32 is stable across runs.
    """
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


# ======================================================================
# Mock spectral library (in-memory SQLite)
# ======================================================================
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
    logger.info("Built mock library: %d spectra", n_spectra)
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
    """Cosine similarity between two peak lists with m/z tolerance."""
    if not peaks_a or not peaks_b:
        return 0.0

    b_sorted = sorted(peaks_b, key=lambda p: p[0])
    b_mz = np.array([p[0] for p in b_sorted], dtype=np.float64)
    b_int = np.array([p[1] for p in b_sorted], dtype=np.float64)

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
    dot = np.dot(a, b)
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(dot / (na * nb))


# ======================================================================
# Scorer routing — classical peak matching vs. foundation-model embeddings
# ======================================================================
PeakPairs = list[tuple[float, float]]


def _build_scorer(
    scoring_method: str,
) -> Callable[[PeakPairs, PeakPairs], float]:
    """Return the pairwise spectrum scorer for *scoring_method*.

    ``classical`` scores matched peak intensities with greedy m/z alignment;
    the foundation-model methods score whole-spectrum 1024-dimensional
    embeddings produced by the corresponding ``SpectralEmbedder`` adapter.
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
    backend_label = (
        "real inference" if embedder.backend == "hf" else "deterministic fallback"
    )
    return (
        f"{embedder.name} deep embedding ({embedder.embedding_dim}-d, {backend_label})"
    )


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

    p = (1 + #null_scores ≥ target_score) / (1 + #null_scores)
    """
    null_arr = np.sort(np.asarray(null_scores, dtype=np.float64))
    n_null = len(null_arr)
    p_vals: list[float] = []
    for s in target_scores:
        exceed = np.searchsorted(null_arr, s, side="right")
        count_above = n_null - exceed
        p = (1.0 + count_above) / (1.0 + n_null)
        p_vals.append(p)
    return p_vals


# ======================================================================
# Experimental spectrum mock loader
# ======================================================================
def _mock_load_experimental(
    file_path: str,
    rng: random.Random | None = None,
) -> list[tuple[float, float]]:
    """Return a synthetic experimental peak list from a file path."""
    if rng is None:
        rng = random.Random(_stable_seed(file_path))
    precursor = rng.uniform(180.0, 900.0)
    return _generate_peak_list(precursor, rng.randint(15, 50), rng)


# ======================================================================
# The search itself — CPU-bound, offloaded to a worker thread
# ======================================================================
def _build_report(
    experimental_file: str,
    database_file: str,
    scoring_method: Literal["classical", "dreams", "lsm-ms2"] = "classical",
    chunk_size: int = 2000,
) -> str:
    """Run the full library search and return a formatted Markdown report.

    Similarity is computed with the scorer selected by *scoring_method*:
    classical greedy peak matching, or whole-spectrum embeddings from the
    DreaMS / LSM-MS2 foundation-model adapters.  The database is scanned in
    batches of *chunk_size* spectra to bound per-transaction I/O and memory.

    This function is CPU-bound (chunked scan of the SQLite library plus
    null-distribution scoring); the dispatcher runs it via
    ``asyncio.to_thread`` so the MCP event loop stays responsive.
    """
    scorer = _build_scorer(scoring_method)
    rng = random.Random(_stable_seed(database_file))
    n_spectra = rng.randint(500, 5000)
    conn = _build_mock_database(
        n_spectra=n_spectra,
        seed=rng.randint(0, 2**31),
    )
    try:
        # --- load experimental spectrum -------------------------------------
        exp_peaks = _mock_load_experimental(
            experimental_file,
            random.Random(rng.randint(0, 2**31)),
        )
        logger.info(
            "Loaded experimental spectrum: %d peaks from %r",
            len(exp_peaks),
            experimental_file,
        )

        # --- small-library guard --------------------------------------------
        SMALL_LIBRARY_THRESHOLD = 2000
        use_fdr = n_spectra >= SMALL_LIBRARY_THRESHOLD

        small_lib_warning = ""
        if not use_fdr:
            small_lib_warning = (
                f"⚠️  **SCIENTIFIC WARNING**\n"
                f"The spectral library contains only **{n_spectra}** spectra "
                f"(< {SMALL_LIBRARY_THRESHOLD} threshold).\n"
                f"Target-Decoy FDR estimation is unreliable with small "
                f"libraries.\n"
                f"→ Automatically switching to **empirical p-value** "
                f"calculation instead.\n\n"
            )

        # --- chunked search -------------------------------------------------
        target_scores: list[float] = []
        target_meta: list[dict[str, Any]] = []

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

        # --- null distribution (decoy scores) -------------------------------
        n_null = n_spectra
        null_scores: list[float] = []
        for _ in range(n_null):
            spec_idx = rng.randrange(len(target_meta))
            orig_peaks = conn.execute(
                "SELECT mz, intensity FROM peaks WHERE spectrum_id=?",
                (target_meta[spec_idx]["id"],),
            ).fetchall()
            shuffled = [(p[0], p[1]) for p in orig_peaks]
            rng.shuffle(shuffled)
            null_scores.append(scorer(exp_peaks, shuffled))

        # --- FDR or p-value calculation -------------------------------------
        REPORT_THRESHOLD = 0.05

        if use_fdr:
            p_values = _estimate_empirical_p(target_scores, null_scores)
            q_values = _benjamini_hochberg(p_values)

            hits = [
                {**meta, "q_value": qv}
                for meta, qv in zip(target_meta, q_values, strict=True)
                if qv <= REPORT_THRESHOLD
            ]
            hits.sort(key=lambda h: h["score"], reverse=True)
            method_line = f"FDR threshold (Benjamini-Hochberg): {REPORT_THRESHOLD}"
        else:
            p_values = _estimate_empirical_p(target_scores, null_scores)

            hits = [
                {**meta, "p_value": pv}
                for meta, pv in zip(target_meta, p_values, strict=True)
                if pv <= REPORT_THRESHOLD
            ]
            hits.sort(key=lambda h: h["score"], reverse=True)
            method_line = f"Empirical p-value threshold: {REPORT_THRESHOLD}"

        # --- format output --------------------------------------------------
        top_n = min(len(hits), 20)

        lines = [
            "## Spectral Library Search Results",
            "",
            f"Database: `{database_file}`",
            f"Experimental file: `{experimental_file}`",
            f"Library size: {n_spectra:,} spectra",
            f"Experimental peaks: {len(exp_peaks)}",
            f"Scoring method: {_scoring_label(scoring_method)}",
            "",
        ]

        if small_lib_warning:
            lines.append(small_lib_warning)

        lines.append(method_line)
        lines.append("")

        if not hits:
            lines.append(
                "**No hits passed the significance threshold.**\n\n"
                "Consider widening the precursor mass tolerance or "
                "re-acquiring the spectrum with higher signal-to-noise."
            )
        else:
            lines.append(f"Top {top_n} hit(s):")
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
                        f"| {i:<4} | {h['compound_name']:<15} | {h['score']:.4f} | {h['q_value']:.4f}       | {h['precursor_mz']:>13.4f} | {h['formula']:<10} |"
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
                        f"| {i:<4} | {h['compound_name']:<15} | {h['score']:.4f} | {h['p_value']:.4f}   | {h['precursor_mz']:>13.4f} | {h['formula']:<10} |"
                    )

            lines.append("")
            total_passing = len(hits)
            if total_passing > top_n:
                lines.append(
                    f"{total_passing} hits passed the threshold ({top_n} shown above)."
                )
            else:
                lines.append(f"{total_passing} hit(s) passed the threshold.")

        logger.info(
            "_build_report(db=%r, n=%d, mode=%s) → %d hits (top %.4f)",
            database_file,
            n_spectra,
            "FDR" if use_fdr else "p-value",
            len(hits),
            hits[0]["score"] if hits else 0.0,
        )

        return "\n".join(lines)

    finally:
        conn.close()


# ======================================================================
# Background job execution
# ======================================================================
# Strong references to fire-and-forget background tasks so the garbage
# collector never reaps a running coroutine (RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _track_background_task(task: asyncio.Task[None]) -> None:
    """Retain a strong reference to *task* until it completes."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _schedule_cleanup(job_id: str, delay_sec: int = 3600) -> None:
    """Expire *job_id* from the job store after *delay_sec* seconds.

    Finished jobs are retained briefly so ``check_search_status`` can still
    return the outcome, then removed so a long-running server does not
    accumulate unbounded in-process state.
    """
    await asyncio.sleep(delay_sec)
    job = _JOB_STORE.pop(job_id, None)
    if job is not None:
        logger.info("Expired finished search job %s from the job store", job_id)
    else:
        logger.debug("Cleanup for search job %s: already absent from store", job_id)


async def _run_search_task(
    job_id: str,
    experimental_file: str,
    database_file: str,
    scoring_method: Literal["classical", "dreams", "lsm-ms2"],
    chunk_size: int = 2000,
) -> None:
    """Execute the search for *job_id* and record the outcome in the store.

    The CPU-bound report generation runs via ``asyncio.to_thread`` so the
    event loop is never blocked; the job status transitions
    ``running`` → ``completed`` (with the report) or ``failed`` (with a
    formatted traceback).
    """
    job = _JOB_STORE[job_id]
    try:
        job.status = "running"
        job.result = await asyncio.to_thread(
            _build_report,
            experimental_file,
            database_file,
            scoring_method,
            chunk_size,
        )
        job.status = "completed"
        logger.info(
            "Search job %s completed (db=%r, method=%s)",
            job_id,
            database_file,
            scoring_method,
        )
    except Exception as exc:
        logger.exception("Search job %s failed", job_id)
        job.error = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        job.status = "failed"
    finally:
        job.task = None  # finished — drop the strong reference
        # Expire the finished job from the store after the TTL so a
        # long-running server never leaks completed/failed job records.
        _track_background_task(asyncio.create_task(_schedule_cleanup(job_id)))


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register the async library-search tools on the MCPServer instance."""

    # ------------------------------------------------------------------
    # Tool 1 — Dispatcher
    # ------------------------------------------------------------------
    @mcp.tool()
    async def search_library(
        experimental_file: str,
        database_file: str,
        scoring_method: Literal["classical", "dreams", "lsm-ms2"] = "classical",
        chunk_size: int = 2000,
    ) -> str:
        """Dispatch a spectral library search and return a job_id for polling.

        Similarity is scored with the method selected by *scoring_method*:
        'classical' greedy peak matching, or whole-spectrum embeddings from
        the 'dreams' / 'lsm-ms2' foundation-model adapters.

        The job is recorded in the in-process ``_JOB_STORE`` and executed in
        the background: ``asyncio.create_task`` spawns ``_run_search_task``,
        which offloads the CPU-bound scan with ``asyncio.to_thread`` so the
        MCP event loop stays responsive.  Use ``check_search_status`` with
        the returned *job_id* to retrieve the report once it completes.
        """
        _ = SearchInput(
            experimental_file=experimental_file,
            database_file=database_file,
            scoring_method=scoring_method,
            chunk_size=chunk_size,
        )

        job_id = uuid.uuid4().hex
        job = SearchJob(
            job_id=job_id,
            experimental_file=experimental_file,
            database_file=database_file,
            scoring_method=scoring_method,
        )
        _JOB_STORE[job_id] = job
        job.task = asyncio.create_task(
            _run_search_task(
                job_id,
                experimental_file,
                database_file,
                scoring_method,
                chunk_size,
            )
        )

        logger.info(
            "Dispatched search job %s (exp=%r, db=%r, method=%s)",
            job_id,
            experimental_file,
            database_file,
            scoring_method,
        )

        return (
            f"## Search Dispatched\n\n"
            f"**Job ID:** `{job_id}`\n\n"
            f"The spectral library search is running in the background as an "
            f"in-process async job; the CPU-bound scan is offloaded to a "
            f"worker thread so the MCP server stays responsive.\n\n"
            f"Use `check_search_status` with this job ID to poll for results:\n\n"
            f'    check_search_status(job_id="{job_id}")\n'
        )

    # ------------------------------------------------------------------
    # Tool 2 — Poller
    # ------------------------------------------------------------------
    @mcp.tool()
    async def check_search_status(job_id: str) -> str:
        """Poll the status of a previously dispatched search job.

        The *job_id* is the uuid4-hex ID returned by ``search_library``.
        This tool reads the in-process job store and returns:
        - A "wait" message while the job is pending or running.
        - The full Markdown hit table once the job completes.
        - The exception traceback if the job failed.
        """
        _ = StatusInput(job_id=job_id)

        try:
            uuid.UUID(job_id)
        except ValueError:
            return (
                f"❓ **Unknown Job**\n\n"
                f"`{job_id}` is not a valid job ID (expected a 32-character "
                f"hex UUID).  Double-check the job ID returned by "
                f"`search_library`."
            )

        job = _JOB_STORE.get(job_id)
        if job is None:
            return (
                f"❓ **Unknown Job**\n\n"
                f"No search job found with ID `{job_id}`.  Double-check the "
                f"job ID or dispatch a new search via `search_library`."
            )

        # --- pending / running ------------------------------------------
        if job.status == "pending":
            return (
                f"⏳ **Pending** — search job `{job_id}` has been queued and "
                f"will start shortly.  Poll again in a moment."
            )
        if job.status == "running":
            return (
                f"🔄 **Running** — search job `{job_id}` is scanning the "
                f"spectral library and computing statistics.  Poll again "
                f"shortly."
            )

        # --- completed --------------------------------------------------
        if job.status == "completed":
            logger.info("check_search_status(%s): returning completed report", job_id)
            return job.result or "ERROR: job completed without a report."

        # --- failed -----------------------------------------------------
        if job.status == "failed":
            logger.info("check_search_status(%s): reporting failure", job_id)
            return (
                f"❌ **Failed** — search job `{job_id}` failed:\n\n"
                f"```\n{job.error}\n```"
            )

        # --- anything else -----------------------------------------------
        return (
            f"⚠️  **Unexpected state** — search job `{job_id}` is in status "
            f"`{job.status}`.  This may indicate an internal error."
        )
