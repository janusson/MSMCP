"""Spectral library search orchestrated durably with Prefect.

Jobs are dispatched as Prefect flow runs instead of ad-hoc asyncio tasks:

* With a Prefect API configured (``PREFECT_API_URL`` or a local
  ``prefect server start``), flow runs are recorded in the server database,
  survive restarts, are observable in the Prefect UI, and can be executed by
  remote workers (multi-process scaling).
* Without an API, Prefect launches an embedded ephemeral server in a
  subprocess, so the dispatcher/poller contract is identical during local
  development.  State is process-local in that mode.

The flow exposes observable lineage: database generation and experimental
peak loading run as Prefect tasks inside the flow run.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import traceback
import uuid
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
from prefect import flow, get_client, task
from prefect.exceptions import ObjectNotFound
from prefect.flow_engine import run_flow
from prefect.states import Pending, aget_state_exception
from pydantic import BaseModel, Field

from msmcp.models.embeddings import get_embedder
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


class StatusInput(BaseModel):
    """Validated input for the check_search_status tool."""

    job_id: str = Field(
        ...,
        min_length=1,
        description="The job_id (Prefect flow run ID) returned by a previous "
        "search_library call.",
    )


# ======================================================================
# Background flow-run bookkeeping
# ======================================================================
# In embedded-server mode this process acts as the "worker": each dispatched
# flow run is executed by a background thread.  Keep strong references to the
# executor futures so the event loop never garbage-collects a running search.
_ACTIVE_SEARCH_RUNS: set[asyncio.Future[Any]] = set()


def _reap_search_future(future: asyncio.Future[Any]) -> None:
    """Drop the finished future, surfacing unexpected executor failures."""
    _ACTIVE_SEARCH_RUNS.discard(future)
    if not future.cancelled():
        exc = future.exception()
        if exc is not None:
            # The flow failure itself is recorded in the Prefect state;
            # this only fires for failures outside the flow engine.
            logger.warning("Background flow execution crashed: %s", exc)


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
    chunk_size: int = 500,
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
    return f"{embedder.name} deep embedding ({embedder.embedding_dim}-d)"


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
        rng = random.Random(hash(file_path) & 0x7FFFFFFF)
    precursor = rng.uniform(180.0, 900.0)
    return _generate_peak_list(precursor, rng.randint(15, 50), rng)


# ======================================================================
# Prefect tasks — observable lineage inside the flow run
# ======================================================================
@task(name="generate-spectral-library", persist_result=False)
def _generate_library_task(n_spectra: int, seed: int) -> sqlite3.Connection:
    """Task: build the (mock) SQLite spectral library."""
    return _build_mock_database(n_spectra=n_spectra, seed=seed)


@task(name="load-experimental-spectrum", persist_result=False)
def _load_experimental_spectrum_task(
    file_path: str,
    seed: int,
) -> list[tuple[float, float]]:
    """Task: load the experimental spectrum peak list."""
    return _mock_load_experimental(file_path, random.Random(seed))


# ======================================================================
# Prefect flow — the durable search job
# ======================================================================
@flow(
    name="Spectral Library Search",
    persist_result=True,
)
def spectral_library_search(
    experimental_file: str,
    database_file: str,
    scoring_method: Literal["classical", "dreams", "lsm-ms2"] = "classical",
) -> str:
    """Perform the full library search and return a formatted Markdown report.

    Similarity is computed with the scorer selected by *scoring_method*:
    classical greedy peak matching, or whole-spectrum embeddings from the
    DreaMS / LSM-MS2 foundation-model adapters.

    The report is persisted as the flow run result, so it can be retrieved
    from the Prefect state by ``check_search_status`` (and survives server
    restarts when a durable Prefect API is configured).
    """
    scorer = _build_scorer(scoring_method)
    rng = random.Random(hash(database_file) & 0x7FFFFFFF)
    n_spectra = rng.randint(500, 5000)
    conn = _generate_library_task(
        n_spectra=n_spectra,
        seed=rng.randint(0, 2**31),
    )
    try:
        # --- load experimental spectrum -------------------------------------
        exp_peaks = _load_experimental_spectrum_task(
            experimental_file,
            rng.randint(0, 2**31),
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

        chunk_size = 500
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
            "spectral_library_search(db=%r, n=%d, mode=%s) → %d hits (top %.4f)",
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
# Dispatch helpers
# ======================================================================
async def _dispatch_flow_run(
    experimental_file: str,
    database_file: str,
    scoring_method: str,
) -> uuid.UUID:
    """Create a Prefect flow run and start executing it in the background.

    The flow run is created through the Prefect client, which records it in
    the server database (or the embedded ephemeral server in development).
    Execution is offloaded to a thread-pool executor so the MCP event loop is
    never blocked by the CPU-bound scan.

    Returns
    -------
    uuid.UUID
        The Prefect flow run ID, returned to the caller as the ``job_id``.
    """
    parameters: dict[str, Any] = {
        "experimental_file": experimental_file,
        "database_file": database_file,
        "scoring_method": scoring_method,
    }

    async with get_client() as client:
        flow_run = await client.create_flow_run(
            flow=spectral_library_search,
            parameters=spectral_library_search.serialize_parameters(parameters),
            state=Pending(),
        )

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None,
        lambda: run_flow(
            flow=spectral_library_search,
            flow_run=flow_run,
            parameters=parameters,
            return_type="state",
        ),
    )
    _ACTIVE_SEARCH_RUNS.add(future)
    future.add_done_callback(_reap_search_future)

    logger.info(
        "Dispatched Prefect flow run %s (exp=%r, db=%r, method=%s)",
        flow_run.id,
        experimental_file,
        database_file,
        scoring_method,
    )
    return flow_run.id


# ======================================================================
# Public registration
# ======================================================================
def register_tools(mcp: Any) -> None:
    """Register the async library-search tools on the FastMCP instance."""

    # ------------------------------------------------------------------
    # Tool 1 — Dispatcher
    # ------------------------------------------------------------------
    @mcp.tool()
    async def search_library(
        experimental_file: str,
        database_file: str,
        scoring_method: Literal["classical", "dreams", "lsm-ms2"] = "classical",
    ) -> str:
        """Dispatch a spectral library search and return a job_id for polling.

        Similarity is scored with the method selected by *scoring_method*:
        'classical' greedy peak matching, or whole-spectrum embeddings from
        the 'dreams' / 'lsm-ms2' foundation-model adapters.

        The search is executed as a Prefect flow run, orchestrated durably by
        Prefect: the run is recorded in the Prefect API (or the embedded
        ephemeral server during development), its state and result survive
        the request, and it can be monitored in the Prefect UI.  Use
        ``check_search_status`` with the returned *job_id* to retrieve the
        report once the run completes.
        """
        _ = SearchInput(
            experimental_file=experimental_file,
            database_file=database_file,
            scoring_method=scoring_method,
        )

        try:
            flow_run_id = await _dispatch_flow_run(
                experimental_file,
                database_file,
                scoring_method,
            )
        except Exception as exc:
            logger.exception("Failed to dispatch search flow run")
            return (
                f"ERROR: could not dispatch the search job: {exc}\n\n"
                f"Check that the Prefect orchestration layer is reachable."
            )

        job_id = str(flow_run_id)
        return (
            f"## Search Dispatched\n\n"
            f"**Job ID:** `{job_id}`\n\n"
            f"The spectral library search is running as a Prefect flow run, "
            f"orchestrated durably by Prefect.  Its state is tracked by the "
            f"Prefect API, survives server restarts, and is visible in the "
            f"Prefect UI (search by flow run ID).\n\n"
            f"Use `check_search_status` with this job ID to poll for results:\n\n"
            f'    check_search_status(job_id="{job_id}")\n'
        )

    # ------------------------------------------------------------------
    # Tool 2 — Poller
    # ------------------------------------------------------------------
    @mcp.tool()
    async def check_search_status(job_id: str) -> str:
        """Poll the status of a previously dispatched search job.

        The *job_id* is a Prefect flow run ID.  This tool queries the Prefect
        client and returns:
        - A "wait" message if the flow run is still pending or running.
        - The full Markdown hit table when the run completes.
        - The exception traceback if the run failed.
        """
        _ = StatusInput(job_id=job_id)

        try:
            flow_run_id = uuid.UUID(job_id)
        except ValueError:
            return (
                f"❓ **Unknown Job**\n\n"
                f"`{job_id}` is not a valid Prefect flow run ID.  "
                f"Double-check the job ID returned by `search_library`."
            )

        try:
            async with get_client() as client:
                flow_run = await client.read_flow_run(flow_run_id)
        except ObjectNotFound:
            return (
                f"❓ **Unknown Job**\n\n"
                f"No Prefect flow run found with ID `{job_id}`.  "
                f"Double-check the job ID or dispatch a new search via "
                f"`search_library`."
            )
        except Exception as exc:
            logger.warning("Prefect client query failed: %s", exc)
            return (
                f"❌ **Orchestrator unavailable** — could not reach the "
                f"Prefect API: {exc}\n\n"
                f"Verify that the Prefect server is running."
            )

        state = flow_run.state

        # --- running / pending -----------------------------------------
        if (
            state is None
            or state.is_pending()
            or state.is_scheduled()
            or state.is_running()
        ):
            if state is not None and state.is_running():
                return (
                    f"🔄 **Running** — Prefect flow run `{job_id}` is "
                    f"scanning the spectral library and computing statistics.  "
                    f"Poll again shortly."
                )
            return (
                f"⏳ **Pending** — Prefect flow run `{job_id}` has been "
                f"queued and will start shortly.  Poll again in a moment."
            )

        # --- completed --------------------------------------------------
        if state.is_completed():
            try:
                result = await state.result(raise_on_failure=True)
            except Exception as exc:
                logger.warning("Result retrieval failed for %s: %s", job_id, exc)
                return (
                    f"❌ **Result unavailable** — Prefect flow run `{job_id}` "
                    f"completed, but its result could not be retrieved: {exc}"
                )
            logger.info("check_search_status(%s): returning completed report", job_id)
            return str(result)

        # --- failed / crashed / cancelled --------------------------------
        if state.is_failed() or state.is_crashed() or state.is_cancelled():
            failure = await aget_state_exception(state)
            tb_text = "".join(
                traceback.format_exception(
                    type(failure), failure, failure.__traceback__
                )
            )
            logger.info("check_search_status(%s): reporting failure", job_id)
            return (
                f"❌ **Failed** — Prefect flow run `{job_id}` ended in state "
                f"`{state.type.value}`:\n\n"
                f"```\n{tb_text}\n```"
            )

        # --- anything else -----------------------------------------------
        return (
            f"⚠️  **Unexpected state** — Prefect flow run `{job_id}` is in "
            f"state `{state.type.value}`.  This may indicate an internal "
            f"orchestration issue."
        )
