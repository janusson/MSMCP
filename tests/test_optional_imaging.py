"""Core ingestion and diagnostics must not require either MassFlow project."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from packaging.requirements import Requirement

from msmcp import massflow_io
from msmcp.errors import MissingDependencyError
from msmcp.ingest import resolve_source
from msmcp.provenance import software_versions
from msmcp.security import SecurityPolicy
from msmcp.tools.system import PingResponse, register_tools


def test_imaging_is_not_a_core_install_requirement() -> None:
    requirements = [
        Requirement(value)
        for value in importlib.metadata.requires("msmcp") or []
        if Requirement(value).name == "massflow"
    ]
    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.marker is not None
    assert not requirement.marker.evaluate({"extra": ""})
    assert not requirement.marker.evaluate({"extra": "dev"})
    assert requirement.marker.evaluate({"extra": "imaging"})
    assert requirement.specifier.contains("0.1.2")
    assert not requirement.specifier.contains("0.2.0")


def test_core_readers_and_server_never_import_massflow(
    valid_mzml: Path, valid_mgf: Path
) -> None:
    # A fresh process catches eager imports even when other tests have already
    # loaded the server. Blocking both names also detects an engine substitution.
    code = """
import importlib.abc
import json
import sys

class NoMassFlow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0].lower() == 'massflow':
            raise AssertionError(f'Core unexpectedly imported {fullname}')

sys.meta_path.insert(0, NoMassFlow())
from msmcp.server import mcp
from msmcp.ingest import resolve_source
from msmcp.provenance import provenance_for
from msmcp.security import SecurityPolicy

backends = []
for filename, count in ((sys.argv[1], 3), (sys.argv[2], 2)):
    source = resolve_source(filename, SecurityPolicy(allowed_root=sys.argv[3]))
    spectra = list(source.iter_spectra())
    assert len(spectra) == source.declared_spectrum_count() == count
    assert all(not spectrum.mz.flags.writeable for spectrum in spectra)
    backends.append(source.backend)
assert not any(name.split('.')[0].lower() == 'massflow' for name in sys.modules)
assert provenance_for('core-test').to_dict()['software']['name'] == 'msmcp'
print(json.dumps(backends))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(valid_mzml),
            str(valid_mgf),
            str(valid_mgf.parent),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout) == ["msmcp.mzml", "msmcp.mgf"]


@pytest.mark.parametrize("version", [None, "0.1.2"])
def test_ping_inventories_metadata_without_importing_backend(
    monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    def metadata_version(name: str) -> str:
        assert name == "massflow"
        if version is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return version

    def no_backend(name: str, package: str | None = None) -> ModuleType:
        pytest.fail(f"ping must not import the backend: {name}")

    monkeypatch.setattr(importlib.metadata, "version", metadata_version)
    monkeypatch.setattr(importlib, "import_module", no_backend)

    class ToolCapture:
        ping: Any

        def tool(self, **kwargs: Any) -> Any:
            def capture(function: Any) -> Any:
                self.ping = function
                return function

            return capture

    tools = ToolCapture()
    register_tools(tools)
    response: PingResponse = tools.ping()
    assert response.status == "ok"
    assert response.massflow_available is (version is not None)
    assert response.massflow_version == version
    assert massflow_io.massflow_version() == version


def test_provenance_omits_absent_imaging_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_version = importlib.metadata.version

    def no_massflow(name: str) -> str:
        if name == "massflow":
            raise importlib.metadata.PackageNotFoundError(name)
        return original_version(name)

    monkeypatch.setattr(importlib.metadata, "version", no_massflow)
    versions = software_versions()
    assert "massflow" not in versions
    assert "msmcp" in versions and "numpy" in versions


@pytest.mark.parametrize("failure", ["absent", "broken", "wrong-api"])
@pytest.mark.parametrize("operation", ["iterate", "count"])
def test_imzml_reports_optional_dependency_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    operation: str,
) -> None:
    # Path dispatch/security remains usable without imaging; dependency failure
    # belongs to reading, before a fake/empty result could escape to tools.
    header = tmp_path / "acquisition.imzML"
    header.write_text("<imzML/>", encoding="utf-8")
    header.with_suffix(".ibd").write_bytes(b"payload")
    source = resolve_source(header, SecurityPolicy(allowed_root=tmp_path))
    assert source.backend == "massflow.imzML"

    def import_backend(name: str, package: str | None = None) -> ModuleType:
        assert name == "massflow.data_manager"
        if failure == "absent":
            raise ModuleNotFoundError("No module named 'massflow'")
        if failure == "broken":
            raise ImportError("imaging transitive dependency failed")
        return ModuleType(name)  # a colliding package without the required API

    monkeypatch.setattr(importlib, "import_module", import_backend)
    with pytest.raises(MissingDependencyError) as error:
        if operation == "iterate":
            list(source.iter_spectra())
        else:
            source.declared_spectrum_count()
    message = str(error.value)
    assert "uv sync --extra imaging" in message
    assert "pip install '.[imaging]'" in message
    assert "analytical MassFlow" in message
    assert "Core mzML/MGF" in message
    assert isinstance(error.value.__cause__, (ImportError, AttributeError))


def test_failed_imaging_import_also_retargets_stdout_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    imaging_logger = logging.getLogger("massflow.optional-test")
    handler = logging.StreamHandler(sys.stdout)

    def broken_import(name: str, package: str | None = None) -> ModuleType:
        imaging_logger.addHandler(handler)
        raise ImportError("failure after logger setup")

    monkeypatch.setattr(importlib, "import_module", broken_import)
    try:
        with pytest.raises(MissingDependencyError):
            massflow_io._import_manager()
        assert handler.stream is sys.stderr
    finally:
        imaging_logger.removeHandler(handler)
        handler.close()
