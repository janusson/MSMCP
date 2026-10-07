"""Ground-truth benchmark for the real library reader (audit item 7 / F4).

The benchmark builds an MSP library whose exact contents are known, reads it
back through :mod:`msmcp.library`, and asks one question for each perturbed
query: **does the search recover the compound the query was drawn from?**

It is a *breakdown*, not a single number: m/z shift, intensity noise, dropped
peaks, extra noise peaks and two combinations are measured separately, because
each stresses a different part of the matching.  One deliberately severe
perturbation (a shift beyond the ±0.02 Da window) must *fail* to recover, so the
benchmark can fail — a benchmark that always passes proves nothing.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from msmcp.library import LibraryProvider, get_library_provider
from msmcp.models.scoring import ClassicalScorer
from msmcp.security import SecurityPolicy

K_COMPOUNDS = 12
TRUE_INDEX = K_COMPOUNDS // 2

# Perturbations the ±0.02 Da classical matcher must tolerate.
RECOVERABLE: tuple[str, ...] = (
    "exact",
    "mz_shift_0.005",
    "mz_shift_0.015",
    "intensity_noise",
    "drop_10pct",
    "drop_30pct",
    "add_noise_peaks",
    "combined_mild",
)
# A shift past the matching window: recovery must degrade, on purpose.
UNRECOVERABLE: tuple[str, ...] = ("mz_shift_0.05",)
ALL_PERTURBATIONS: tuple[str, ...] = RECOVERABLE + UNRECOVERABLE


def _library_peaks(seed: int) -> list[tuple[float, float]]:
    """A deterministic, compound-specific peak list."""
    rng = random.Random(seed)
    n_peaks = rng.randint(10, 18)
    mz = sorted(rng.uniform(50.0, 500.0) for _ in range(n_peaks))
    intensity = [rng.uniform(1.0, 100.0) for _ in range(n_peaks)]
    return [(round(m, 4), round(i, 2)) for m, i in zip(mz, intensity, strict=True)]


def _write_library(path: Path) -> dict[str, list[tuple[float, float]]]:
    """Write a K-compound MSP library and return its ground truth."""
    truth: dict[str, list[tuple[float, float]]] = {}
    blocks: list[str] = ["# MSMCP ground-truth benchmark library"]
    for i in range(K_COMPOUNDS):
        name = f"Compound-{i:02d}"
        peaks = _library_peaks(1000 + i)
        truth[name] = peaks
        formula = f"C{10 + i}H{12 + i}N{i}O2"
        blocks.append(f"Name: {name}")
        blocks.append(f"Formula: {formula}")
        blocks.append(f"PrecursorMZ: {300.0 + i}")
        blocks.append("Ion_mode: P")
        blocks.append(f"Num Peaks: {len(peaks)}")
        blocks.extend(f"{mz} {intensity}" for mz, intensity in peaks)
        blocks.append("")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return truth


def _perturb(
    peaks: list[tuple[float, float]], kind: str, rng: random.Random
) -> list[tuple[float, float]]:
    """Apply one perturbation family to a query peak list."""
    if kind == "exact":
        return list(peaks)
    if kind.startswith("mz_shift_"):
        shift = float(kind.removeprefix("mz_shift_"))
        return [(mz + shift, intensity) for mz, intensity in peaks]
    if kind == "intensity_noise":
        return [
            (mz, intensity * rng.lognormvariate(0.0, 0.3)) for mz, intensity in peaks
        ]
    if kind.startswith("drop_") and kind.endswith("pct"):
        fraction = int(kind.removeprefix("drop_").removesuffix("pct")) / 100.0
        keep = max(2, round(len(peaks) * (1.0 - fraction)))
        return sorted(rng.sample(peaks, keep))
    if kind == "add_noise_peaks":
        extra = [
            (round(rng.uniform(50.0, 500.0), 4), round(rng.uniform(1.0, 50.0), 2))
            for _ in range(5)
        ]
        return sorted([*peaks, *extra])
    if kind == "combined_mild":
        shifted = _perturb(peaks, "mz_shift_0.01", rng)
        dropped = _perturb(shifted, "drop_20pct", rng)
        noised = _perturb(dropped, "intensity_noise", rng)
        return _perturb(noised, "add_noise_peaks", rng)
    raise AssertionError(f"unknown perturbation {kind!r}")


def _provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LibraryProvider, dict[str, list[tuple[float, float]]]]:
    """Build the benchmark library in *tmp_path* and read it back."""
    from msmcp import security

    monkeypatch.setattr(
        security, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path)
    )
    truth = _write_library(tmp_path / "ground_truth.msp")
    provider = get_library_provider(
        str(tmp_path / "ground_truth.msp"), SecurityPolicy(allowed_root=tmp_path)
    )
    assert provider.describe().n_spectra == K_COMPOUNDS
    return provider, truth


def _rank(
    provider: LibraryProvider, query: list[tuple[float, float]]
) -> tuple[int, str, float, float]:
    """Return (rank of true compound, top name, top score, true score)."""
    scorer = ClassicalScorer()
    true_name = f"Compound-{TRUE_INDEX:02d}"
    scored: list[tuple[float, str]] = [
        (scorer.score(query, spectrum.peaks), spectrum.compound_name)
        for chunk in provider.iter_spectra(4)
        for spectrum in chunk
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    true_score = next(score for score, name in scored if name == true_name)
    rank = next(
        i for i, (_score, name) in enumerate(scored, start=1) if name == true_name
    )
    return rank, scored[0][1], scored[0][0], true_score


def test_retrieval_is_exact_for_an_unperturbed_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, truth = _provider(tmp_path, monkeypatch)
    true_name = f"Compound-{TRUE_INDEX:02d}"
    rank, top, top_score, true_score = _rank(provider, truth[true_name])

    assert rank == 1
    assert top == true_name
    assert top_score == pytest.approx(1.0)
    assert true_score == pytest.approx(1.0)


@pytest.mark.parametrize("kind", RECOVERABLE)
def test_recovery_per_perturbation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Every recoverable perturbation retrieves the true compound at rank 1."""
    provider, truth = _provider(tmp_path, monkeypatch)
    true_name = f"Compound-{TRUE_INDEX:02d}"
    query = _perturb(truth[true_name], kind, random.Random(7))

    rank, top, _top_score, true_score = _rank(provider, query)
    assert rank == 1, f"{kind}: true compound ranked {rank} (top={top})"
    assert top == true_name, f"{kind}: top hit was {top}"
    assert true_score > 0.0, f"{kind}: the true compound did not match at all"


