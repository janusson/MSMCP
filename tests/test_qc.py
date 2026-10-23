"""Tests for the truthful QC summary tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from msmcp.errors import InaccessiblePathError, MalformedFileError
from msmcp.security import SecurityPolicy
from msmcp.tools import qc


class TestQC:
    def test_empty_imaging_prefix_stays_unverified_without_reading_later_pixels(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = SecurityPolicy(allowed_root=valid_mzml.parent, max_spectra=1)
        source = qc.ingest.resolve_source(valid_mzml, policy)
        first = next(source.iter_spectra())
        empty = replace(first, mz=np.array([]), intensity=np.array([]))
        closed: list[bool] = []

        class SparseImagingSource:
            format = "imzML"
            backend = "massflow.imzML"

            def iter_spectra(self):
                try:
                    yield empty
                    raise AssertionError(
                        "QC scanned past its budget for payload checks"
                    )
                finally:
                    closed.append(True)

        monkeypatch.setattr(qc, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(
            qc.ingest, "resolve_source", lambda _path, _policy: SparseImagingSource()
        )
        with pytest.raises(MalformedFileError, match="later pixels may contain signal"):
            qc_tools["generate_qc_summary"](str(valid_mzml))
        assert closed == [True]

    @pytest.mark.parametrize("limit", [1, 2])
    def test_capped_imzml_rejects_unreadable_binary_payload(
        self,
        qc_tools: dict[str, Callable[..., str]],
        imzml_fixture: Path,
        monkeypatch: pytest.MonkeyPatch,
        limit: int,
    ) -> None:
        imzml_fixture.with_suffix(".ibd").write_bytes(b"\x00\x01\x02")
        monkeypatch.setattr(
            qc,
            "DEFAULT_POLICY",
            SecurityPolicy(allowed_root=imzml_fixture.parent, max_spectra=limit),
        )
        with pytest.raises(MalformedFileError, match="payload"):
            qc_tools["generate_qc_summary"](str(imzml_fixture))

    def test_capped_nonempty_imzml_keeps_partial_coverage(
        self,
        qc_tools: dict[str, Callable[..., str]],
        imzml_fixture: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc,
            "DEFAULT_POLICY",
            SecurityPolicy(allowed_root=imzml_fixture.parent, max_spectra=1),
        )
        out = qc_tools["generate_qc_summary"](str(imzml_fixture))
        assert "**Spectra analysed:** 1" in out
        assert "full-acquisition coverage is not confirmed" in out

    def test_qc_summary(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=valid_mzml.parent)
        )
        out = qc_tools["generate_qc_summary"](str(valid_mzml))

        assert "**Spectra analysed:** 3" in out
        assert "Total ion current (analysed spectra):" in out
        assert "Mean TIC / analysed spectrum:" in out
        assert "Coverage:** complete" in out
        assert "Signal-to-Noise Ratio" in out
        assert "Peak Density" in out

    def test_qc_stops_at_policy_limit_and_discloses_incomplete_coverage(
        self,
        qc_tools: dict[str, Callable[..., str]],
        valid_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = SecurityPolicy(allowed_root=valid_mzml.parent, max_spectra=2)
        real_source = qc.ingest.resolve_source(valid_mzml, policy)
        first_two = list(real_source.iter_spectra())[:2]

        class SourceAtLimit:
            format = real_source.format
            backend = real_source.backend

            def iter_spectra(self):
                yield from first_two
                raise AssertionError("QC read beyond its configured spectrum limit")

        monkeypatch.setattr(qc, "DEFAULT_POLICY", policy)
        monkeypatch.setattr(
            qc.ingest, "resolve_source", lambda _path, _policy: SourceAtLimit()
        )
        out = qc_tools["generate_qc_summary"](str(valid_mzml))

        assert "**Spectra analysed:** 2" in out
        assert "processing limit was reached" in out
        assert "full-acquisition coverage is not confirmed" in out
        assert "Total ion current (analysed spectra):" in out

    def test_qc_truncated_file(
        self,
        qc_tools: dict[str, Callable[..., str]],
        truncated_mzml: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=truncated_mzml.parent)
        )
        with pytest.raises(MalformedFileError):
            qc_tools["generate_qc_summary"](str(truncated_mzml))

    def test_qc_missing_file(
        self,
        qc_tools: dict[str, Callable[..., str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(qc, "DEFAULT_POLICY", SecurityPolicy(allowed_root=tmp_path))
        with pytest.raises(InaccessiblePathError):
            qc_tools["generate_qc_summary"](str(tmp_path / "missing.mzML"))
