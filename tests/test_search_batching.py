"""Classical chunk batching must preserve the scalar search contract."""

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from msmcp import library, security
from msmcp.errors import MalformedFileError
from msmcp.instruments import get_profile
from msmcp.models.scoring import (
    ClassicalScorer,
    EmbeddingScorer,
    PeakList,
    SpectrumScorer,
)
from msmcp.tools import search

PEAKS = ((100.0, 1.0), (200.0, 3.0))
CONTEXT = search.QueryContext(
    ms_level=2, precursor_mz=100.0, polarity="positive", source="file"
)


class ScalarClassical(ClassicalScorer):
    """An alternative scorer uses the unchanged per-pair scan path."""


class FixtureProvider(library.LibraryProvider):
    format = "MSP"

    def __init__(self, entries: list[library.LibrarySpectrum]) -> None:
        super().__init__(Path("virtual.msp"))
        self.entries = entries

    def describe(self) -> library.LibraryInfo:
        return library.LibraryInfo(
            str(self.path),
            "virtual",
            self.format,
            "fixture",
            len(self.entries),
            None,
            None,
        )

    def iter_spectra(
        self, chunk_size: int = 2000
    ) -> Iterator[list[library.LibrarySpectrum]]:
        # Exercise a genuinely empty chunk as well as an empty library.
        yield []
        for offset in range(0, len(self.entries), chunk_size):
            yield self.entries[offset : offset + chunk_size]


def entry(
    index: int,
    precursor: float | None = 100.0,
    polarity: str | None = "positive",
    peaks: tuple[tuple[float, float], ...] = PEAKS,
) -> library.LibrarySpectrum:
    return library.LibrarySpectrum(
        index, f"entry {index}", precursor, None, peaks, polarity
    )


def run(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[library.LibrarySpectrum],
    scorer: SpectrumScorer,
    *,
    chunk_size: int = 2,
    context: search.QueryContext | None = CONTEXT,
    peaks: tuple[tuple[float, float], ...] = PEAKS,
    method: search.ScoringMethod = "classical",
) -> search.SearchOutcome:
    monkeypatch.setattr(
        search, "_library_provider_for", lambda _: FixtureProvider(entries)
    )
    monkeypatch.setattr(search, "_build_scorer", lambda *args: scorer)
    return search._run_scan(
        search.SearchRequest(
            database_file="virtual.msp",
            experimental_peaks=peaks,
            scoring_method=method,
            chunk_size=chunk_size,
            query_context=context,
        )
    )


def assert_same(a: search.SearchOutcome, b: search.SearchOutcome) -> None:
    assert a.report == b.report
    assert a.summary == b.summary
    pa, pb = a.provenance.to_dict(), b.provenance.to_dict()
    pa.pop("created_at")
    pb.pop("created_at")
    assert pa == pb


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 100, 2000, 10000])
@pytest.mark.parametrize("context", [None, CONTEXT])
@pytest.mark.parametrize(
    "peaks",
    [
        PEAKS,
        ((200.0, 0.0), (100.0, 0.0)),
        ((200.0, 1e300), (100.0, 1e300), (100.0, 1.0)),
    ],
)
def test_batch_matches_scalar_search(
    monkeypatch: pytest.MonkeyPatch,
    chunk_size: int,
    context: search.QueryContext | None,
    peaks: tuple[tuple[float, float], ...],
) -> None:
    entries = [
        entry(1, peaks=peaks),
        entry(2, peaks=peaks),  # Exact tie must retain provider order.
        entry(3, precursor=None, polarity=None, peaks=peaks),
        entry(4, precursor=900.0, polarity="negative", peaks=peaks),
        entry(5, peaks=((100.0, 0.0),)),
    ]
    batched = run(
        monkeypatch,
        entries,
        ClassicalScorer(),
        chunk_size=chunk_size,
        context=context,
        peaks=peaks,
    )
    scalar = run(
        monkeypatch,
        entries,
        ScalarClassical(),
        chunk_size=chunk_size,
        context=context,
        peaks=peaks,
    )
    assert_same(batched, scalar)
    assert batched.report.index("entry 1") < batched.report.index("entry 2")
    if context is not None:
        assert batched.provenance.parameters["eligibility_counts"] == {
            "matched": 3,
            "unknown": 1,
            "excluded": 1,
        }
        assert batched.provenance.parameters["eligibility_exclusion_reasons"] == {
            "precursor outside declared tolerance; polarity mismatch": 1
        }


