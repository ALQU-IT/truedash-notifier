"""
Generates a self-signed TLS certificate on first startup and stores it in
/data/ so the fingerprint is stable across container restarts. The iOS app
TOFU-pins the certificate fingerprint on first connection.
"""
import datetime
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

CERT_PATH = Path(os.getenv("CONFIG_PATH", "/data/config.json")).parent / "cert.pem"
KEY_PATH  = CERT_PATH.parent / "key.pem"


def ensure_cert() -> tuple[Path, Path]:
    if CERT_PATH.exists() and KEY_PATH.exists():
        return CERT_PATH, KEY_PATH

    log.info("Generating self-signed TLS certificate")

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "truedash-notifier")])
    now  = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("truedash-notifier")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    CERT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Write key with restricted permissions before writing cert.
    key_fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(key_fd, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    log.info(f"Certificate written to {CERT_PATH}")
    return CERT_PATH, KEY_PATH
