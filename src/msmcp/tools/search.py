"""Spectral library search with async dispatch, chunked iteration, FDR, and p-value fallback."""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import uuid
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

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


class StatusInput(BaseModel):
    """Validated input for the check_search_status tool."""

    job_id: str = Field(
        ...,
        min_length=1,
        description="The job_id returned by a previous search_library call.",
    )


# ======================================================================
# Async job store — maps job_id → {status, result, error, …}
# ======================================================================
_JOB_STORE: dict[str, dict[str, Any]] = {}
"""Module-level registry for tracking background search tasks.

Each entry has keys:
    status   – "pending" | "running" | "completed" | "failed"
    result   – str | None  (the formatted Markdown report)
    error    – str | None  (traceback message on failure)
"""

# ======================================================================
# Mock spectral library (in-memory SQLite)
# ======================================================================
_COMPOUNDS: list[tuple[str, str, float]] = [
    ("Caffeine",         "C8H10N4O2",   194.0804),
    ("Theobromine",      "C7H8N4O2",    180.0647),
    ("Theophylline",     "C7H8N4O2",    180.0647),
    ("Paraxanthine",     "C7H8N4O2",    180.0647),
    ("Glucose",          "C6H12O6",     180.0634),
    ("Fructose",         "C6H12O6",     180.0634),
    ("Sucrose",          "C12H22O11",   342.1162),
    ("Lactose",          "C12H22O11",   342.1162),
    ("Aspirin",          "C9H8O4",      180.0423),
    ("Ibuprofen",        "C13H18O2",    206.1307),
    ("Acetaminophen",    "C8H9NO2",     151.0633),
    ("Diazepam",         "C16H13ClN2O", 284.0716),
    ("Morphine",         "C17H19NO3",   285.1365),
    ("Codeine",          "C18H21NO3",   299.1521),
    ("Cocaine",          "C17H21NO4",   303.1471),
    ("Nicotine",         "C10H14N2",    162.1157),
    ("Serotonin",        "C10H12N2O",   176.0950),
    ("Dopamine",         "C8H11NO2",    153.0790),
    ("Epinephrine",      "C9H13NO3",    183.0895),
    ("Histamine",        "C5H9N3",      111.0796),
    ("Atropine",         "C17H23NO3",   289.1678),
    ("Quinine",          "C20H24N2O2",  324.1838),
    ("Reserpine",        "C33H40N2O9",  608.2734),
    ("Penicillin G",     "C16H18N2O4S", 334.0987),
    ("Tetracycline",     "C22H24N2O8",  444.1533),
    ("Erythromycin",     "C37H67NO13",  733.4612),
    ("Chloramphenicol",  "C11H12Cl2N2O5", 322.0123),
    ("Warfarin",         "C19H16O4",    308.1049),
    ("Testosterone",     "C19H28O2",    288.2089),
    ("Estradiol",        "C18H24O2",    272.1776),
    ("Cortisol",         "C21H30O5",    362.2093),
    ("Cholesterol",      "C27H46O",     386.3549),
    ("ATP",              "C10H16N5O13P3", 506.9957),
    ("NADH",             "C21H27N7O14P2", 663.1091),
    ("Glutathione",      "C10H17N3O6S", 307.0838),
    ("Melatonin",        "C13H16N2O2",  232.1212),
    ("Taxol",            "C47H51NO14",  853.3310),
    ("Vancomycin",       "C66H75Cl2N9O24", 1447.4300),
    ("Cyclosporin A",    "C62H111N11O12", 1201.8410),
    ("Rapamycin",        "C51H79NO13",  913.5551),
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

    peaks.append((
        round(precursor_mz + rng.uniform(-0.1, 0.1), 4),
        round(rng.uniform(100, 1000), 2),
    ))
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
            spectra.append({
                "id": spec_id,
                "compound_name": name,
                "formula": formula,
                "precursor_mz": precursor_mz,
                "peaks": peak_rows,
            })

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
# Synchronous search engine (dispatched via asyncio.to_thread)
# ======================================================================
def _build_report(
    experimental_file: str,
    database_file: str,
) -> str:
    """Perform the full library search and return a formatted Markdown report.

    This is a CPU-bound synchronous function.  It is called via
    ``asyncio.to_thread()`` to avoid blocking the event loop.
    """
    rng = random.Random(hash(database_file) & 0x7FFFFFFF)
    n_spectra = rng.randint(500, 5000)
    conn = _build_mock_database(n_spectra=n_spectra, seed=rng.randint(0, 2**31))
    try:
        # --- load experimental spectrum -------------------------------------
        exp_peaks = _mock_load_experimental(experimental_file, rng)
        logger.info(
            "Loaded experimental spectrum: %d peaks from %r",
            len(exp_peaks), experimental_file,
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
            n_spectra, chunk_size,
            "FDR" if use_fdr else "p-value",
        )

        for chunk_id, chunk in _iter_spectra_chunked(conn, chunk_size):
            for spec in chunk:
                score = _cosine(exp_peaks, spec["peaks"])
                target_scores.append(score)
                target_meta.append({
                    "id": spec["id"],
                    "compound_name": spec["compound_name"],
                    "formula": spec["formula"],
                    "precursor_mz": spec["precursor_mz"],
                    "score": score,
                })
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
            null_scores.append(_cosine(exp_peaks, shuffled))

        # --- FDR or p-value calculation -------------------------------------
        REPORT_THRESHOLD = 0.05

        if use_fdr:
            p_values = _estimate_empirical_p(target_scores, null_scores)
            q_values = _benjamini_hochberg(p_values)

            hits = [
                {**meta, "q_value": qv}
                for meta, qv in zip(target_meta, q_values)
                if qv <= REPORT_THRESHOLD
            ]
            hits.sort(key=lambda h: h["score"], reverse=True)
            method_line = f"FDR threshold (Benjamini-Hochberg): {REPORT_THRESHOLD}"
        else:
            p_values = _estimate_empirical_p(target_scores, null_scores)

            hits = [
                {**meta, "p_value": pv}
                for meta, pv in zip(target_meta, p_values)
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
                    f"{total_passing} hits passed the threshold "
                    f"({top_n} shown above)."
                )
            else:
                lines.append(
                    f"{total_passing} hit(s) passed the threshold."
                )

        logger.info(
            "_build_report(db=%r, n=%d, mode=%s) → %d hits (top %.4f)",
            database_file, n_spectra,
            "FDR" if use_fdr else "p-value",
            len(hits),
            hits[0]["score"] if hits else 0.0,
        )

        return "\n".join(lines)

    finally:
        conn.close()


# ======================================================================
# Async background task
# ======================================================================
async def _run_search_task(
    job_id: str,
    exp_file: str,
    db_file: str,
) -> None:
    """Background coroutine: executes the library search via a thread.

    Offloads the CPU-bound ``_build_report`` call to a thread-pool
    executor so the asyncio event loop is never blocked.
    """
    try:
        _JOB_STORE[job_id]["status"] = "running"
        logger.info("Search task %s: started", job_id)

        report = await asyncio.to_thread(_build_report, exp_file, db_file)

        _JOB_STORE[job_id]["status"] = "completed"
        _JOB_STORE[job_id]["result"] = report
        logger.info("Search task %s: completed", job_id)

    except Exception as exc:
        logger.exception("Search task %s failed", job_id)
        _JOB_STORE[job_id]["status"] = "failed"
        _JOB_STORE[job_id]["error"] = f"{type(exc).__name__}: {exc}"


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
    ) -> str:
        """Dispatch a spectral library search and return a job_id for polling.

        The search runs asynchronously in the background.  Use
        ``check_search_status`` with the returned *job_id* to retrieve
        results once the scan completes.  This pattern prevents host
        LLM timeouts on large libraries.
        """
        _ = SearchInput(
            experimental_file=experimental_file,
            database_file=database_file,
        )

        # Short, readable job identifier
        job_id = uuid.uuid4().hex[:8]

        _JOB_STORE[job_id] = {
            "status": "pending",
            "result": None,
            "error": None,
            "experimental_file": experimental_file,
            "database_file": database_file,
        }

        asyncio.create_task(
            _run_search_task(job_id, experimental_file, database_file)
        )

        logger.info(
            "Dispatched search job %s (exp=%r, db=%r)",
            job_id, experimental_file, database_file,
        )

        return (
            f"## Search Dispatched\n\n"
            f"**Job ID:** `{job_id}`\n\n"
            f"The spectral library search is running in the background.\n"
            f"Use `check_search_status` with this job ID to poll for results:\n\n"
            f'    check_search_status(job_id="{job_id}")\n'
        )

    # ------------------------------------------------------------------
    # Tool 2 — Poller
    # ------------------------------------------------------------------
    @mcp.tool()
    async def check_search_status(job_id: str) -> str:
        """Poll the status of a previously dispatched search job.

        Returns:
        - A "wait" message if the job is still pending or running.
        - The full Markdown hit table when the search completes.
        - An error message if the job failed.
        """
        _ = StatusInput(job_id=job_id)

        job = _JOB_STORE.get(job_id)
        if job is None:
            return (
                f"❓ **Unknown Job**\n\n"
                f"No search job found with ID `{job_id}`.  "
                f"Double-check the job ID or dispatch a new search via "
                f"`search_library`."
            )

        status = job["status"]

        if status == "pending":
            return (
                f"⏳ **Pending** — Job `{job_id}` has been queued and will "
                f"start shortly.  Poll again in a moment."
            )
        elif status == "running":
            return (
                f"🔄 **Running** — Job `{job_id}` is scanning the spectral "
                f"library and computing statistics.  Poll again shortly."
            )
        elif status == "completed":
            logger.info("check_search_status(%s): returning completed report", job_id)
            return job["result"]
        elif status == "failed":
            return (
                f"❌ **Failed** — Job `{job_id}` encountered an error:\n\n"
                f"```\n{job['error']}\n```"
            )
        else:
            return (
                f"⚠️  **Unknown status** `{status}` for job `{job_id}`.  "
                f"This may indicate an internal state corruption."
            )
