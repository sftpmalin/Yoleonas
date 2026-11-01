"""Persistent local HTTPS trust for the NAS, shared by setup and service startup."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import ipaddress
import os
from pathlib import Path
import tempfile

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


CA_FILENAME = "yoleo-ca.crt"


def _write(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".certificate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _public_key(key) -> bytes:
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _private_bytes(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _names(addresses) -> list:
    names = [x509.DNSName("yoleo.local"), x509.DNSName("localhost")]
    for value in ("127.0.0.1", "::1", *addresses):
        # Scoped link-local IPv6 addresses cannot be certificate identities.
        if "%" in str(value):
            continue
        try:
            name = x509.IPAddress(ipaddress.ip_address(value))
        except ValueError:
            continue
        if name not in names:
            names.append(name)
    return names


def ensure_certificate(directory: Path, addresses=(), *, timestamp=None) -> tuple[Path, Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    lock_path = directory / ".certificate.lock"
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _ensure_certificate(directory, addresses, timestamp or datetime.now(timezone.utc))


def _ensure_certificate(directory, addresses, timestamp):
    ca_path, ca_key_path = directory / CA_FILENAME, directory / "yoleo-ca.key"
    cert_path, key_path = directory / "yoleo.crt", directory / "yoleo.key"
    if ca_path.exists() != ca_key_path.exists():
        raise RuntimeError("Autorité HTTPS locale incomplète : restaurer ensemble yoleo-ca.crt et yoleo-ca.key.")
    if ca_path.exists():
        # Never replace a trusted authority silently: PCs must keep trusting the
        # same identity after service restarts, address changes and leaf renewal.
        ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        if _public_key(ca_cert.public_key()) != _public_key(ca_key.public_key()):
            raise RuntimeError("Le certificat de confiance HTTPS et sa clé ne correspondent pas.")
        ca_cert.verify_directly_issued_by(ca_cert)
        if not ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise RuntimeError("Le certificat de confiance HTTPS n’est pas une autorité.")
        if not ca_cert.not_valid_before_utc <= timestamp < ca_cert.not_valid_after_utc - timedelta(days=31):
            raise RuntimeError("Le certificat de confiance HTTPS doit être renouvelé puis réinstallé sur les PC.")
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "YoLeo NAS OS - Autorite locale")])
        ca_cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(timestamp - timedelta(minutes=5)).not_valid_after(timestamp + timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                       key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                   .sign(ca_key, hashes.SHA256()))
        _write(ca_key_path, _private_bytes(ca_key), 0o600)
        _write(ca_path, ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)

    key = None
    if key_path.exists():
        try:
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except (ValueError, TypeError):
            pass
    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = _names(addresses)
    reusable = False
    if cert_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            cert.verify_directly_issued_by(ca_cert)
            reusable = (
                _public_key(cert.public_key()) == _public_key(key.public_key())
                and not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                and cert.not_valid_before_utc <= timestamp < cert.not_valid_after_utc - timedelta(days=30)
                and set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) == set(names)
                and ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            )
        except (ValueError, TypeError, InvalidSignature, x509.ExtensionNotFound):
            pass
    if not reusable:
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "yoleo.local")]))
                .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(timestamp - timedelta(minutes=5))
                .not_valid_after(min(timestamp + timedelta(days=397), ca_cert.not_valid_after_utc - timedelta(days=1)))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName(names), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False, key_agreement=False,
                    key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        _write(key_path, _private_bytes(key), 0o600)
        _write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    for path in (key_path, ca_key_path):
        path.chmod(0o600)
    for path in (cert_path, ca_path):
        path.chmod(0o644)
    return cert_path, key_path, ca_path


def register_certificate_download(blueprint, conf_directory) -> None:
    from flask import abort, send_file

    @blueprint.get("/system/https/certificat.crt")
    def system_https_certificate():
        # Fixed public file only. No parameter may select a key or another file.
        path = Path(conf_directory) / "https" / CA_FILENAME
        if not path.is_file():
            abort(404, description="Le certificat de confiance HTTPS local n’est pas disponible.")
        response = send_file(path, mimetype="application/x-x509-ca-cert", as_attachment=True,
                             download_name="YoLeo-local.crt", conditional=False, max_age=0)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--addresses", default="")
    arguments = parser.parse_args()
    certificate, key, authority = ensure_certificate(arguments.directory, arguments.addresses.split())
    print(f"HTTPS local : certificat serveur {certificate} ; certificat de confiance {authority}.")
