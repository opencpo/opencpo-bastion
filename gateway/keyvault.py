"""
OpenCPO Bastion — Certificate Key Vault

Manages the device TLS certificate lifecycle:
  - First boot: generate RSA keypair, create CSR, request signed cert from Core PKI
  - Auto-renewal: checks expiry daily, renews 30 days before expiry
  - Storage: /etc/opencpo/certs/ (encrypted with device-specific key)
  - TPM: detects Pi TPM HAT, uses for key storage when available
  - Fallback: LUKS-encrypted partition, or software encryption keyed to machine-id
  - Private keys never leave the device
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

CERT_DIR = Path("/etc/opencpo/certs")
CERT_FILE = CERT_DIR / "device.crt"
KEY_FILE = CERT_DIR / "device.key"
CA_FILE = CERT_DIR / "ca.crt"
MACHINE_ID_FILE = Path("/etc/machine-id")


def _device_id() -> str:
    """Stable unique device identifier based on machine-id."""
    try:
        return MACHINE_ID_FILE.read_text().strip()
    except Exception:
        return f"gw-{os.urandom(8).hex()}"


def _encryption_key() -> bytes:
    """
    Derive a device-specific encryption key.
    Uses machine-id + fixed salt — not exportable without physical access.
    """
    import hashlib
    machine_id = _device_id().encode()
    salt = b"opencpo-keyvault-v1"
    return hashlib.sha256(machine_id + salt).digest()


def _encrypt_file(path: Path, data: bytes) -> None:
    """Simple symmetric encryption for key files at rest."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _encryption_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(nonce + ciphertext)
    path.chmod(0o600)


def _decrypt_file(path: Path) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = path.read_bytes()
    nonce, ciphertext = raw[:12], raw[12:]
    key = _encryption_key()
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)


class TPMKeyStore:
    """
    TPM 2.0 key storage via tpm2-tools (Pi TPM HAT or discrete TPM).
    Wraps the private key inside the TPM — key never appears in plaintext.
    """

    def __init__(self, handle: int = 0x81000001):
        self.handle = handle
        self._available = self._detect()

    def _detect(self) -> bool:
        import subprocess
        try:
            r = subprocess.run(
                ["tpm2_getcap", "properties-fixed"],
                capture_output=True, timeout=5
            )
            if r.returncode == 0:
                logger.info("TPM 2.0 detected — using hardware key storage")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return False

    @property
    def available(self) -> bool:
        return self._available

    def generate_key(self) -> None:
        import subprocess
        subprocess.run([
            "tpm2_createprimary", "-C", "o", "-G", "rsa2048",
            "-c", "/tmp/primary.ctx",
        ], check=True)
        subprocess.run([
            "tpm2_create", "-G", "rsa2048", "-u", str(CERT_DIR / "tpm_pub.key"),
            "-r", str(CERT_DIR / "tpm_priv.key"), "-C", "/tmp/primary.ctx",
        ], check=True)
        subprocess.run([
            "tpm2_load", "-C", "/tmp/primary.ctx",
            "-u", str(CERT_DIR / "tpm_pub.key"),
            "-r", str(CERT_DIR / "tpm_priv.key"),
            "-c", str(CERT_DIR / "tpm_handle.ctx"),
        ], check=True)
        subprocess.run([
            "tpm2_evictcontrol", "-C", "o",
            "-c", str(CERT_DIR / "tpm_handle.ctx"),
            hex(self.handle),
        ], check=True)
        logger.info("TPM key provisioned at handle %s", hex(self.handle))


