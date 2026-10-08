"""
Tests for orchestrator.core.tee_attestation — TEE hardware detection and graceful fallback.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock


from orchestrator.core.tee_attestation import (
    TeeAttestationProvider,
    detect_tee_hardware,
    generate_enclave_quote,
    _generate_tdx_quote,
    _generate_sev_quote,
)


# ── detect_tee_hardware ─────────────────────────────────────────────────────


class TestDetectTeeHardware:
    def test_no_tee_on_standard_hardware(self):
        with patch("orchestrator.core.tee_attestation._TDX_SYSFS") as tdx:
            tdx.exists.return_value = False
            with patch("orchestrator.core.tee_attestation._SEV_GUEST_DEV") as sev:
                sev.exists.return_value = False
                result = detect_tee_hardware()
                assert result is None

    def test_tdx_detected(self):
        with patch("orchestrator.core.tee_attestation._TDX_SYSFS") as tdx:
            tdx.exists.return_value = True
            result = detect_tee_hardware()
            assert result == "tdx"

    def test_sev_detected(self):
        with patch("orchestrator.core.tee_attestation._TDX_SYSFS") as tdx:
            tdx.exists.return_value = False
            with patch("orchestrator.core.tee_attestation._SEV_GUEST_DEV") as sev:
                sev.exists.return_value = True
                result = detect_tee_hardware()
                assert result == "sev-snp"

    def test_os_error_returns_none(self):
        with patch("orchestrator.core.tee_attestation._TDX_SYSFS") as tdx:
            tdx.exists.side_effect = OSError("permission denied")
            result = detect_tee_hardware()
            assert result is None


# ── generate_enclave_quote ───────────────────────────────────────────────────


class TestGenerateEnclaveQuote:
    def test_no_tee_returns_none(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value=None):
            result = generate_enclave_quote(b"test data")
            assert result is None

    def test_tdx_delegates(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value="tdx"):
            with patch("orchestrator.core.tee_attestation._generate_tdx_quote", return_value=b"tdx-quote"):
                result = generate_enclave_quote(b"test data")
                assert result == b"tdx-quote"

    def test_sev_delegates(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value="sev-snp"):
            with patch("orchestrator.core.tee_attestation._generate_sev_quote", return_value=b"sev-quote"):
                result = generate_enclave_quote(b"test data")
                assert result == b"sev-quote"

    def test_unknown_platform_returns_none(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value="unknown"):
            result = generate_enclave_quote(b"test data")
            assert result is None


# ── _generate_tdx_quote ─────────────────────────────────────────────────────


class TestGenerateTdxQuote:
    def test_inblob_not_found(self):
        with patch("pathlib.Path.exists", return_value=False):
            result = _generate_tdx_quote(b"test")
            assert result is None

    def test_os_error(self):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.write_bytes", side_effect=OSError("fail")):
                result = _generate_tdx_quote(b"test")
                assert result is None


# ── _generate_sev_quote ─────────────────────────────────────────────────────


class TestGenerateSevQuote:
    def test_snpguest_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _generate_sev_quote(b"test")
            assert result is None

    def test_snpguest_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
            result = _generate_sev_quote(b"test")
            assert result is None

    def test_snpguest_os_error(self):
        with patch("subprocess.run", side_effect=OSError("fail")):
            result = _generate_sev_quote(b"test")
            assert result is None

    def test_snpguest_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"sev-quote-data"
        with patch("subprocess.run", return_value=mock_result):
            result = _generate_sev_quote(b"test")
            assert result == b"sev-quote-data"

    def test_snpguest_nonzero_exit(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        with patch("subprocess.run", return_value=mock_result):
            result = _generate_sev_quote(b"test")
            assert result is None


# ── TeeAttestationProvider ───────────────────────────────────────────────────


class TestTeeAttestationProvider:
    def test_not_available_on_standard_hardware(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value=None):
            provider = TeeAttestationProvider()
            assert provider.is_available() is False

    def test_available_with_tdx(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value="tdx"):
            provider = TeeAttestationProvider()
            assert provider.is_available() is True

    def test_get_quote_unavailable(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value=None):
            provider = TeeAttestationProvider()
            assert provider.get_quote(b"test") is None

    def test_get_quote_returns_dict(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value="tdx"):
            with patch("orchestrator.core.tee_attestation.generate_enclave_quote", return_value=b"quote"):
                provider = TeeAttestationProvider()
                result = provider.get_quote(b"test")
                assert result is not None
                assert result["quote"] == b"quote"
                assert result["platform"] == "tdx"
                assert "timestamp" in result

    def test_get_quote_none_when_generation_fails(self):
        with patch("orchestrator.core.tee_attestation.detect_tee_hardware", return_value="tdx"):
            with patch("orchestrator.core.tee_attestation.generate_enclave_quote", return_value=None):
                provider = TeeAttestationProvider()
                result = provider.get_quote(b"test")
                assert result is None
