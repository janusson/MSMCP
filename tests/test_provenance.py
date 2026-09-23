"""Tests for structured provenance.

Provenance is only useful if it is trustworthy, so these tests check the
properties that make it so: records are immutable, always JSON-serialisable,
carry the versions and parameters that were actually used, link to parent
references, and survive a multi-step workflow end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from msmcp.provenance import (
    ModelInfo,
    Provenance,
    SoftwareInfo,
    SourceRef,
    file_digest,
    provenance_for,
    software_versions,
)


class TestSoftwareVersions:
    def test_include_msmcp_and_python(self) -> None:
        versions = software_versions()
        assert "python" in versions
        assert "msmcp" in versions
        assert all(isinstance(v, str) for v in versions.values())

    def test_optional_packages_simply_absent(self) -> None:
        """An absent optional dependency must not appear, and must not raise."""
        versions = software_versions()
        assert "definitely-not-installed" not in versions


class TestSourceRef:
    def test_from_path_captures_size_and_digest(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.mzML"
        path.write_bytes(b"<mzML/>")
        source = SourceRef.from_path(path, format="mzML", backend="msmcp.mzml")
        assert source.path == str(path)
        assert source.format == "mzML"
        assert source.backend == "msmcp.mzml"
        assert source.size_bytes == len(b"<mzML/>")
        assert source.digest == file_digest(path)
        assert source.digest is not None and source.digest.startswith("sha256:")

    def test_digest_is_content_addressed(self, tmp_path: Path) -> None:
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"same bytes")
        b.write_bytes(b"same bytes")
        assert file_digest(a) == file_digest(b)
        b.write_bytes(b"different bytes")
        assert file_digest(a) != file_digest(b)

    def test_large_files_are_not_hashed(self, tmp_path: Path) -> None:
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * 64)
        assert file_digest(path, limit_bytes=16) is None

    def test_unreadable_path_does_not_raise(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.bin"
        assert file_digest(missing) is None
        source = SourceRef.from_path(missing)
        assert source.size_bytes is None
        assert source.digest is None

    def test_to_dict_is_json_safe(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.mzML"
        path.write_text("x", encoding="utf-8")
        payload = SourceRef.from_path(path).to_dict()
        json.dumps(payload)
        assert payload["path"] == str(path)


class TestProvenance:
    def test_parameters_are_immutable(self) -> None:
        provenance = provenance_for("op", parameters={"a": 1})
        with pytest.raises(TypeError):
            provenance.parameters["a"] = 2  # type: ignore[index]

    def test_parameters_are_deeply_coerced_to_json_safe_types(self) -> None:
        provenance = provenance_for(
            "op",
            parameters={
                "array": np.array([1.0, 2.0]),
                "scalar": np.float64(3.5),
                "path": Path("/tmp/x"),
                "nested": {"tuple": (1, 2)},
            },
        )
        payload = json.loads(json.dumps(provenance.to_dict()))
        assert payload["parameters"]["array"] == [1.0, 2.0]
        assert payload["parameters"]["scalar"] == 3.5
        assert payload["parameters"]["path"] == "/tmp/x"
        assert payload["parameters"]["nested"]["tuple"] == [1, 2]

    def test_created_at_is_iso_utc(self) -> None:
        payload = provenance_for("op").to_dict()
        assert payload["created_at"].endswith("+00:00")
        assert "T" in payload["created_at"]

    def test_parents_are_stored_as_plain_reference_strings(self) -> None:
        provenance = provenance_for(
            "op", parents=["ptr:spectrum:abc", "ptr:peak-list:def"]
        )
        payload = provenance.to_dict()
        assert payload["parents"] == ["ptr:spectrum:abc", "ptr:peak-list:def"]

    def test_model_is_none_for_classical_work(self) -> None:
        assert provenance_for("op").to_dict()["model"] is None

    def test_model_is_recorded_when_a_model_ran(self) -> None:
        provenance = provenance_for(
            "op",
            model=ModelInfo(
                name="DreaMS", backend="real", embedding_dim=1024, checkpoint=None
            ),
        )
        model = provenance.to_dict()["model"]
        assert model == {
            "name": "DreaMS",
            "backend": "real",
            "embedding_dim": 1024,
            "checkpoint": None,
        }

    def test_software_defaults_to_this_installation(self) -> None:
        payload = provenance_for("op").to_dict()
        assert payload["software"]["name"] == "msmcp"
        assert "msmcp" in payload["software"]["versions"]

    def test_explicit_software_overrides_detection(self) -> None:
        provenance = provenance_for(
            "op", software=SoftwareInfo(name="msmcp", versions={"msmcp": "9.9.9"})
        )
        assert provenance.to_dict()["software"]["versions"] == {"msmcp": "9.9.9"}

    def test_sources_are_serialised(self, tmp_path: Path) -> None:
        path = tmp_path / "s.mzML"
        path.write_text("x", encoding="utf-8")
        provenance = provenance_for("op", sources=[SourceRef.from_path(path)])
        sources = provenance.to_dict()["sources"]
        assert len(sources) == 1
        assert sources[0]["path"] == str(path)

    def test_sources_and_parents_are_tuples(self) -> None:
        provenance = Provenance(operation="op")
        assert isinstance(provenance.sources, tuple)
        assert isinstance(provenance.parents, tuple)


class TestProvenanceAcrossAWorkflow:
    def test_a_derived_result_links_back_to_its_source(self, tmp_path: Path) -> None:
        """The multi-step case: load -> register -> derive, and still answer 'from what?'."""
        source_path = tmp_path / "run.mzML"
        source_path.write_bytes(b"<mzML/>")

        load_provenance = provenance_for(
            "load_spectrum",
            parameters={"spectrum_index": 0, "source_format": "mzML"},
            sources=[
                SourceRef.from_path(source_path, format="mzML", backend="msmcp.mzml")
            ],
        )
        reference = "ptr:spectrum:abc123"
        assert "parents" in load_provenance.to_dict()

        scoring_provenance = provenance_for(
            "search_library",
            parameters={"scoring_method": "classical", "query_peaks": 42},
            parents=[reference],
            model=None,
        )

        payload = scoring_provenance.to_dict()
        assert payload["parents"] == [reference]
        assert payload["parameters"]["scoring_method"] == "classical"
        assert payload["operation"] == "search_library"
        # The chain is answerable: which reference fed this result?
        assert payload["parents"][0].startswith("ptr:spectrum:")
        # ...and the earlier record answers which file and which reader.
        assert load_provenance.to_dict()["sources"][0]["backend"] == "msmcp.mzml"
