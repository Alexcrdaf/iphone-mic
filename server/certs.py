"""Local CA and server TLS certificate management for LAN HTTPS/WSS."""

from __future__ import annotations

import datetime
import ipaddress
import socket
from pathlib import Path
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def get_local_ips() -> list[str]:
    """Return sorted non-loopback IPv4 addresses, prioritizing standard LAN ranges."""
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except OSError:
        pass

    # Probe active default-route interface IP without sending actual packets.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    def _priority(addr: str) -> tuple[int, str]:
        if addr.startswith("192.168."):
            return (0, addr)
        if addr.startswith("10."):
            return (1, addr)
        if addr.startswith("172."):
            return (2, addr)
        return (3, addr)

    return sorted(ips, key=_priority)


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _ensure_ca(ca_cert_path: Path, ca_key_path: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    if ca_cert_path.exists() and ca_key_path.exists():
        cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        if isinstance(key, rsa.RSAPrivateKey):
            return cert, key

    key = _generate_key()
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "iPhoneMic Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, "iPhoneMic Local Root CA"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )

    ca_key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def _cert_has_all_ips(cert_path: Path, ips: Iterable[str]) -> bool:
    if not cert_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        now = datetime.datetime.now(datetime.timezone.utc)
        if cert.not_valid_after_utc <= now + datetime.timedelta(days=7):
            return False
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        existing_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        return all(ip in existing_ips for ip in ips)
    except Exception:
        return False


def ensure_certificates(cert_dir: Path, extra_ips: Iterable[str] = ()) -> tuple[Path, Path, Path]:
    """Ensure local CA and LAN server TLS certificate exist and cover current local IPs.

    Returns (server_cert_path, server_key_path, ca_cert_path).
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_cert_path = cert_dir / "ca.crt"
    ca_key_path = cert_dir / "ca.key"
    srv_cert_path = cert_dir / "server.crt"
    srv_key_path = cert_dir / "server.key"

    ca_cert, ca_key = _ensure_ca(ca_cert_path, ca_key_path)

    all_ips = sorted({"127.0.0.1", *get_local_ips(), *extra_ips})
    if _cert_has_all_ips(srv_cert_path, all_ips) and srv_key_path.exists():
        return srv_cert_path, srv_key_path, ca_cert_path

    srv_key = _generate_key()
    now = datetime.datetime.now(datetime.timezone.utc)
    hostname = socket.gethostname()
    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(f"{hostname}.local"),
    ]
    for ip in all_ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "iPhoneMic Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, all_ips[0]),
        ]
    )

    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    srv_key_path.write_bytes(
        srv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    srv_cert_path.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    return srv_cert_path, srv_key_path, ca_cert_path
