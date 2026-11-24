"""The failover link's own certificate, and connections that trust only the paired NVR's (#502, part 2; the owner's decision of 24 November 2026).

The NVR has no certificate of its own for its web interface (HTTPS comes from the tunnel or proxy in front of it), so it makes one for the
failover link alone: a self-signed certificate (an EC P-256 key) made the first time it is needed and kept beside the credential key, with
the private key encrypted at rest under that key (``app.credentials``). Neither file is in a backup, so each NVR, a standby restored
from the primary's backup included, has its own.

**Pinning.** When the two NVRs are paired, each is given the other's fingerprint: the SHA-256 of the other's whole certificate (DER), as
``openssl x509 -noout -fingerprint -sha256`` and a browser show it, so an administrator can check it with tools of their own. The
certificate is never renewed by itself (its validity is not what is checked), so pinning the public key instead would buy nothing; a new
certificate means a new key and a new pairing either way. A connection to the other NVR checks the certificate it presents against that
fingerprint during the TLS handshake, before anything is sent, and trusts no certificate authority: a different certificate is refused.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import logging
import os
import re
import secrets
import ssl
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpcore
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .credentials import decrypt_bytes, encrypt_bytes, key_path

logger = logging.getLogger("nvr.failover")

DEFAULT_PORT = 8443
CERTIFICATE_FILE, KEY_FILE, LOCK_FILE = "failover-link.crt", "failover-link.key", ".failover-link.lock"
VALID_DAYS = 25 * 365  # Not checked by either NVR (the pin is): long, so that no tool that does look at it calls it expired
FINGERPRINT_HELP = "The certificate fingerprint must be the 64 hexadecimal characters (SHA-256) shown on the other NVR's failover page"


def directory() -> Path:
    """Where the link's certificate and key are kept: with the NVR's other keys, beside the credential key."""
    return key_path().parent


def certificate_path() -> Path:
    return directory() / CERTIFICATE_FILE


def key_file_path() -> Path:
    return directory() / KEY_FILE


def fingerprint_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def normalize_fingerprint(text: str) -> str:
    """A fingerprint as pasted (with or without colons or spaces, in either case) as 64 lower-case hexadecimal characters."""
    value = re.sub(r"[\s:\-]", "", text or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(FINGERPRINT_HELP)
    return value


def display(fingerprint: str | None) -> str | None:
    """As ``openssl`` shows it: upper case, in pairs separated by colons."""
    return ":".join(fingerprint[index:index + 2] for index in range(0, len(fingerprint), 2)).upper() if fingerprint else None


# --- this NVR's own certificate ------------------------------------------------------------------------------------------------------------

_lock = threading.Lock()
_known: dict[str, tuple[tuple[Any, ...], bytes, str]] = {}  # Per certificate file: what both files looked like when checked, the certificate, its fingerprint


@contextlib.contextmanager
def _files_locked() -> Iterator[None]:
    """One maker at a time, across this NVR's threads and processes."""
    directory().mkdir(parents=True, exist_ok=True)
    with _lock, open(directory() / LOCK_FILE, "a+b") as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _seen() -> tuple[Any, ...] | None:
    try:
        found = [path.stat() for path in (certificate_path(), key_file_path(), key_path())]
    except FileNotFoundError:
        return None
    return tuple((item.st_mtime_ns, item.st_size, item.st_ino) for item in found)


def _private_key() -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(decrypt_bytes(key_file_path().read_bytes()), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("The failover link's key is not of the kind this NVR makes")
    return key


def _write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _make() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NVR failover link"), x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, secrets.token_hex(8))])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=VALID_DAYS))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False, data_encipherment=False, key_agreement=False,
                                                key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(key, hashes.SHA256()))
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    _write(key_file_path(), encrypt_bytes(private))  # Encrypted at rest with the credential key; never written in the clear
    _write(certificate_path(), certificate.public_bytes(serialization.Encoding.PEM))  # Last: a certificate on disk always has its key beside it


