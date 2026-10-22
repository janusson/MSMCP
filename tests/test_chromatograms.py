"""Scientific behavior tests for scan-level chromatogram extraction."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pytest

from msmcp.chromatograms import extract_chromatograms, tolerance_bounds
from msmcp.mzml import Spectrum, iter_spectra
from msmcp.security import SecurityPolicy, SpectrumCountExceededError
from msmcp.tools import chromatograms as chromatogram_module
from msmcp.tools.chromatograms import ChromatogramToolResult


def _spectrum(
    index: int,
    ms_level: int | None,
    retention_time: float | None,
    mz: list[float],
    intensity: list[float],
    representation: str | None = "centroid",
    polarity: str | None = "positive",
) -> Spectrum:
    return Spectrum(
        index=index,
        ms_level=ms_level,
        retention_time=retention_time,
        precursor_mz=None,
        mz=np.asarray(mz, dtype=np.float64),
        intensity=np.asarray(intensity, dtype=np.float64),
        polarity=polarity,
        representation=representation,
    )


def test_tolerance_bounds_use_ppm_and_da() -> None:
    assert tolerance_bounds(200.0, 5.0, "ppm") == pytest.approx((199.999, 200.001))
    assert tolerance_bounds(200.0, 0.01, "Da") == pytest.approx((199.99, 200.01))


def test_tolerance_bounds_reject_non_finite_calculated_windows() -> None:
    with pytest.raises(ValueError, match="non-finite m/z window"):
        tolerance_bounds(1e308, 1e308, "Da")


def test_mzml_reader_reports_centroid_profile_and_unknown(
    mzml_with_representation_metadata,
) -> None:
    spectra = list(iter_spectra(mzml_with_representation_metadata))
    assert [spectrum.representation for spectrum in spectra] == [
        "centroid",
        "profile",
        None,
    ]


def test_xic_includes_both_exact_mz_boundaries() -> None:
    result = extract_chromatograms(
        [_spectrum(7, 1, 1.0, [199.999, 200.001, 200.0011], [2.0, 3.0, 50.0])],
        targets_mz=[200.0],
        tolerance=0.001,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.xic[0].intensities == [5.0]


def test_multiple_targets_use_the_same_single_scan_pass() -> None:
    result = extract_chromatograms(
        [_spectrum(0, 1, 0.5, [100.0, 223.0634, 297.0822], [5.0, 11.0, 13.0])],
        targets_mz=[223.0634, 297.0822],
        tolerance=5.0,
        tolerance_unit="ppm",
        max_spectra=5,
    )
    assert [trace.intensities for trace in result.chromatograms.xic] == [
        [11.0],
        [13.0],
    ]
    assert result.chromatograms.tic_intensities == [29.0]
    assert result.chromatograms.bpc_intensities == [13.0]


def test_ms_level_filter_keeps_source_scan_indices_and_rt_order() -> None:
    result = extract_chromatograms(
        [
            _spectrum(10, 1, 2.0, [100.0], [4.0]),
            _spectrum(11, 2, 2.1, [100.0], [900.0]),
            _spectrum(12, 1, 2.0, [100.0], [6.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.scan_indices == [10, 12]
    assert result.chromatograms.retention_times_min == [2.0, 2.0]
    assert result.chromatograms.xic[0].intensities == [4.0, 6.0]


def test_empty_valid_scan_is_zero_but_invalid_arrays_are_missing() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 0.1, [], []),
            _spectrum(2, 1, 0.2, [100.0, 200.0], [3.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.xic[0].intensities == [0.0, None]
    assert result.chromatograms.tic_intensities == [0.0, None]
    assert result.chromatograms.bpc_intensities == [None, None]
    assert result.missing_observations[0].source_spectrum_index == 2


def test_overflowed_aggregates_are_reported_as_missing() -> None:
    result = extract_chromatograms(
        [_spectrum(8, 1, 1.0, [100.0, 100.0], [1e308, 1e308])],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=1,
    )
    assert result.chromatograms.tic_intensities == [None]
    assert result.chromatograms.bpc_intensities == [None]
    assert result.chromatograms.xic[0].intensities == [None]
    assert result.missing_observations[0].reason == "non_finite_aggregate"


def test_missing_rt_is_not_replaced_or_emitted_as_zero() -> None:
    result = extract_chromatograms(
        [_spectrum(3, 1, None, [100.0], [12.0])],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.chromatograms.retention_times_min == [None]
    assert result.chromatograms.xic[0].intensities == [None]
    assert result.missing_observations[0].reason == "retention_time_unavailable"


def test_rt_range_is_inclusive_and_does_not_reorder_scans() -> None:
    result = extract_chromatograms(
        [
            _spectrum(4, 1, 1.5, [100.0], [1.0]),
            _spectrum(5, 1, 1.0, [100.0], [2.0]),
            _spectrum(6, 1, 2.0, [100.0], [3.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        retention_time_range=(1.0, 1.5),
        max_spectra=5,
    )
    assert result.chromatograms.scan_indices == [4, 5]
    assert result.chromatograms.retention_times_min == [1.5, 1.0]


def test_selected_empty_scan_count_is_independent_of_rt_range() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 0.5, [], []),
            _spectrum(2, 1, 2.5, [100.0], [5.0]),
        ],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        retention_time_range=(2.0, 3.0),
        max_spectra=5,
    )

    assert result.acquisition["selected_empty_peak_array_spectra"] == 1
    assert result.chromatograms.scan_indices == [2]


def test_mixed_polarity_is_preserved_for_each_returned_trace_point(
    chromatogram_tools, mzml_with_polarity, monkeypatch
) -> None:
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=mzml_with_polarity.parent),
    )

    result = chromatogram_tools["extract_chromatograms"](
        file_path=str(mzml_with_polarity), ms_level=2, targets_mz=[]
    )

    assert result.chromatograms.polarities == ["positive", "negative"]
    assert result.model_dump(mode="json")["chromatograms"]["polarities"] == [
        "positive",
        "negative",
    ]


def test_missing_observations_are_serialized_as_structured_data(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    import re

    text = valid_mzml.read_text(encoding="utf-8")
    text = re.sub(r'<cvParam cvRef="MS" accession="MS:1000016"[^>]*/>', "", text)
    valid_mzml.write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )

    result = chromatogram_tools["extract_chromatograms"](
        file_path=str(valid_mzml), targets_mz=[]
    )

    payload = result.model_dump(mode="json")
    assert payload["missing_observations"][0]["reason"] == "retention_time_unavailable"
    assert payload["missing_observations"][0]["source_spectrum_index"] == 0


def test_representation_unknown_and_profile_are_reported() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 1.0, [100.0], [2.0], representation=None),
            _spectrum(2, 1, 1.1, [100.0], [3.0], representation="profile"),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.acquisition["representation_counts"] == {
        "centroid": 0,
        "profile": 1,
        "unknown": 1,
    }
    assert any("profile" in warning.lower() for warning in result.warnings)
    assert any("unknown" in warning.lower() for warning in result.warnings)


def test_polarity_metadata_is_counted_without_assuming_missing_values() -> None:
    result = extract_chromatograms(
        [
            _spectrum(1, 1, 1.0, [100.0], [2.0], polarity="positive"),
            _spectrum(2, 2, 1.1, [100.0], [3.0], polarity="negative"),
            _spectrum(3, 1, 1.2, [100.0], [4.0], polarity=None),
        ],
        targets_mz=[100.0],
        tolerance=0.1,
        tolerance_unit="Da",
        max_spectra=5,
    )
    assert result.acquisition["polarity_counts"] == {
        "positive": 1,
        "negative": 1,
        "unknown": 1,
    }
    assert result.acquisition["selected_polarity_counts"] == {
        "positive": 1,
        "negative": 0,
        "unknown": 1,
    }


def test_resource_limit_counts_scanned_spectra_not_selected_points() -> None:
    spectra = iter(
        [
            _spectrum(0, 2, 1.0, [100.0], [1.0]),
            _spectrum(1, 2, 1.1, [100.0], [1.0]),
            _spectrum(2, 1, 1.2, [100.0], [1.0]),
        ]
    )
    with pytest.raises(SpectrumCountExceededError, match="scanned spectra"):
        extract_chromatograms(
            spectra,
            targets_mz=[100.0],
            tolerance=0.1,
            tolerance_unit="Da",
            ms_level=1,
            max_spectra=2,
        )


def test_extraction_accepts_the_documented_ten_thousand_scan_bound() -> None:
    spectra = (
        _spectrum(index, 1, index / 60.0, [100.0], [1.0]) for index in range(5_001)
    )
    result = extract_chromatograms(
        spectra,
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=10_000,
    )

    assert result.completeness["scanned_spectra"] == 5_001
    assert result.completeness["full_acquisition_processed"] is True
    assert len(result.chromatograms.xic[0].intensities) == 5_001


def test_results_are_deterministic_and_include_calculation_provenance() -> None:
    spectra = [_spectrum(0, 1, 1.0, [100.0], [2.0])]
    first = extract_chromatograms(
        spectra, targets_mz=[100.0], tolerance=0.1, tolerance_unit="Da", max_spectra=5
    ).to_dict()
    second = extract_chromatograms(
        spectra, targets_mz=[100.0], tolerance=0.1, tolerance_unit="Da", max_spectra=5
    ).to_dict()
    assert first["chromatograms"] == second["chromatograms"]
    provenance = cast(dict[str, object], first["provenance"])
    parameters = cast(dict[str, object], first["extraction_parameters"])
    assert provenance["operation"] == "extract_chromatograms"
    assert parameters["xic_aggregation"] == "sum_inclusive_mz_window"


def test_mcp_tool_returns_structured_data_with_file_provenance(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    result = chromatogram_tools["extract_chromatograms"](
        file_path=str(valid_mzml),
        targets_mz=[200.0],
        tolerance=0.0,
        tolerance_unit="Da",
        ms_level=1,
        max_spectra=5,
    )

    assert isinstance(result, ChromatogramToolResult)
    payload = result.model_dump(mode="json")
    assert payload["acquisition"]["scanned_spectra"] == 3
    assert payload["acquisition"]["spectrum_counts_by_ms_level"] == {"1": 3}
    assert payload["chromatograms"]["tic_intensities"] == [60.0, 10.0, 10.0]
    assert payload["chromatograms"]["xic"][0]["intensities"] == [20.0, 0.0, 0.0]
    assert payload["chromatograms"]["retention_times_min"] == pytest.approx(
        [0.0, 1 / 60, 2 / 60]
    )
    assert payload["source"]["digest"].startswith("sha256:")
    assert payload["provenance"]["sources"][0]["backend"] == "msmcp.mzml"
    assert payload["provenance"]["software"]["versions"]["msmcp"]
    assert payload["completeness"]["full_acquisition_processed"] is True


def test_zero_intensity_centroid_remains_a_valid_zero_xic() -> None:
    result = extract_chromatograms(
        [_spectrum(0, 1, 1.0, [100.0], [0.0])],
        targets_mz=[100.0],
        tolerance=0.0,
        tolerance_unit="Da",
        max_spectra=1,
    )
    assert result.chromatograms.xic[0].intensities == [0.0]
    assert result.missing_observations == []


def test_tool_refuses_to_return_a_partial_acquisition(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with pytest.raises(SpectrumCountExceededError, match="No partial result"):
        chromatogram_tools["extract_chromatograms"](
            file_path=str(valid_mzml),
            targets_mz=[],
            max_spectra=2,
        )


def test_tool_authenticates_file_above_legacy_digest_ceiling(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    import hashlib

    from msmcp.provenance import DEFAULT_DIGEST_LIMIT_BYTES

    with valid_mzml.open("ab") as handle:
        for _ in range(65):
            handle.write(b" " * (1024 * 1024))
    assert valid_mzml.stat().st_size > DEFAULT_DIGEST_LIMIT_BYTES
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with valid_mzml.open("rb") as handle:
        expected = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    result = chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))
    assert result.source.digest == expected
    assert result.provenance["sources"][0]["digest"] == expected


def test_tool_preserves_native_scan_ids(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    result = chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))
    assert result.chromatograms.native_scan_ids == ["scan=0", "scan=1", "scan=2"]


def test_tool_rejects_declared_scan_count_mismatch(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    from msmcp.errors import MalformedFileError

    valid_mzml.write_text(
        valid_mzml.read_text().replace(
            'spectrumList count="3"', 'spectrumList count="4"'
        )
    )
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with pytest.raises(MalformedFileError, match="count"):
        chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))


def test_tool_carries_reader_length_warning(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    valid_mzml.write_text(
        valid_mzml.read_text().replace(
            'defaultArrayLength="3"', 'defaultArrayLength="4"'
        )
    )
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    result = chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))
    assert any(
        "declared_array_length_mismatch" in warning for warning in result.warnings
    )
    assert result.chromatograms.tic_intensities == [60.0, 10.0, 10.0]


@pytest.mark.parametrize("field", ["ms_level", "max_spectra"])
@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_tool_rejects_non_integer_resource_and_level_arguments(
    chromatogram_tools, valid_mzml, field, value
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        chromatogram_tools["extract_chromatograms"](
            file_path=str(valid_mzml), **{field: value}
        )


def test_result_rejects_unsupported_schema(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    from pydantic import ValidationError

    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    payload = chromatogram_tools["extract_chromatograms"](
        file_path=str(valid_mzml)
    ).model_dump()
    payload["schema_version"] = "99.0"
    with pytest.raises(ValidationError, match="schema_version"):
        ChromatogramToolResult.model_validate(payload)


def test_five_ppm_boundaries_and_adjacent_floats() -> None:
    lower, upper = 199.999, 200.001
    result = extract_chromatograms(
        [
            _spectrum(
                0,
                1,
                1.0,
                [
                    np.nextafter(lower, -np.inf),
                    lower,
                    upper,
                    np.nextafter(upper, np.inf),
                ],
                [100.0, 2.0, 3.0, 200.0],
            )
        ],
        targets_mz=[200.0],
        tolerance=5.0,
        tolerance_unit="ppm",
    )
    assert result.chromatograms.xic[0].intensities == [5.0]


@pytest.mark.parametrize(
    "corruption", ["xml_truncated", "missing_arrays", "invalid_base64", "missing_list"]
)
def test_tool_rejects_malformed_acquisition(
    chromatogram_tools, valid_mzml, monkeypatch, corruption
) -> None:
    from msmcp.errors import MalformedFileError

    text = valid_mzml.read_text()
    if corruption == "xml_truncated":
        text = text[:-30]
    elif corruption == "missing_arrays":
        text = text.replace("binaryDataArrayList", "absentArrayList")
    elif corruption == "invalid_base64":
        text = text.replace("<binary>", "<binary>@@@")
    else:
        text = "<mzML/>"
    valid_mzml.write_text(text)
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with pytest.raises(MalformedFileError):
        chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))


def test_tool_refuses_missing_authentication(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    from msmcp.errors import MsmcpError
    from msmcp.provenance import SourceRef

    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    monkeypatch.setattr(
        SourceRef, "from_path", lambda *args, **kwargs: SourceRef(path=str(valid_mzml))
    )
    with pytest.raises(MsmcpError, match="authentication failed"):
        chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))


def test_tool_refuses_source_changed_during_extraction(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    from msmcp import ingest
    from msmcp.errors import MsmcpError

    original = ingest.Source.iter_spectra

    def changing_reader(source):
        yield from original(source)
        with source.path.open("ab") as handle:
            handle.write(b" ")

    monkeypatch.setattr(ingest.Source, "iter_spectra", changing_reader)
    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(allowed_root=valid_mzml.parent),
    )
    with pytest.raises(MsmcpError, match="changed during extraction"):
        chromatogram_tools["extract_chromatograms"](file_path=str(valid_mzml))


def test_tool_cannot_override_file_size_policy(
    chromatogram_tools, valid_mzml, monkeypatch
) -> None:
    from msmcp.security import FileSizeExceededError

    monkeypatch.setattr(
        chromatogram_module,
        "DEFAULT_POLICY",
        SecurityPolicy(
            allowed_root=valid_mzml.parent,
            max_file_size_bytes=valid_mzml.stat().st_size - 1,
        ),
    )
    with pytest.raises(FileSizeExceededError):
        chromatogram_tools["extract_chromatograms"](
            file_path=str(valid_mzml), max_spectra=10000
        )


@pytest.mark.asyncio
async def test_chromatogram_stdio_contract_and_failures(valid_mzml, tmp_path) -> None:
    import gzip
    import os
    import sys

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    original = valid_mzml.read_bytes()
    compressed = tmp_path / "expanded.mzML.gz"
    compressed.write_bytes(gzip.compress(original + b" " * 10000))
    oversized = tmp_path / "oversized.mzML"
    oversized.write_bytes(original + b" " * 10000)
    truncated = tmp_path / "truncated.mzML"
    truncated.write_bytes(original[:-30])
    malformed = tmp_path / "bad.mzML"
    malformed.write_text("<mzML/>")
    env = dict(
        os.environ,
        MSMCP_ALLOWED_ROOT=str(tmp_path),
        MSMCP_MAX_FILE_SIZE_BYTES=str(len(original) + 100),
        MSMCP_EMBEDDING_BACKEND="mock",
    )
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "msmcp.server"], env=env
    )
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write, read_timeout_seconds=30) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        tool = next(t for t in listing.tools if t.name == "extract_chromatograms")
        assert tool.output_schema is not None
        assert tool.output_schema["properties"]["schema_version"]["const"] == "1.0"
        ok = await session.call_tool(
            "extract_chromatograms",
            dict(file_path=str(valid_mzml), targets_mz=[200.0], tolerance=5.0),
        )
        assert not ok.is_error
        payload = ok.structured_content
        assert payload is not None
        assert payload["chromatograms"]["tic_intensities"] == [60.0, 10.0, 10.0]
        assert payload["chromatograms"]["native_scan_ids"] == [
            "scan=0",
            "scan=1",
            "scan=2",
        ]
        assert payload["source"]["digest"].startswith("sha256:")
        cases: list[dict[str, Any]] = [
            dict(file_path=str(valid_mzml), max_spectra=2),
            dict(file_path=str(valid_mzml), max_spectra=10001),
            dict(file_path=str(valid_mzml), max_spectra=True),
            dict(file_path=str(valid_mzml), ms_level=1.5),
            dict(file_path=str(valid_mzml), ms_level=True),
            dict(file_path=str(valid_mzml), targets_mz=[0.0]),
            dict(file_path=str(valid_mzml), targets_mz=[-1.0]),
            dict(file_path=str(valid_mzml), targets_mz=[100.0] * 21),
            dict(file_path=str(valid_mzml), tolerance_unit="da"),
            dict(file_path=str(oversized)),
            dict(file_path=str(compressed)),
            dict(file_path=str(truncated)),
            dict(file_path=str(malformed)),
            dict(file_path=str(tmp_path.parent / "outside.mzML")),
        ]
        for arguments in cases:
            failure = await session.call_tool("extract_chromatograms", arguments)
            assert failure.is_error, (arguments, failure)
            assert not failure.structured_content, failure
        # A failed extraction must not poison the server or return cached traces.
        after = await session.call_tool(
            "extract_chromatograms", dict(file_path=str(valid_mzml))
        )
        assert not after.is_error
        assert after.structured_content is not None
        assert after.structured_content["chromatograms"]["tic_intensities"] == [
            60.0,
            10.0,
            10.0,
        ]