@pytest.mark.parametrize("entries", [[], [entry(1, precursor=900.0)]])
def test_empty_and_excluded_chunks_do_not_score(
    monkeypatch: pytest.MonkeyPatch, entries: list[library.LibrarySpectrum]
) -> None:
    scorer = ClassicalScorer()

    def forbidden(*args: Any) -> list[float]:
        raise AssertionError("no eligible candidates")

    monkeypatch.setattr(scorer, "score_many", forbidden)
    assert_same(
        run(monkeypatch, entries, scorer),
        run(monkeypatch, entries, ScalarClassical()),
    )


def test_scores_only_eligible_candidates_in_chunk_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scorer = ClassicalScorer()
    original = scorer.score_many
    batches: list[list[PeakList]] = []

    def capture(query: PeakList, references: Any) -> list[float]:
        refs = list(references)
        batches.append(refs)
        return original(query, refs)

    monkeypatch.setattr(scorer, "score_many", capture)
    entries = [entry(1), entry(2, precursor=900.0), entry(3), entry(4)]
    run(monkeypatch, entries, scorer)
    assert batches == [[list(PEAKS)], [list(PEAKS), list(PEAKS)]]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_wrong_batch_cardinality_fails(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    scorer = ClassicalScorer()
    monkeypatch.setattr(scorer, "score_many", lambda *args: [0.0] * count)
    with pytest.raises(search.MsmcpError, match="incorrect batch score count"):
        run(monkeypatch, [entry(1), entry(2)], scorer)


@pytest.mark.parametrize(
    "score",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.1,
        1.1,
        math.nextafter(0.0, -math.inf),
        math.nextafter(1.0, math.inf),
    ],
)
def test_invalid_batch_scores_are_not_discarded(
    monkeypatch: pytest.MonkeyPatch, score: float
) -> None:
    scorer = ClassicalScorer()
    # Even if scalar replay succeeds, the original batch error must survive.
    monkeypatch.setattr(scorer, "score_many", lambda *args: [score, 0.0])
    with pytest.raises(search.MsmcpError, match="invalid similarity score"):
        run(monkeypatch, [entry(1), entry(2)], scorer)


@pytest.mark.parametrize("later_error", ["score", "eligibility"])
def test_earlier_invalid_score_wins_over_later_error(
    monkeypatch: pytest.MonkeyPatch, later_error: str
) -> None:
    scorer = ClassicalScorer()
    original_score = scorer.score

    def invalid_first(query: PeakList, reference: PeakList) -> float:
        return -0.1 if reference[0][0] == 100.0 else original_score(query, reference)

    monkeypatch.setattr(scorer, "score", invalid_first)
    if later_error == "score":
        entries = [entry(1), entry(2, peaks=((300.0, -1.0),))]
    else:
        entries = [entry(1), entry(2, precursor=float("nan"))]
        original_assess = search._assess_eligibility

        def assess(ctx: Any, candidate: Any, profile: Any) -> dict[str, Any]:
            if candidate["id"] == 2:
                raise ValueError("later invalid metadata")
            return original_assess(ctx, candidate, profile)

        monkeypatch.setattr(search, "_assess_eligibility", assess)
    # score_many raises on the later record (or eligibility fails before it runs).
    # Scalar replay must still expose the earlier invalid score.
    with pytest.raises(search.MsmcpError, match="invalid similarity score"):
        run(monkeypatch, entries, scorer)


