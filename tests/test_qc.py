"""Tests for the truthful QC summary tool."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from msmcp.errors import InaccessiblePathError, MalformedFileError
from msmcp.security import SecurityPolicy
from msmcp.tools import qc


class TestQC:
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
        assert "Total ion current:" in out
        assert "Mean TIC / spectrum:" in out
        assert "Signal-to-Noise Ratio" in out
        assert "Peak Density" in out

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
