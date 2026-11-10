"""A throwaway self-signed certificate for the TLS tests.

Generated at test time, never committed: a private key in the repository would trip secret
scanning and would expire anyway. ``cryptography`` is an optional extra, so the TLS tests
skip themselves without it — exactly like the SigV4 cross-check against ``botocore``.
"""

from __future__ import annotations

import datetime
import os


def write_self_signed_pair(directory: str, common_name: str = "tf.example.lan") -> tuple[str, str]:
    """Write ``cert.pem``/``key.pem`` into *directory* and return their paths.

    The certificate is issued for a hostname, never for ``127.0.0.1``: that is the real
    world case the health probe has to survive, because a cert for the public name is
    what an operator mounts.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path