def test_earlier_malformed_peaks_win_over_later_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = search._assess_eligibility

    def assess(ctx: Any, candidate: Any, profile: Any) -> dict[str, Any]:
        if candidate["id"] == 2:
            raise ValueError("later metadata")
        return original(ctx, candidate, profile)

    monkeypatch.setattr(search, "_assess_eligibility", assess)
    entries = [entry(1, peaks=((100.0, -1.0),)), entry(2)]
    with pytest.raises(ValueError, match="negative intensity"):
        run(monkeypatch, entries, ClassicalScorer())


def test_malformed_later_record_matches_scalar_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [entry(1), entry(2, peaks=((100.0, float("nan")),))]
    failures = []
    for scorer in (ScalarClassical(), ClassicalScorer()):
        with pytest.raises(ValueError) as error:
            run(monkeypatch, entries, scorer)
        failures.append(str(error.value))
    assert failures[0] == failures[1]


def test_batch_only_fault_is_never_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scorer = ClassicalScorer()

    def broken(*args: Any) -> list[float]:
        raise RuntimeError("batch-only failure")

    monkeypatch.setattr(scorer, "score_many", broken)
    with pytest.raises(RuntimeError, match="batch-only failure"):
        run(monkeypatch, [entry(1)], scorer)


def test_parser_failure_precedes_scoring_of_an_unyielded_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken.msp"
    path.write_text(
        "Name: first\nPrecursorMZ: 100\nNum Peaks: 1\n100 1\n\n"
        "Name: broken\nNum Peaks: 1\nnot a peak\n"
    )
    monkeypatch.setattr(
        security, "DEFAULT_POLICY", security.SecurityPolicy(allowed_root=tmp_path)
    )
    scorer = ClassicalScorer()

    def forbidden(*args: Any) -> list[float]:
        raise AssertionError("parser must fail before the chunk is yielded")

    monkeypatch.setattr(scorer, "score_many", forbidden)
    monkeypatch.setattr(search, "_build_scorer", lambda *args: scorer)
    with pytest.raises(MalformedFileError):
        search._run_scan(search.SearchRequest(str(path), PEAKS, chunk_size=2))


def test_alternative_scorer_retains_pairwise_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Embedding scorers also take this branch; no new call to score_many.
    scorer = ScalarClassical()

    def forbidden(*args: Any) -> list[float]:
        raise AssertionError("alternative scorers must not batch")

    monkeypatch.setattr(scorer, "score_many", forbidden)
    assert run(monkeypatch, [entry(1)], scorer).summary.n_hits == 1


@pytest.mark.parametrize("method", ["dreams", "lsm-ms2"])
@pytest.mark.parametrize("negative", [False, True])
def test_embedding_execution_and_signed_score_policy_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, method: search.ScoringMethod, negative: bool
) -> None:
    # Test/dev mock encoders establish routing only, not real model performance.
    scorer = EmbeddingScorer(method)
    original = scorer.score
    calls = []

    def pair(query: PeakList, reference: PeakList) -> float:
        calls.append(reference)
        return -0.25 if negative else original(query, reference)

    def forbidden(*args: Any) -> list[float]:
        raise AssertionError("embedding scoring must not batch")

    monkeypatch.setattr(scorer, "score", pair)
    monkeypatch.setattr(scorer, "score_many", forbidden)
    entries = [entry(1), entry(2)]
    if negative:
        with pytest.raises(search.MsmcpError, match="invalid similarity score"):
            run(monkeypatch, entries, scorer, method=method)
        assert len(calls) == 1  # Do not evaluate later embeddings after failure.
    else:
        assert run(monkeypatch, entries, scorer, method=method).summary.n_hits == 2
        assert len(calls) == 2


def test_classical_chunk_does_not_mutate_inputs() -> None:
    chunk = [search._library_scan_dict(entry(1))]
    original = repr(chunk)
    search._classical_chunk_scores(
        ClassicalScorer(), list(PEAKS), chunk, CONTEXT, get_profile(None)
    )
    assert repr(chunk) == original