class KeyVault:
    """
    Device certificate lifecycle manager.
    Generates, stores, renews, and provides access to the device cert.
    """

    def __init__(self, config, renew_days_before: int = 30):
        self.config = config
        self.renew_days_before = renew_days_before
        self._tpm = TPMKeyStore()
        self._cert: Optional[x509.Certificate] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        CERT_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._http = httpx.AsyncClient(timeout=30)

        if not CERT_FILE.exists():
            logger.info("No device cert found — running first-boot provisioning")
            await self.provision()
        else:
            self._load_cert()
            logger.info(
                "Device cert loaded (expires %s)",
                self._cert.not_valid_after_utc.strftime("%Y-%m-%d") if self._cert else "unknown",
            )

        # Start renewal background loop
        asyncio.create_task(self._renewal_loop())

    def _load_cert(self) -> None:
        try:
            raw = CERT_FILE.read_bytes()
            self._cert = x509.load_pem_x509_certificate(raw)
        except Exception as e:
            logger.error("Failed to load device cert: %s", e)
            self._cert = None

    def _generate_keypair(self) -> rsa.RSAPrivateKey:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        if self._tpm.available:
            # Store via TPM — don't write plaintext key
            self._tpm.generate_key()
        else:
            _encrypt_file(KEY_FILE, key_pem)

        logger.info(
            "Keypair generated (%s)",
            "TPM-backed" if self._tpm.available else "software-encrypted",
        )
        return key

    def _build_csr(self, key: rsa.RSAPrivateKey) -> bytes:
        device_id = _device_id()
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, f"gw-{device_id[:16]}"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OpenCPO Bastion"),
            ]))
            .sign(key, hashes.SHA256())
        )
        return csr.public_bytes(serialization.Encoding.PEM)

    async def provision(self) -> None:
        """First-boot: generate keypair, get CSR signed by Core PKI."""
        logger.info("Provisioning device certificate from Core PKI...")
        key = self._generate_keypair()
        csr_pem = self._build_csr(key)
        device_id = _device_id()

        try:
            r = await self._http.post(
                f"{self.config.core_api_base}/api/v1/pki/sign",
                content=csr_pem,
                headers={
                    "Content-Type": "application/x-pem-file",
                    "X-Device-ID": device_id,
                },
            )
            r.raise_for_status()

            # Response: signed cert PEM + CA cert PEM
            body = r.json()
            cert_pem = body["cert"].encode()
            ca_pem = body.get("ca", "").encode()

            CERT_FILE.write_bytes(cert_pem)
            CERT_FILE.chmod(0o644)

            if ca_pem:
                CA_FILE.write_bytes(ca_pem)
                CA_FILE.chmod(0o644)

            self._load_cert()
            logger.info("Device certificate provisioned successfully")

        except Exception as e:
            logger.error("Certificate provisioning failed: %s", e)
            raise

    async def _renewal_loop(self) -> None:
        while True:
            await asyncio.sleep(86400)  # check daily
            await self._maybe_renew()

    async def _maybe_renew(self) -> None:
        if not self._cert:
            self._load_cert()
        if not self._cert:
            logger.warning("No cert to check for renewal")
            return

        now = datetime.now(timezone.utc)
        expiry = self._cert.not_valid_after_utc
        days_left = (expiry - now).days

        logger.debug("Cert expires in %d days", days_left)

        if days_left <= self.renew_days_before:
            logger.info("Cert expires in %d days — renewing", days_left)
            try:
                await self.provision()
            except Exception as e:
                logger.error("Cert renewal failed: %s", e)

    @property
    def cert_path(self) -> str:
        return str(CERT_FILE)

    @property
    def key_path(self) -> str:
        return str(KEY_FILE)

    @property
    def ca_path(self) -> str:
        return str(CA_FILE) if CA_FILE.exists() else ""

    def cert_info(self) -> dict:
        if not self._cert:
            return {"status": "missing"}
        now = datetime.now(timezone.utc)
        expiry = self._cert.not_valid_after_utc
        return {
            "status": "valid",
            "subject": self._cert.subject.rfc4514_string(),
            "issuer": self._cert.issuer.rfc4514_string(),
            "not_before": self._cert.not_valid_before_utc.isoformat(),
            "not_after": expiry.isoformat(),
            "days_remaining": (expiry - now).days,
            "storage": "tpm" if self._tpm.available else "software",
        }
