"""HTTPS для доступа по локальной сети: самоподписанный сертификат на адреса этого компьютера.

Без https браузеры не дают странице микрофон (кроме localhost). Сертификат выпускается
один раз и перевыпускается, если у компьютера появился новый адрес или срок подходит к концу.
Браузер один раз предупредит о «непроверенном сертификате» — это ожидаемо для домашней сети.
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VALID_DAYS = 825  # дольше Apple не принимает сертификаты серверов
RENEW_BEFORE = timedelta(days=30)


def local_addresses() -> list[str]:
    """IPv4-адреса компьютера в локальных сетях (Wi-Fi, Ethernet), без VPN-туннелей."""
    found: list[str] = []
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2", "en3"):
            try:
                ip = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=3).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                ip = ""
            if ip:
                found.append(ip)
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.168.0.1", 9))  # пакет не отправляется — только выбор интерфейса
                found.append(s.getsockname()[0])
        except OSError:
            pass
    return [ip for ip in dict.fromkeys(found) if ipaddress.ip_address(ip).is_private]


def host_names() -> list[str]:
    names = ["localhost"]
    try:
        if sys.platform == "darwin":
            local = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
            if local:
                names.append(f"{local}.local")
    except (OSError, subprocess.SubprocessError):
        pass
    host = socket.gethostname()
    if host and host not in names:
        names.append(host)
    return names


def _needs_renewal(cert_file: Path, names: list[str], ips: list[str]) -> bool:
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    except (OSError, ValueError):
        return True
    if cert.not_valid_after_utc - datetime.now(timezone.utc) < RENEW_BEFORE:
        return True
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    have_names = set(san.get_values_for_type(x509.DNSName))
    have_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    return not (set(names) <= have_names and set(ips) <= have_ips)


def ensure_certificate(folder: Path, names: list[str] | None = None, ips: list[str] | None = None) -> tuple[Path, Path]:
    """Возвращает (сертификат, ключ); выпускает новые при необходимости."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    names = names if names is not None else host_names()
    ips = sorted(set((ips if ips is not None else local_addresses()) + ["127.0.0.1"]))
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    if cert_file.exists() and key_file.exists() and not _needs_renewal(cert_file, names, ips):
        return cert_file, key_file

    folder.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LM Agent (локальная сеть)")])
    now = datetime.now(timezone.utc)
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    key_file.chmod(0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file
