"""Real PyTorch inference backends for the spectral embedding adapters.

The deterministic stand-ins in :mod:`msmcp.models.embeddings` keep the MCP
server fully operational without heavyweight model dependencies.  This
module provides the real-inference counterparts behind the same
:class:`~msmcp.models.embeddings.SpectralEmbedder` contract:

* :class:`DreaMSInferenceEmbedder` - the official DreaMS transformer
  (Bushuiev et al., *Nature Biotechnology* 2025,
  https://doi.org/10.1038/s41587-025-02663-3), pre-trained on millions of
  tandem mass spectra.  The package is installed from source
  (``uv pip install "git+https://github.com/pluskal-lab/DreaMS.git"``; the
  ``dreams`` name on PyPI is an unrelated nanophotonics library) and the
  pre-trained 1024-dimensional embedding checkpoint is downloaded
  automatically on first use.  Spectra are embedded through the model's own
  preprocessing pipeline (DataFormat-A: fragment peaks strictly below the
  precursor, sorted by m/z, intensity max-normalised) via a temporary MGF
  file, so no preprocessing logic is duplicated here.

* :class:`LSMMS2InferenceEmbedder` - bring-your-own-checkpoint adapter.
  As of 2026-08 no public LSM-MS2 inference weights are distributed
  upstream (only a peer-review code drop exists at
  ``matterworksbio/LSM1-MS2``), so the adapter activates only when the
  ``MSMCP_LSM_MS2_CKPT`` environment variable points at a checkpoint whose
  model object exposes ``encode(mz, intensity, precursor_mz) -> np.ndarray``.

Both adapters keep the ``SpectralEmbedder`` contract: an (N, 2) float64
peak list in, an L2-normalised float32 embedding vector out.  Inference is
CPU-bound and runs inside the worker threads of the search dispatcher.

Embedder resolution
-------------------
:func:`get_embedder` selects between the deterministic mock embedders in
:mod:`msmcp.models.embeddings` and the real-inference adapters below based
on the ``MSMCP_EMBEDDING_BACKEND`` environment variable (``mock`` |
``auto`` | ``hf``).
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from msmcp.models.embeddings import (
    EMBEDDING_DIM,
    DreaMSEmbedder,
    LSMMS2Embedder,
    SpectralEmbedder,
    _coerce_peaks,
    _precursor_proxy,
)

logger = logging.getLogger("msmcp.models.backends")

LSM_MS2_CKPT_ENV = "MSMCP_LSM_MS2_CKPT"
"""Environment variable pointing at a user-supplied LSM-MS2 checkpoint."""


class EmbeddingBackendUnavailable(RuntimeError):
    """Raised when a real inference backend cannot be loaded.

    The message carries explicit, self-service instructions (install
    commands, environment variables) so the LLM can self-correct.
    """


# ======================================================================
# Shared helpers
# ======================================================================
@contextlib.contextmanager
def _stdout_to_stderr() -> Iterator[None]:
    """Redirect dependency prints to stderr to protect the stdio transport.

    The DreaMS package and torch may write progress or debug output to
    stdout; on the MCP stdio transport any stray stdout bytes corrupt the
    JSON-RPC framing.  Redirecting for the duration of the call guarantees
    the boundary even if a dependency misbehaves.  The transport holds its
    own reference to the original stdout stream, so the swap is invisible
    to the MCP server.
    """
    original = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = original


def _write_mgf(path: Path, peaks: np.ndarray, precursor_mz: float) -> None:
    """Write a minimal single-spectrum MGF consumable by the DreaMS package.

    *peaks* must already be sorted by m/z with max-normalised intensities;
    m/z values are written with 4 decimal places, matching the DreaMS
    training-data convention.
    """
    lines = [
        "BEGIN IONS",
        f"PEPMASS={precursor_mz:.6f}",
        "CHARGE=1+",
    ]
    lines.extend(f"{mz:.4f} {intensity:.4f}" for mz, intensity in peaks)
    lines.append("END IONS")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _l2_normalise(vector: np.ndarray) -> np.ndarray:
    """Return *vector* as an L2-normalised float32 array."""
    arr = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        raise ValueError("the model returned a zero embedding vector")
    return (arr / norm).astype(np.float32)


def _normalise_intensity(peaks: np.ndarray) -> np.ndarray:
    """Sort *peaks* by m/z and scale intensities to a unit maximum."""
    order = np.argsort(peaks[:, 0], kind="stable")
    sorted_peaks = peaks[order]
    peak_max = float(sorted_peaks[:, 1].max())
    if peak_max > 0.0:
        sorted_peaks = sorted_peaks.copy()
        sorted_peaks[:, 1] /= peak_max
    return sorted_peaks


# ======================================================================
# DreaMS — official pre-trained transformer (Nat. Biotechnol. 2025)
# ======================================================================
def _run_dreams_inference(api: Any, model: Any, mgf_path: Path) -> np.ndarray:
    """Embed the spectra in *mgf_path* with the loaded DreaMS model.

    Returns an (n, ``EMBEDDING_DIM``) float64 matrix.  Kept as a module
    function so tests can substitute a stub without importing torch.
    """
    preds = api.dreams_predictions(
        model_ckpt=model,
        spectra=mgf_path,
        model_cls=api.ContrastiveHead,
        batch_size=32,
        progress_bar=False,
        title="DreaMS_embedding",
    )
    arr = np.asarray(preds, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != EMBEDDING_DIM:
        raise ValueError(
            f"DreaMS returned embeddings of shape {arr.shape}; expected "
            f"(n, {EMBEDDING_DIM})"
        )
    return arr


class DreaMSInferenceEmbedder(SpectralEmbedder):
    """DreaMS real-inference adapter (official 1024-d embedding model).

    The checkpoint download and model load happen at most once per process
    and are reused across all subsequent embeddings.  Spectrum inputs are
    canonicalised to DreaMS DataFormat-A (fragment peaks strictly below the
    precursor, sorted by m/z, intensity max-normalised) before being handed
    to the model through a temporary MGF file.
    """

    name: ClassVar[str] = "DreaMS"
    backend: ClassVar[str] = "hf"
    embedding_dim: int = EMBEDDING_DIM

    _model: tuple[Any, Any] | None = None  # (dreams.api module, PreTrainedModel)
    _model_lock: ClassVar[threading.Lock] = threading.Lock()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @staticmethod
    def check_available() -> None:
        """Raise :class:`EmbeddingBackendUnavailable` unless the package is installed."""
        if importlib.util.find_spec("dreams") is None:
            raise EmbeddingBackendUnavailable(
                "The DreaMS package is not installed.  Install it from "
                "source with: "
                '`uv pip install "git+https://github.com/pluskal-lab/DreaMS.git"` '
                "(the `dreams` name on PyPI is an unrelated nanophotonics "
                "library).  The pre-trained 1024-d embedding checkpoint is "
                "downloaded automatically on first use."
            )

    @staticmethod
    def _import_api() -> Any:
        try:
            return importlib.import_module("dreams.api")
        except ImportError as exc:
            raise EmbeddingBackendUnavailable(
                "The DreaMS package is not installed.  Install it from "
                "source with: "
                '`uv pip install "git+https://github.com/pluskal-lab/DreaMS.git"` '
                "(the `dreams` name on PyPI is an unrelated nanophotonics "
                "library)."
            ) from exc

    @classmethod
    def _build_model(cls) -> tuple[Any, Any]:
        """Import the DreaMS API and load the pre-trained embedding model."""
        api = cls._import_api()
        logger.info(
            "Loading the pre-trained DreaMS embedding model; the checkpoint "
            "is downloaded automatically on first use."
        )
        model = api.PreTrainedModel.from_name(api.DREAMS_EMBEDDING)
        return api, model

    @classmethod
    def _load_model(cls) -> tuple[Any, Any]:
        """Return the cached (api, model) pair, loading it once per process."""
        cached = cls._model
        if cached is not None:
            return cached
        with cls._model_lock:
            loaded = cls._model
            if loaded is not None:
                return loaded
            loaded = cls._build_model()
            cls._model = loaded
            return loaded

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    def embed_spectrum(
        self, peaks: np.ndarray, precursor_mz: float | None = None
    ) -> np.ndarray:
        peaks_arr = _coerce_peaks(peaks)
        precursor = (
            precursor_mz if precursor_mz is not None else _precursor_proxy(peaks_arr)
        )

        # DreaMS DataFormat-A training spectra keep fragment peaks strictly
        # below the precursor m/z; drop anything at or above it.
        fragments = peaks_arr[peaks_arr[:, 0] < precursor]
        if len(fragments) == 0:
            raise ValueError(
                "no fragment peaks below the precursor m/z "
                f"({precursor:.4f}); DreaMS DataFormat-A spectra require "
                "all peaks to lie below the precursor"
            )
        fragments = _normalise_intensity(fragments)

        with tempfile.TemporaryDirectory(prefix="msmcp-dreams-") as tmp_dir:
            mgf_path = Path(tmp_dir) / "spectrum.mgf"
            _write_mgf(mgf_path, fragments, precursor)
            # The load (checkpoint download) and the inference may print to
            # stdout; keep the stdio transport boundary intact.
            with _stdout_to_stderr():
                api, model = self._load_model()
                rows = _run_dreams_inference(api, model, mgf_path)

        if len(rows) != 1:
            raise ValueError(
                f"DreaMS produced {len(rows)} embeddings for a single input spectrum"
            )
        return _l2_normalise(rows[0])


# ======================================================================
# LSM-MS2 — bring-your-own-checkpoint adapter
# ======================================================================
class LSMMS2InferenceEmbedder(SpectralEmbedder):
    """LSM-MS2 real-inference adapter (bring-your-own-checkpoint).

    Upstream has not published LSM-MS2 inference weights (only a peer-review
    code drop, ``matterworksbio/LSM1-MS2``), so this adapter activates only
    when the ``MSMCP_LSM_MS2_CKPT`` environment variable points at a
    checkpoint.  The checkpoint must deserialise (``torch.jit.load`` or
    ``torch.load``) to a model object exposing
    ``encode(mz, intensity, precursor_mz) -> np.ndarray``.  Peaks are sorted
    by m/z and intensity max-normalised before encoding; the embedding
    dimensionality is derived from the first ``encode`` output and the
    vector is L2-normalised float32.
    """

    name: ClassVar[str] = "LSM-MS2"
    backend: ClassVar[str] = "hf"
    embedding_dim: int = EMBEDDING_DIM  # replaced by the derived dim on first embed

    _model: Any | None = None
    _model_lock: ClassVar[threading.Lock] = threading.Lock()
    _derived_dim: int | None = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @staticmethod
    def check_available() -> None:
        """Raise :class:`EmbeddingBackendUnavailable` unless a checkpoint is configured."""
        if not os.environ.get(LSM_MS2_CKPT_ENV):
            raise EmbeddingBackendUnavailable(
                "No LSM-MS2 inference checkpoint is configured.  Set the "
                f"{LSM_MS2_CKPT_ENV} environment variable to a checkpoint "
                "path whose model object exposes "
                "encode(mz, intensity, precursor_mz).  Note that upstream "
                "currently publishes only peer-review code "
                "(matterworksbio/LSM1-MS2), not public inference weights."
            )

    @classmethod
    def _build_model(cls) -> Any:
        cls.check_available()
        ckpt = Path(os.environ[LSM_MS2_CKPT_ENV])
        if not ckpt.is_file():
            raise EmbeddingBackendUnavailable(f"LSM-MS2 checkpoint not found: {ckpt}")
        try:
            import torch  # pyright: ignore[reportMissingImports]  # optional `ml` extra; ImportError handled below
        except ImportError as exc:
            raise EmbeddingBackendUnavailable(
                "PyTorch is required for LSM-MS2 inference; install it with "
                "`uv pip install torch`."
            ) from exc
        try:
            model = torch.jit.load(ckpt, map_location="cpu")
        except Exception:
            model = torch.load(ckpt, map_location="cpu")
        if not callable(getattr(model, "encode", None)):
            raise EmbeddingBackendUnavailable(
                "The LSM-MS2 checkpoint does not expose "
                "encode(mz, intensity, precursor_mz).  Load the checkpoint "
                "and wrap the model so it provides that callable interface."
            )
        return model

    @classmethod
    def _load_model(cls) -> Any:
        """Return the cached model, loading it once per process."""
        cached = cls._model
        if cached is not None:
            return cached
        with cls._model_lock:
            loaded = cls._model
            if loaded is not None:
                return loaded
            loaded = cls._build_model()
            cls._model = loaded
            return loaded

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    def embed_spectrum(
        self, peaks: np.ndarray, precursor_mz: float | None = None
    ) -> np.ndarray:
        peaks_arr = _coerce_peaks(peaks)
        precursor = (
            precursor_mz if precursor_mz is not None else _precursor_proxy(peaks_arr)
        )
        canonical = _normalise_intensity(peaks_arr)

        model = self._load_model()
        with _stdout_to_stderr():
            raw = model.encode(canonical[:, 0], canonical[:, 1], float(precursor))

        vec = np.asarray(raw, dtype=np.float64)
        if vec.ndim != 1 or vec.size == 0:
            raise ValueError(
                f"LSM-MS2 encode() returned shape {vec.shape}; expected a "
                "1-D embedding vector"
            )
        if self._derived_dim is None:
            self._derived_dim = vec.size
            self.embedding_dim = vec.size
        elif vec.size != self._derived_dim:
            raise ValueError(
                f"LSM-MS2 embedding dimensionality changed between calls "
                f"({self._derived_dim} -> {vec.size})"
            )
        return _l2_normalise(vec)


# ======================================================================
# Registry for real inference backends
# ======================================================================
HF_EMBEDDERS: dict[str, type[SpectralEmbedder]] = {
    "dreams": DreaMSInferenceEmbedder,
    "lsm-ms2": LSMMS2InferenceEmbedder,
}
"""Real-inference embedder classes keyed by the registered method names."""


# ======================================================================
# Embedder resolution (deterministic mock fallback vs. real inference)
# ======================================================================
BACKEND_MODES: tuple[str, ...] = ("auto", "mock", "hf")
"""Valid values for the ``MSMCP_EMBEDDING_BACKEND`` environment variable."""

_EMBEDDER_REGISTRY: dict[str, type[SpectralEmbedder]] = {
    "dreams": DreaMSEmbedder,
    "lsm-ms2": LSMMS2Embedder,
}
"""Deterministic mock embedder classes keyed by the registered method names."""


def _resolve_backend_mode(backend: str | None) -> str:
    """Resolve the effective backend mode from *backend* or the environment."""
    mode = backend or os.environ.get("MSMCP_EMBEDDING_BACKEND", "auto")
    if mode not in BACKEND_MODES:
        logger.warning(
            "Unknown embedding backend mode %r (expected %s); using 'auto'",
            mode,
            ", ".join(BACKEND_MODES),
        )
        return "auto"
    return mode


def get_embedder(method: str, backend: str | None = None) -> SpectralEmbedder:
    """Instantiate the embedder registered under *method*.

    Parameters
    ----------
    method
        Registered embedding method (``"dreams"``, ``"lsm-ms2"``).
    backend
        Backend selection overriding the ``MSMCP_EMBEDDING_BACKEND``
        environment variable.  ``"auto"`` (default) uses real inference when
        the model package / checkpoint is available and falls back to the
        deterministic mock otherwise; ``"mock"`` always uses the
        deterministic fallback; ``"hf"`` requires real inference and raises
        :class:`~msmcp.models.backends.EmbeddingBackendUnavailable` with
        install instructions when it is not available.

    Raises
    ------
    ValueError
        If *method* is not a registered embedding method.
    msmcp.models.backends.EmbeddingBackendUnavailable
        In ``"hf"`` mode when the real backend cannot be loaded.
    """
    try:
        mock_cls = _EMBEDDER_REGISTRY[method]
    except KeyError:
        known = ", ".join(sorted(_EMBEDDER_REGISTRY))
        raise ValueError(
            f"Unknown embedding method {method!r}; expected one of: {known}"
        ) from None

    mode = _resolve_backend_mode(backend)
    if mode == "mock":
        return mock_cls()

    hf_cls = HF_EMBEDDERS[method]
    try:
        hf_cls.check_available()
    except EmbeddingBackendUnavailable as exc:
        if mode == "hf":
            raise
        logger.warning(
            "Real %s inference unavailable (%s); falling back to the "
            "deterministic mock.  Set MSMCP_EMBEDDING_BACKEND=mock to "
            "silence this warning.",
            method,
            exc,
        )
        return mock_cls()
    return hf_cls()