def certificate(*, replace: bool = False) -> tuple[bytes, str]:
    """This NVR's link certificate (PEM) and its fingerprint, made the first time it is asked for (or anew with ``replace``).

    A certificate whose key cannot be read with this NVR's credential key (the key file restored from another NVR's backup, as when a standby
    is set up) cannot be served: a new one is made, and the other NVR, which pinned the old one, refuses this one until it is paired again."""
    seen = _seen()
    held = _known.get(str(certificate_path()))
    if not replace and seen is not None and held is not None and held[0] == seen:
        return held[1], held[2]
    with _files_locked():
        if not replace and certificate_path().exists() and key_file_path().exists():
            try:
                pem = certificate_path().read_bytes()
                found = x509.load_pem_x509_certificate(pem)
                if found.public_key().public_numbers() == _private_key().public_key().public_numbers():
                    fingerprint = fingerprint_of(found.public_bytes(serialization.Encoding.DER))
                    _known[str(certificate_path())] = (_seen(), pem, fingerprint)
                    return pem, fingerprint
                logger.warning("The failover link's certificate and key do not belong together: a new certificate was made; pair the other NVR again with its fingerprint")
            except (ValueError, RuntimeError, OSError) as exc:  # The message never holds the key: it is the type of what went wrong
                logger.warning("The failover link's key could not be read with this NVR's credential key (%s): a new certificate was made; "
                               "pair the other NVR again with its fingerprint", type(exc).__name__)
        _make()
        pem = certificate_path().read_bytes()
        fingerprint = fingerprint_of(x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER))
        _known[str(certificate_path())] = (_seen(), pem, fingerprint)
        return pem, fingerprint


def server_context() -> tuple[ssl.SSLContext, str]:
    """The TLS context the link is served with, and the fingerprint of its certificate.

    Python loads a key only from a file, so the key is written for a moment, encrypted with a one-time password, to a file only this user can
    read, loaded and removed: it is never on disk in the clear."""
    _, fingerprint = certificate()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    password = secrets.token_bytes(32)
    sealed = _private_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password))
    descriptor, temporary = tempfile.mkstemp(prefix=".failover-link-load-", dir=directory())
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(sealed)
        context.load_cert_chain(certificate_path(), temporary, password=password)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return context, fingerprint


# --- connections to the other NVR ----------------------------------------------------------------------------------------------------------

class CertificateMismatch(httpx.TransportError):
    """The other NVR's link presented a certificate other than the one it was paired with: the connection is closed before anything is sent."""

    def __init__(self, presented: str, pinned: str) -> None:
        super().__init__(f"The other NVR presented a certificate with fingerprint {display(presented) or 'none'}, not the one it was paired with ({display(pinned)})")
        self.presented, self.pinned = presented, pinned


class _PinnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner: httpcore.AsyncNetworkStream, fingerprint: str) -> None:
        self._inner, self._fingerprint = inner, fingerprint

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._inner.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._inner.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.start_tls(ssl_context, server_hostname, timeout)
        tls = stream.get_extra_info("ssl_object")
        der = tls.getpeercert(binary_form=True) if tls is not None else None
        presented = fingerprint_of(der) if der else ""
        if not hmac.compare_digest(presented, self._fingerprint):
            await stream.aclose()
            raise CertificateMismatch(presented, self._fingerprint)
        return stream


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, fingerprint: str) -> None:
        self._inner, self._fingerprint = httpcore.AnyIOBackend(), fingerprint

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        return _PinnedStream(await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address, socket_options=socket_options), self._fingerprint)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("The failover link is not served on a local socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def pinned_transport(fingerprint: str) -> httpx.AsyncHTTPTransport:
    """A transport that connects only to the certificate with this fingerprint. No certificate authority is trusted and the name is not
    checked (the other NVR is reached by an address, and its certificate is its own): the fingerprint is the whole of the trust."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Verified by the fingerprint instead, before a byte of the request is sent
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    transport = httpx.AsyncHTTPTransport(verify=context, trust_env=False)
    # httpx takes no network backend of its own: its pool is made again with the one that checks the certificate. tests/test_failover_https.py
    # serves a different certificate from a real TLS server, so an httpx that stopped honouring this would fail there, not quietly trust it.
    transport._pool = httpcore.AsyncConnectionPool(ssl_context=context, max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0,
                                                   http1=True, http2=False, network_backend=_PinnedBackend(fingerprint))
    return transport
