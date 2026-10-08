"""
Modus — Neuromorphic Hardware Backend Adapter (Phase 8e)
============================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Hardware backend adapters for real neuromorphic chips.  Currently
supports Intel Loihi (via lava-nc) and SynSense Speck (via rockpool).
These are optional, experimental integrations; the default path is the
software simulator in neuromorphic_engine.py (a research demo with no
hardware energy benefit on standard servers).

Both are strictly optional — Tier 2 dependencies that gracefully fall
back to the software emulator in neuromorphic_engine.py.  The hardware
detector probes for importable libraries and device files, caching the
result so detection only runs once per process lifetime.

Complies with the Four Laws:
    - Zero required dependencies — all hardware imports behind try/except
    - Graceful fallback — caller always gets None if hardware unavailable
    - No latency impact on software path
    - All computation local — no data leaves customer infrastructure
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.core.snn_compiler import SNNTopology

logger = logging.getLogger(__name__)


# ── Lazy import checks ───────────────────────────────────────────────────────

_lava_available: Optional[bool] = None
_rockpool_available: Optional[bool] = None


def _check_lava() -> bool:
    """Check if lava-nc is importable."""
    global _lava_available
    if _lava_available is None:
        try:
            import lava.magma.core.process.process as _lp  # noqa: F401  # type: ignore[import-untyped]
            _lava_available = True
        except (ImportError, ModuleNotFoundError):
            _lava_available = False
    return _lava_available


def _check_rockpool() -> bool:
    """Check if rockpool is importable."""
    global _rockpool_available
    if _rockpool_available is None:
        try:
            import rockpool  # noqa: F401  # type: ignore[import-untyped]
            _rockpool_available = True
        except (ImportError, ModuleNotFoundError):
            _rockpool_available = False
    return _rockpool_available


# ── Hardware Detector ────────────────────────────────────────────────────────


class NeuromorphicHardwareDetector:
    """Detects available neuromorphic hardware backends.

    Detection runs once and caches the result.  Checks:
        1. lava-nc importability (Loihi)
        2. rockpool importability (Speck)
        3. /dev/neuromorphic device files (physical hardware)
    """

    _cached_result: Optional[str] = None

    @classmethod
    def detect(cls) -> str:
        """Detect available hardware.  Returns 'loihi', 'speck', or 'none'.

        Result is cached — subsequent calls return immediately.
        """
        if cls._cached_result is not None:
            return cls._cached_result

        # Check for physical neuromorphic device files
        device_paths = [
            "/dev/neuromorphic",
            "/dev/loihi",
            "/dev/speck",
        ]
        has_device = any(os.path.exists(p) for p in device_paths)

        # Check Loihi (higher priority)
        if _check_lava():
            cls._cached_result = "loihi"
            logger.info(
                "Neuromorphic hardware detected: Loihi (lava-nc available%s)",
                ", device present" if has_device else "",
            )
            return cls._cached_result

        # Check Speck
        if _check_rockpool():
            cls._cached_result = "speck"
            logger.info(
                "Neuromorphic hardware detected: Speck (rockpool available%s)",
                ", device present" if has_device else "",
            )
            return cls._cached_result

        cls._cached_result = "none"
        logger.debug("No neuromorphic hardware detected — using software emulator")
        return cls._cached_result

    @classmethod
    def reset(cls) -> None:
        """Reset cached detection result (for testing)."""
        cls._cached_result = None


# ── Loihi Backend ────────────────────────────────────────────────────────────


class LoihiBackend:
    """Intel Loihi backend via lava-nc.

    Translates an SNNTopology into a Lava Process network and runs it
    on Loihi hardware (or the Lava software simulator if no physical
    chip is present).

    All lava imports are lazy — this class can be instantiated safely
    even if lava-nc is not installed; methods will raise RuntimeError.
    """

    def __init__(self) -> None:
        self._lava = None
        if _check_lava():
            try:
                import lava  # type: ignore[import-untyped]
                self._lava = lava
            except (ImportError, ModuleNotFoundError):
                pass

    @property
    def available(self) -> bool:
        return self._lava is not None

    def deploy(self, topology: "SNNTopology") -> Any:
        """Translate SNNTopology to a Lava Process network.

        Returns a Lava Process object that can be passed to run().

        Raises:
            RuntimeError: If lava-nc is not available.
        """
        if not self.available:
            raise RuntimeError(
                "Loihi backend unavailable — lava-nc is not installed. "
                "Install with: pip install lava-nc"
            )

        # Import Lava components
        from lava.proc.lif.process import LIF  # type: ignore
        from lava.proc.dense.process import Dense  # type: ignore

        logger.info(
            "Deploying SNN topology to Loihi: %d neurons, %d synapses",
            topology.neuron_count,
            topology.synapse_count,
        )

        # Build weight matrix (neuron_count x neuron_count)
        n = topology.neuron_count
        weights = [[0.0] * n for _ in range(n)]
        for s in topology.synapses:
            weights[s.post_idx][s.pre_idx] = s.weight

        # Create Lava LIF population
        thresholds = [neuron.threshold for neuron in topology.neurons]
        leak_rates = [neuron.leak_rate for neuron in topology.neurons]

        lif_proc = LIF(
            shape=(n,),
            vth=thresholds,
            du=leak_rates,
        )

        # Create Dense connectivity
        dense_proc = Dense(weights=weights)

        # Connect: LIF -> Dense -> LIF
        lif_proc.s_out.connect(dense_proc.s_in)
        dense_proc.a_out.connect(lif_proc.a_in)

        logger.info("Lava process network deployed successfully")
        return lif_proc

    def run(
        self,
        process: Any,
        inputs: list[float],
        timesteps: int = 10,
    ) -> dict[int, int]:
        """Run the deployed Lava process for *timesteps*.

        Returns dict mapping neuron index to spike count.

        Raises:
            RuntimeError: If lava-nc is not available.
        """
        if not self.available:
            raise RuntimeError("Loihi backend unavailable — lava-nc not installed")

        from lava.magma.core.run_configs import Loihi1SimCfg  # type: ignore
        from lava.magma.core.run_conditions import RunSteps  # type: ignore

        logger.debug("Running Loihi simulation for %d timesteps", timesteps)

        process.run(
            condition=RunSteps(num_steps=timesteps),
            run_cfg=Loihi1SimCfg(),
        )

        # Extract spike data (simplified — real implementation would
        # read from process probes)
        spike_counts: dict[int, int] = {}
        process.stop()

        return spike_counts


# ── Speck Backend ────────────────────────────────────────────────────────────


class SpeckBackend:
    """SynSense Speck backend via rockpool.

    Translates an SNNTopology into a rockpool network for deployment
    on SynSense Speck neuromorphic chips.

    All rockpool imports are lazy — safe to instantiate without the
    library installed.
    """

    def __init__(self) -> None:
        self._rockpool = None
        if _check_rockpool():
            try:
                import rockpool  # type: ignore[import-untyped]
                self._rockpool = rockpool
            except (ImportError, ModuleNotFoundError):
                pass

    @property
    def available(self) -> bool:
        return self._rockpool is not None

    def deploy(self, topology: "SNNTopology") -> Any:
        """Translate SNNTopology to a rockpool network.

        Returns a rockpool Module that can be passed to run().

        Raises:
            RuntimeError: If rockpool is not available.
        """
        if not self.available:
            raise RuntimeError(
                "Speck backend unavailable — rockpool is not installed. "
                "Install with: pip install rockpool"
            )

        from rockpool.nn.modules import LIFTorch  # type: ignore

        logger.info(
            "Deploying SNN topology to Speck: %d neurons, %d synapses",
            topology.neuron_count,
            topology.synapse_count,
        )

        # Build weight matrix
        n = topology.neuron_count
        weights = [[0.0] * n for _ in range(n)]
        for s in topology.synapses:
            weights[s.post_idx][s.pre_idx] = s.weight

        thresholds = [neuron.threshold for neuron in topology.neurons]
        leak_rates = [neuron.leak_rate for neuron in topology.neurons]

        net = LIFTorch(
            shape=(n,),
            threshold=thresholds,
            tau_mem=leak_rates,
        )

        logger.info("Rockpool network deployed successfully")
        return net

    def run(
        self,
        network: Any,
        inputs: list[float],
        timesteps: int = 10,
    ) -> dict[int, int]:
        """Run the deployed rockpool network for *timesteps*.

        Returns dict mapping neuron index to spike count.

        Raises:
            RuntimeError: If rockpool is not available.
        """
        if not self.available:
            raise RuntimeError("Speck backend unavailable — rockpool not installed")

        logger.debug("Running Speck simulation for %d timesteps", timesteps)

        # Simplified — real implementation would construct input tensor
        # and call network.evolve()
        spike_counts: dict[int, int] = {}
        return spike_counts


# ── Hardware Backend Factory ─────────────────────────────────────────────────


class HardwareBackendFactory:
    """Factory for neuromorphic hardware backends.

    Returns the appropriate backend if available, or None if hardware
    is not detected.  The caller (NeuromorphicEnforcer) falls back to
    the software emulator when None is returned.
    """

    @staticmethod
    def get_backend(backend_name: str) -> Optional[LoihiBackend | SpeckBackend]:
        """Get a hardware backend by name.

        Args:
            backend_name: One of 'loihi', 'speck', 'auto', or 'software'.
                - 'auto': detect hardware and return the best available
                - 'software': always return None (use software emulator)
                - 'loihi': return LoihiBackend if available
                - 'speck': return SpeckBackend if available

        Returns:
            A backend instance, or None if unavailable / software-only.
        """
        if backend_name == "software":
            return None

        if backend_name == "auto":
            detected = NeuromorphicHardwareDetector.detect()
            if detected == "none":
                return None
            backend_name = detected

        if backend_name == "loihi":
            backend = LoihiBackend()
            if backend.available:
                return backend
            logger.warning(
                "Loihi backend requested but lava-nc not available — "
                "falling back to software emulator"
            )
            return None

        if backend_name == "speck":
            backend = SpeckBackend()
            if backend.available:
                return backend
            logger.warning(
                "Speck backend requested but rockpool not available — "
                "falling back to software emulator"
            )
            return None

        logger.warning("Unknown backend %r — falling back to software", backend_name)
        return None
