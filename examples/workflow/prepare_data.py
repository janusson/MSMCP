"""Derive a small, reproducible workflow dataset from a pinned MassBank release.

The demonstration needs real spectra with traceable provenance, and it must not
commit third-party data to this repository. This script therefore fetches the
MassBank release the project already pins for scientific validation, verifies
its SHA-256 before reading a byte of it, and derives a small, deterministic
subset from it.

What it writes into ``--out`` (default ``data/`` next to this file):

``library.msp``
    Reference spectra in MSP/NIST text: other measurements of the held-out
    compound plus a seeded sample of distractor compounds.
``query.mgf``
    One held-out spectrum, as MGF.
``manifest.json``
    Provenance: source URL and digest, subset rules, seed, counts, and the
    held-out compound's InChIKey. The InChIKey is the *answer key*, not an
    input: the search never sees it.

Records are parsed with :func:`msmcp.library.iter_msp_spectra`, the same reader
the server uses, so the subset cannot disagree with how the server will parse
it. Selection rules mirror ``VALIDATION_SIMILARITY.md`` §Preprocessing.

Usage::

    python examples/workflow/prepare_data.py --fetch
    python examples/workflow/prepare_data.py --source /data/MassBank_NISTformat.msp
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from msmcp.library import LibrarySpectrum, iter_msp_spectra

MASSBANK_URL = (
    "https://github.com/MassBank/MassBank-data/releases/download/2026.03/"
    "MassBank_NISTformat.msp"
)
MASSBANK_SHA256 = "cf49e1b293f1c8c1b4a645123638826eea70db656bd76e09ea0c47a1a56fc269"
MASSBANK_BYTES = 137_309_196
MASSBANK_DOI = "10.5281/zenodo.3378723"
MASSBANK_LICENCE = "CC-BY-4.0 (attribution to the MassBank consortium required)"

DEFAULT_SEED = 20261109
DEFAULT_REFERENCES = 200
MIN_PEAKS = 5
MAX_PEAKS = 300
MIN_PRECURSOR = 100.0
MAX_PRECURSOR = 1200.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A library record that survived the documented preprocessing filters."""

    inchi_key: str
    name: str
    precursor_mz: float
    peaks: tuple[tuple[float, float], ...]
    formula: str | None = None
    accession: str | None = None

    @property
    def peak_fingerprint(self) -> tuple[float, ...]:
        """m/z values rounded to the reader's own four-decimal reporting."""
        return tuple(round(mz, 4) for mz, _intensity in self.peaks)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(destination: Path) -> Path:
    """Download the pinned asset once, verifying it before it is used."""
    if destination.is_file() and sha256_of(destination) == MASSBANK_SHA256:
        print(f"using the verified copy already at {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {MASSBANK_URL} ({MASSBANK_BYTES / 1e6:.0f} MB) ...")
    partial = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(MASSBANK_URL) as response, partial.open("wb") as sink:
        while block := response.read(1 << 20):
            sink.write(block)
    digest = sha256_of(partial)
    if digest != MASSBANK_SHA256:
        partial.unlink(missing_ok=True)
        raise SystemExit(
            f"the downloaded file is not the pinned MassBank release\n"
            f"  expected sha256 {MASSBANK_SHA256}\n"
            f"  observed sha256 {digest}\n"
            f"nothing was written; check the URL or your connection"
        )
    partial.replace(destination)
    return destination


def _canonical(field: str) -> str:
    """Reduce a header name to letters and digits: ``Ion_mode`` == ``IONMODE``."""
    return "".join(character for character in field.lower() if character.isalnum())


def _metadata(spectrum: LibrarySpectrum, key: str) -> str | None:
    wanted = _canonical(key)
    for name, value in spectrum.metadata.items():
        if _canonical(name) == wanted:
            return value.strip()
    return None


def candidates(path: Path) -> list[Candidate]:
    """Apply the documented preprocessing filter, streaming the whole library."""
    kept: list[Candidate] = []
    seen = 0
    for spectrum in iter_msp_spectra(path):
        seen += 1
        ion_mode = _metadata(spectrum, "ion_mode")
        spectrum_type = _metadata(spectrum, "spectrum_type")
        inchi_key = _metadata(spectrum, "inchikey")
        if ion_mode is None or ion_mode.upper() != "POSITIVE":
            continue
        if spectrum_type is not None and spectrum_type.upper() != "MS2":
            continue
        if not (MIN_PEAKS <= len(spectrum.peaks) <= MAX_PEAKS):
            continue
        precursor = spectrum.precursor_mz
        if precursor is None or not (MIN_PRECURSOR <= precursor <= MAX_PRECURSOR):
            continue
        if not inchi_key:
            continue
        kept.append(
            Candidate(
                inchi_key=inchi_key,
                name=spectrum.compound_name,
                precursor_mz=precursor,
                peaks=spectrum.peaks,
                formula=_metadata(spectrum, "formula"),
                accession=_metadata(spectrum, "db"),
            )
        )
    print(
        f"read {seen:,} records; {len(kept):,} satisfy the documented filter "
        f"(positive, MS2, {MIN_PEAKS}-{MAX_PEAKS} peaks, precursor "
        f"{MIN_PRECURSOR:.0f}-{MAX_PRECURSOR:.0f} Da, InChIKey stated)"
    )
    return kept


