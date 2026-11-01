"""SEC-01/SEC-02 regressions; synthetic TLS only, no network or app import."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import ssl
import tempfile
import unittest

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from httpx._config import create_ssl_context


REMOVED_G3_SHA256 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def trust_fingerprints(context):
    return {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True)}


def complete_handshake(client_context, server_context, hostname):
    """Exercise real OpenSSL chain/hostname verification through MemoryBIO."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(100):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        pending = client_out.read()
        if pending:
            server_in.write(pending)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        pending = server_out.read()
        if pending:
            client_in.write(pending)
        if client_done and server_done:
            return client.version()
    raise AssertionError("synthetic TLS handshake did not finish")


class SecurityTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="mathagent-tls-regression-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic regression CA")])
        now = datetime.now(timezone.utc)
        ca = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example")]))
            .issuer_name(ca_name)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.example")]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        cls.fixture_ca = root / "ca.pem"
        cls.fixture_ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert_path, key_path = root / "leaf.pem", root / "leaf.key"
        cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(str(cert_path), str(key_path))

    def client_context(self, trust_fixture=False):
        context = create_ssl_context(trust_env=False)
        if trust_fixture:
            context.load_verify_locations(cafile=str(self.fixture_ca))
        return context

    def test_certifi_bundle_excludes_removed_anchor(self):
        context = ssl.create_default_context(cafile=certifi.where())
        self.assertFalse(REMOVED_G3_SHA256 in trust_fingerprints(context), "certifi still trusts removed emSign G3")

    def test_httpx_defaults_exclude_removed_anchor_and_verify(self):
        context = self.client_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(REMOVED_G3_SHA256 in trust_fingerprints(context), "HTTPX still trusts removed emSign G3")

    def test_trusted_tls_chain_succeeds(self):
        version = complete_handshake(self.client_context(True), self.server_context, "fixture.example")
        self.assertIn(version, ("TLSv1.2", "TLSv1.3"))

    def test_untrusted_tls_chain_fails(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            complete_handshake(self.client_context(), self.server_context, "fixture.example")

    def test_wrong_hostname_fails(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            complete_handshake(self.client_context(True), self.server_context, "wrong.example")

    def test_minimum_certifi_version_is_declared(self):
        text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        match = re.search(r"(?m)^certifi>=(\d+)\.(\d+)\.(\d+)\s*$", text)
        self.assertIsNotNone(match, "SEC-01 must survive a newly resolved environment")
        self.assertGreaterEqual(tuple(map(int, match.groups())), (2026, 10, 23))


class NginxPolicyTests(unittest.TestCase):
    def test_nginx_template_disables_stateless_tickets(self):
        text = (PROJECT_ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
        directives = re.findall(r"(?m)^\s*ssl_session_tickets\s+([^;]+);", text)
        self.assertEqual(directives, ["off"])


if __name__ == "__main__":
    unittest.main()
