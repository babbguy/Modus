"""
Modus — TEE Attestation Provider
================================================================
Optional Trusted Execution Environment enclave quote generation.

Detects Intel TDX and AMD SEV-SNP hardware and provides enclave
quotes for attestation signing when running inside a TEE.

Gracefully degrades on non-TEE hardware — all functions return
None / False when no TEE is available.

Stdlib only.  No required dependencies.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Hardware detection paths ─────────────────────────────────────────────────

_TDX_SYSFS = Path("/sys/firmware/tdx")
_SEV_GUEST_DEV = Path("/dev/sev-guest")


# ── Public API ───────────────────────────────────────────────────────────────

def detect_tee_hardware() -> Optional[str]:
    """
    Detect available TEE hardware.

    Returns:
        ``"tdx"`` for Intel TDX, ``"sev-snp"`` for AMD SEV-SNP,
        or ``None`` if no supported TEE hardware is detected.
    """
    try:
        if _TDX_SYSFS.exists():
            logger.info("TEE hardware detected: Intel TDX")
            return "tdx"
        if _SEV_GUEST_DEV.exists():
            logger.info("TEE hardware detected: AMD SEV-SNP")
            return "sev-snp"
    except OSError as exc:
        logger.debug("TEE detection error: %s", exc)

    logger.debug("No TEE hardware detected — running on standard platform")
    return None


def generate_enclave_quote(payload: bytes) -> Optional[bytes]:
    """
    Generate a TEE enclave quote over *payload*.

    Uses the detected TEE platform to produce a hardware-rooted quote.
    Returns ``None`` if no TEE hardware is available or quote generation
    fails.
    """
    platform = detect_tee_hardware()
    if platform is None:
        logger.debug("Enclave quote unavailable — no TEE hardware")
        return None

    if platform == "tdx":
        return _generate_tdx_quote(payload)
    if platform == "sev-snp":
        return _generate_sev_quote(payload)

    return None


# ── TDX quote generation ────────────────────────────────────────────────────

def _generate_tdx_quote(payload: bytes) -> Optional[bytes]:
    """Generate an Intel TDX quote via the configfs-tsm report interface."""
    report_path = Path("/sys/kernel/config/tsm/report")
    try:
        # Write the report data (SHA-256 of payload fits in 64-byte field)
        import hashlib
        report_data = hashlib.sha256(payload).digest()

        inblob = report_path / "inblob"
        if not inblob.exists():
            logger.debug("TDX configfs-tsm report interface not available")
            return None

        inblob.write_bytes(report_data)
        outblob = report_path / "outblob"
        quote = outblob.read_bytes()
        logger.info("TDX enclave quote generated (%d bytes)", len(quote))
        return quote
    except OSError as exc:
        logger.warning("TDX quote generation failed: %s", exc)
        return None


# ── SEV-SNP quote generation ────────────────────────────────────────────────

def _generate_sev_quote(payload: bytes) -> Optional[bytes]:
    """Generate an AMD SEV-SNP attestation report via sev-guest ioctl."""
    try:
        import hashlib
        report_data = hashlib.sha256(payload).digest()

        # Attempt via snpguest CLI tool if available
        result = subprocess.run(
            ["snpguest", "report", "--random"],
            input=report_data,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            logger.info(
                "SEV-SNP enclave quote generated (%d bytes)",
                len(result.stdout),
            )
            return result.stdout

        logger.debug("snpguest tool returned non-zero or empty output")
        return None
    except FileNotFoundError:
        logger.debug("snpguest tool not found — SEV-SNP quote unavailable")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("SEV-SNP quote generation timed out")
        return None
    except OSError as exc:
        logger.warning("SEV-SNP quote generation failed: %s", exc)
        return None


# ── High-level provider class ────────────────────────────────────────────────

class TeeAttestationProvider:
    """
    Convenience wrapper for TEE attestation.

    Caches the hardware detection result so repeated calls to
    ``is_available`` don't hit the filesystem.
    """

    __slots__ = ("_platform",)

    def __init__(self) -> None:
        self._platform: Optional[str] = detect_tee_hardware()

    def is_available(self) -> bool:
        """Return True if a supported TEE platform was detected."""
        return self._platform is not None

    def get_quote(self, data: bytes) -> Optional[dict]:
        """
        Generate an enclave quote and return a structured result.

        Returns a dict with ``quote`` (bytes), ``platform`` (str), and
        ``timestamp`` (float, epoch seconds), or ``None`` if unavailable.
        """
        if not self.is_available():
            return None

        quote = generate_enclave_quote(data)
        if quote is None:
            return None

        return {
            "quote": quote,
            "platform": self._platform,
            "timestamp": time.time(),
        }