def choose(
    pool: list[Candidate], seed: int, n_references: int
) -> tuple[Candidate, list[Candidate], list[Candidate]]:
    """Pick a held-out query, its same-compound references, and distractors."""
    by_key: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in pool:
        by_key[candidate.inchi_key].append(candidate)

    # Compounds measured more than once: the query is one measurement, the
    # references are genuinely different ones, so a correct answer cannot be a
    # self-match.
    repeatable = sorted(
        key
        for key, group in by_key.items()
        if len({c.peak_fingerprint for c in group}) > 1
    )
    if not repeatable:
        raise SystemExit("no compound in this release has two distinct measurements")

    rng = random.Random(seed)
    query_key = rng.choice(repeatable)
    group = sorted(by_key[query_key], key=lambda c: (-len(c.peaks), c.name))
    query = group[0]
    query_fingerprint = query.peak_fingerprint
    same_compound = [c for c in group[1:] if c.peak_fingerprint != query_fingerprint]

    distractor_keys = [key for key in repeatable if key != query_key]
    rng.shuffle(distractor_keys)
    distractors: list[Candidate] = []
    for key in distractor_keys:
        if len(distractors) >= n_references - len(same_compound):
            break
        distractors.append(sorted(by_key[key], key=lambda c: -len(c.peaks))[0])

    # A record whose peak list duplicates the query would leak the answer.
    distractors = [c for c in distractors if c.peak_fingerprint != query_fingerprint]
    return (
        query,
        same_compound,
        distractors[: max(0, n_references - len(same_compound))],
    )


def write_msp(path: Path, spectra: list[Candidate]) -> None:
    lines: list[str] = []
    for candidate in spectra:
        lines.extend(
            [
                f"Name: {candidate.name}",
                # The MassBank record this came from, so any hit in the report
                # can be traced back to the source deposition.
                *([f"DB#: {candidate.accession}"] if candidate.accession else []),
                f"InChIKey: {candidate.inchi_key}",
                *([f"Formula: {candidate.formula}"] if candidate.formula else []),
                "Ion_mode: POSITIVE",
                "Spectrum_type: MS2",
                # `PrecursorMZ` is the key the MSP reader maps to a precursor;
                # `PEPMASS` would parse as an unknown header and leave every
                # reference context-unknown, so the gate could never apply.
                f"PrecursorMZ: {candidate.precursor_mz:.6f}",
                f"Num Peaks: {len(candidate.peaks)}",
            ]
        )
        lines.extend(f"{mz:.4f} {intensity:.4f}" for mz, intensity in candidate.peaks)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_mgf(path: Path, query: Candidate) -> None:
    lines = [
        "BEGIN IONS",
        f"TITLE={query.name}_held_out",
        f"PEPMASS={query.precursor_mz:.6f}",
        "MSLEVEL=2",
        "IONMODE=POSITIVE",
    ]
    lines.extend(f"{mz:.4f} {intensity:.4f}" for mz, intensity in query.peaks)
    lines.extend(["END IONS", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path.home() / ".cache" / "msmcp-workflow",
        help="where the downloaded release is kept (never inside the repository)",
    )
    parser.add_argument(
        "--fetch", action="store_true", help="download the pinned release"
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="an existing MassBank_NISTformat.msp (skips the download)",
    )
    parser.add_argument("--references", type=int, default=DEFAULT_REFERENCES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--trust-source",
        action="store_true",
        help="accept --source without checking its SHA-256 (not reproducible)",
    )
    arguments = parser.parse_args()

    if arguments.source is None and not arguments.fetch:
        parser.error("choose --fetch or --source PATH")

    if arguments.source is not None:
        source = arguments.source.expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"no such file: {source}")
        if not arguments.trust_source:
            digest = sha256_of(source)
            if digest != MASSBANK_SHA256:
                raise SystemExit(
                    f"{source} is not the pinned MassBank release "
                    f"(sha256 {digest}); pass --trust-source to use it anyway"
                )
    else:
        source = fetch(arguments.cache.expanduser() / "MassBank_NISTformat.msp")

    pool = candidates(source)
    query, same_compound, distractors = choose(
        pool, arguments.seed, arguments.references
    )
    references = same_compound + distractors
    if not references:
        raise SystemExit("the selected compound has no usable references")

    arguments.out.mkdir(parents=True, exist_ok=True)
    write_msp(arguments.out / "library.msp", references)
    write_mgf(arguments.out / "query.mgf", query)

    manifest = {
        "source": {
            "dataset": "MassBank",
            "release": "2026.03",
            "url": MASSBANK_URL,
            "sha256": MASSBANK_SHA256,
            "bytes": MASSBANK_BYTES,
            "doi": MASSBANK_DOI,
            "licence": MASSBANK_LICENCE,
        },
        "selection": {
            "seed": arguments.seed,
            "filters": (
                f"positive, MS2, {MIN_PEAKS}-{MAX_PEAKS} peaks, precursor "
                f"{MIN_PRECURSOR:.0f}-{MAX_PRECURSOR:.0f} Da, InChIKey stated"
            ),
            "candidates_after_filter": len(pool),
            "references": len(references),
            "same_compound_references": len(same_compound),
        },
        "query": {
            "compound": query.name,
            "accession": query.accession,
            "inchi_key": query.inchi_key,
            "precursor_mz": query.precursor_mz,
            "peaks": len(query.peaks),
            "note": (
                "answer key, never sent to the server: the search receives the "
                "peak list and precursor only"
            ),
        },
    }
    (arguments.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(
        f"held-out query: {query.name} ({len(query.peaks)} peaks, "
        f"precursor {query.precursor_mz:.4f})"
    )
    print(
        f"references:      {len(references)} "
        f"({len(same_compound)} other measurements of the same compound)"
    )
    print(f"written to:      {arguments.out}")


if __name__ == "__main__":
    sys.exit(main())
