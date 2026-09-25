"""Completes broken HTTPS certificate chains (common on .gov.bd sites) the same way browsers do."""
from __future__ import annotations

import socket
import ssl
import sys
import tempfile
from pathlib import Path

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

HEADERS = {"User-Agent": "AloScheduleBot/1.0 (+public load shedding schedule reader)"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


_BUNDLES: dict[str, str] = {}


def _load_certs(data: bytes):
    for loader in (lambda d: [x509.load_der_x509_certificate(d)],
                   lambda d: [x509.load_pem_x509_certificate(d)],
                   pkcs7.load_der_pkcs7_certificates,
                   pkcs7.load_pem_pkcs7_certificates):
        try:
            return loader(data)
        except Exception:
            continue
    return []


def bundle_with_intermediates(host: str, port: int = 443) -> str:
    """Many .gov.bd servers forget to send their intermediate certificate. Browsers fetch it
    themselves (from the 'CA Issuers' link inside the certificate); Python doesn't. We do the
    same thing here, then verify normally against the standard roots plus that intermediate.
    Verification stays ON: the intermediate still has to chain up to a trusted root."""
    if host in _BUNDLES:
        return _BUNDLES[host]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # only to read the server's certificate, nothing is trusted from it
    with socket.create_connection((host, port), timeout=20) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
    pems = []
    for _ in range(3):
        try:
            aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
        if not urls:
            break
        issuers = _load_certs(requests.get(urls[0], timeout=20, headers=HEADERS).content)
        if not issuers:
            break
        pems += [c.public_bytes(Encoding.PEM).decode() for c in issuers]
        cert = issuers[0]
        if cert.issuer == cert.subject:
            break
    if not pems:
        raise RuntimeError(f"{host}: certificate chain incomplete and no issuer link found")
    path = Path(tempfile.gettempdir()) / f"alo-ca-{host}.pem"
    path.write_text(Path(certifi.where()).read_text() + "\n" + "".join(pems))
    _BUNDLES[host] = str(path)
    log(f"       fetched missing intermediate certificate for {host}")
    return str(path)