def test_breakdown_reports_every_perturbation_and_can_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Print the per-perturbation breakdown; the severe case must not recover."""
    provider, truth = _provider(tmp_path, monkeypatch)
    true_name = f"Compound-{TRUE_INDEX:02d}"

    breakdown: dict[str, tuple[int, float]] = {}
    for kind in ALL_PERTURBATIONS:
        rng = random.Random(7)
        query = _perturb(truth[true_name], kind, rng)
        rank, _top, _top_score, true_score = _rank(provider, query)
        breakdown[kind] = (rank, true_score)

    header = f"{'perturbation':<18} {'true rank':>9} {'true score':>10}  recovered"
    lines = [header, "-" * len(header)]
    for kind in ALL_PERTURBATIONS:
        rank, score = breakdown[kind]
        lines.append(f"{kind:<18} {rank:>9} {score:>10.4f}  {rank == 1}")
    print("\n" + "\n".join(lines))

    for kind in RECOVERABLE:
        assert breakdown[kind][0] == 1, f"{kind} did not recover the true compound"
    severe_rank, severe_score = breakdown[UNRECOVERABLE[0]]
    assert severe_score < 0.1, (
        f"a shift beyond the ±0.02 Da window still scored {severe_score:.4f}; the "
        f"benchmark cannot distinguish a match from a miss"
    )
    assert K_COMPOUNDS > 1  # the library is not a single-compound triviality
    assert severe_rank >= 0
