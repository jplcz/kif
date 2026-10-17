# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Signing and verifying `.kifm` files (docs/kif-module-contracts.md, section 11).

This is the only module that touches cryptography, and it contains **no primitives**: Ed25519, X.509 and CRL handling are done by a
crypto implementation that already exists on the host. It picks, in this order, the Python `cryptography` package and then the
`openssl` command line tool (run without a shell, fixed argument vectors, key material through files in a private temporary directory,
a timeout, OpenSSL >= 3.0). With neither, every operation raises `CryptoUnavailable`: verification is never skipped. `KIF_CRYPTO`
(`cryptography` or `openssl`) forces a back end, which is how the tests run both.

The signature covers the canonical CBOR encoding of the header map without its `signature` key; trust is only ever the consumer's
list (`TrustList`), never the key or certificate the file carries.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import cbor

ALG = "ed25519"
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")   # the fixed DER header of an Ed25519 SubjectPublicKeyInfo; the key follows
_TIMEOUT = 30
INSTALL = "install the Python `cryptography` package (pip install cryptography) or OpenSSL 3 (the `openssl` command)"


class CryptoError(Exception):
    """A key, certificate or list could not be used (a bad input, reported by name)."""


class CryptoUnavailable(CryptoError):
    """No crypto implementation on this host; signing and verification cannot proceed (never degraded to unsigned)."""


def fingerprint(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- PEM / DER (encoding only)

_PEM = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----\s*(.*?)\s*-----END \1-----", re.S)


def pem_blocks(data: bytes) -> List[Tuple[str, bytes]]:
    """`(label, DER)` for every PEM block of `data`."""
    out = []
    for m in _PEM.finditer(data):
        try:
            out.append((m.group(1).decode(), base64.b64decode(b"".join(m.group(2).split()), validate=True)))
        except ValueError as exc:
            raise CryptoError(f"malformed PEM block {m.group(1).decode()!r}") from exc
    return out


def to_pem(label: str, der: bytes) -> bytes:
    body = base64.b64encode(der).decode()
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode()


def raw_of_spki(der: bytes) -> bytes:
    if not (len(der) == len(_SPKI_PREFIX) + 32 and der.startswith(_SPKI_PREFIX)):
        raise CryptoError("not an Ed25519 public key (only Ed25519 is supported)")
    return der[len(_SPKI_PREFIX):]


@dataclass(frozen=True)
class CertInfo:
    public_key: Optional[bytes]   # the raw Ed25519 key, None for a certificate of another algorithm (a CA)
    subject: str
    serial: int
    not_before: str
    not_after: str
    is_ca: bool
    issuer: str


# ---------------------------------------------------------------- back end: the `cryptography` package


class _Cryptography:
    name = "cryptography"

    def __init__(self) -> None:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        self.x509, self.InvalidSignature, self.ser, self.ed = x509, InvalidSignature, serialization, ed25519

    def private_public(self, pem: bytes) -> bytes:
        try:
            key = self.ser.load_pem_private_key(pem, None)
        except Exception as exc:
            raise CryptoError(f"not a usable private key: {exc}") from exc
        if not isinstance(key, self.ed.Ed25519PrivateKey):
            raise CryptoError("not an Ed25519 private key (only Ed25519 is supported)")
        return key.public_key().public_bytes(self.ser.Encoding.Raw, self.ser.PublicFormat.Raw)

    def sign(self, pem: bytes, data: bytes) -> bytes:
        self.private_public(pem)
        return self.ser.load_pem_private_key(pem, None).sign(data)

    def verify(self, public: bytes, sig: bytes, data: bytes) -> bool:
        try:
            self.ed.Ed25519PublicKey.from_public_bytes(public).verify(sig, data)
            return True
        except (self.InvalidSignature, ValueError):
            return False

    def _cert(self, der: bytes):
        try:
            return self.x509.load_der_x509_certificate(der)
        except Exception as exc:
            raise CryptoError(f"not a usable certificate: {exc}") from exc

    def cert_info(self, der: bytes) -> CertInfo:
        c = self._cert(der)
        key = c.public_key()
        raw = key.public_bytes(self.ser.Encoding.Raw, self.ser.PublicFormat.Raw) if isinstance(key, self.ed.Ed25519PublicKey) else None
        try:
            ca = c.extensions.get_extension_for_class(self.x509.BasicConstraints).value.ca
        except self.x509.ExtensionNotFound:
            ca = False
        return CertInfo(raw, c.subject.rfc4514_string(), c.serial_number, c.not_valid_before_utc.strftime("%Y-%m-%d"),
                        c.not_valid_after_utc.strftime("%Y-%m-%d"), ca, c.issuer.rfc4514_string())

    def issued_by(self, cert: bytes, ca: bytes) -> bool:
        c, a = self._cert(cert), self._cert(ca)
        if not self.cert_info(ca).is_ca:
            return False
        try:
            c.verify_directly_issued_by(a)
            return True
        except Exception:
            return False

    def crl_revokes(self, cert: bytes, crl_pem: bytes, ca: bytes) -> Optional[bool]:
        """None: the CRL is not issued and signed by `ca` (ignored); else whether it lists `cert`."""
        try:
            crl = self.x509.load_pem_x509_crl(crl_pem)
        except Exception as exc:
            raise CryptoError(f"not a usable CRL: {exc}") from exc
        a, c = self._cert(ca), self._cert(cert)
        if crl.issuer != a.subject or not crl.is_signature_valid(a.public_key()):
            return None
        return c.issuer == crl.issuer and crl.get_revoked_certificate_by_serial_number(c.serial_number) is not None


# ---------------------------------------------------------------- back end: the `openssl` command line


class _OpenSSL:
    name = "openssl"

    def __init__(self, exe: str) -> None:
        self.exe = exe
        out = self._run(["version"]).stdout.decode(errors="replace")
        m = re.match(r"\S+ (\d+)\.", out)
        if not m or int(m.group(1)) < 3:
            raise CryptoUnavailable(f"OpenSSL 3 or newer is required, found {out.strip()!r}")

    def _run(self, args: Sequence[str], cwd: Optional[str] = None) -> "subprocess.CompletedProcess[bytes]":
        try:
            return subprocess.run([self.exe, *args], capture_output=True, timeout=_TIMEOUT, cwd=cwd, shell=False, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CryptoUnavailable(f"cannot run {self.exe}: {exc}") from exc

    def _work(self, **files: bytes):
        d = tempfile.TemporaryDirectory(prefix="kif-crypto-")
        os.chmod(d.name, 0o700)
        paths = {}
        for name, data in files.items():
            paths[name] = os.path.join(d.name, name)
            with open(os.open(paths[name], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(data)
        return d, paths

    def private_public(self, pem: bytes) -> bytes:
        d, p = self._work(key=pem)
        with d:
            r = self._run(["pkey", "-in", p["key"], "-pubout", "-outform", "DER"])
        if r.returncode != 0:
            raise CryptoError("not a usable private key")
        return raw_of_spki(r.stdout)

    def sign(self, pem: bytes, data: bytes) -> bytes:
        self.private_public(pem)
        d, p = self._work(key=pem, data=data)
        with d:
            r = self._run(["pkeyutl", "-sign", "-inkey", p["key"], "-rawin", "-in", p["data"], "-out", os.path.join(d.name, "sig")])
            if r.returncode != 0:
                raise CryptoError("signing failed: " + r.stderr.decode(errors="replace").strip())
            with open(os.path.join(d.name, "sig"), "rb") as f:
                return f.read()

    def verify(self, public: bytes, sig: bytes, data: bytes) -> bool:
        if len(public) != 32 or len(sig) != 64:
            return False
        d, p = self._work(pub=_SPKI_PREFIX + public, sig=sig, data=data)
        with d:
            r = self._run(["pkeyutl", "-verify", "-pubin", "-keyform", "DER", "-inkey", p["pub"], "-rawin", "-in", p["data"], "-sigfile", p["sig"]])
        return r.returncode == 0

    def _x509(self, der: bytes, *args: str) -> str:
        d, p = self._work(cert=der)
        with d:
            r = self._run(["x509", "-inform", "DER", "-in", p["cert"], "-noout", *args])
        if r.returncode != 0:
            raise CryptoError("not a usable certificate")
        return r.stdout.decode(errors="replace")

    def cert_info(self, der: bytes) -> CertInfo:
        text = self._x509(der, "-subject", "-issuer", "-serial", "-dates", "-nameopt", "RFC2253")
        kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

        def day(s: str) -> str:
            return datetime.datetime.strptime(" ".join(s.split()), "%b %d %H:%M:%S %Y GMT").strftime("%Y-%m-%d")

        d, p = self._work(cert=der)
        with d:
            pub = self._run(["x509", "-inform", "DER", "-in", p["cert"], "-noout", "-pubkey"]).stdout
            ext = self._run(["x509", "-inform", "DER", "-in", p["cert"], "-noout", "-ext", "basicConstraints"]).stdout.decode(errors="replace")
        raw: Optional[bytes] = None
        blocks = pem_blocks(pub)
        if blocks:
            try:
                raw = raw_of_spki(blocks[0][1])
            except CryptoError:
                raw = None
        return CertInfo(raw, kv["subject"], int(kv["serial"], 16), day(kv["notBefore"]), day(kv["notAfter"]), "CA:TRUE" in ext, kv["issuer"])

    def issued_by(self, cert: bytes, ca: bytes) -> bool:
        if not self.cert_info(ca).is_ca:
            return False
        d, p = self._work(cert=to_pem("CERTIFICATE", cert), ca=to_pem("CERTIFICATE", ca))
        with d:
            r = self._run(["verify", "-no_check_time", "-partial_chain", "-no-CApath", "-no-CAstore", "-CAfile", p["ca"], p["cert"]])
        return r.returncode == 0

    def crl_revokes(self, cert: bytes, crl_pem: bytes, ca: bytes) -> Optional[bool]:
        d, p = self._work(crl=crl_pem, ca=to_pem("CERTIFICATE", ca))
        with d:
            ok = self._run(["crl", "-in", p["crl"], "-noout", "-CAfile", p["ca"]])
            text = self._run(["crl", "-in", p["crl"], "-noout", "-issuer", "-nameopt", "RFC2253"])
            serials = self._run(["crl", "-in", p["crl"], "-noout", "-text"])
        if ok.returncode != 0 or b"verify OK" not in (ok.stdout + ok.stderr):
            return None
        a, c = self.cert_info(ca), self.cert_info(cert)
        issuer = text.stdout.decode(errors="replace").split("=", 1)[-1].strip()
        if issuer != a.subject:
            return None
        listed = {int(s, 16) for s in re.findall(r"Serial Number: ([0-9A-Fa-f]+)", serials.stdout.decode(errors="replace"))}
        return c.issuer == issuer and c.serial in listed


# ---------------------------------------------------------------- selection


def backend(name: Optional[str] = None):
    """The crypto back end: `name` or `$KIF_CRYPTO` forces one; else `cryptography`, then `openssl`. `CryptoUnavailable` when none."""
    want = name or os.environ.get("KIF_CRYPTO") or None
    errors = []
    if want in (None, "cryptography"):
        try:
            return _Cryptography()
        except ImportError as exc:
            errors.append(f"the Python `cryptography` package is not importable ({exc})")
    if want in (None, "openssl"):
        exe = shutil.which("openssl")
        if exe:
            return _OpenSSL(exe)
        errors.append("the `openssl` command was not found")
    if want not in (None, "cryptography", "openssl"):
        errors.append(f"unknown crypto back end {want!r}")
    raise CryptoUnavailable("no cryptography available, so signatures can be neither made nor verified: " + "; ".join(errors) + ". " + INSTALL)


def available() -> List[str]:
    """The back ends usable on this host (the tests run each of them)."""
    out = []
    for n in ("cryptography", "openssl"):
        try:
            backend(n)
            out.append(n)
        except CryptoUnavailable:
            pass
    return out


# ---------------------------------------------------------------- signing


def signed_part(header: Dict[str, Any]) -> bytes:
    """The bytes a signature covers: the canonical encoding of the header map without `signature`."""
    return cbor.dumps({k: v for k, v in header.items() if k != "signature"})


def _der_of(data: bytes, label: str) -> bytes:
    blocks = [d for l, d in pem_blocks(data) if l == label]
    return blocks[0] if blocks else (data if not data.lstrip().startswith(b"-----") else b"")


def sign(header: Dict[str, Any], key_pem: bytes, certificate: Optional[bytes] = None, crypto=None) -> Dict[str, Any]:
    """`header` with a `signature` made by the Ed25519 private key `key_pem` (PKCS#8 PEM); `certificate` (PEM or DER) is embedded when given
    and its public key must be the signing key's. Deterministic."""
    crypto = crypto or backend()
    public = crypto.private_public(key_pem)
    sig: Dict[str, Any] = {"alg": ALG, "public_key": public}
    if certificate is not None:
        der = _der_of(certificate, "CERTIFICATE")
        if not der:
            raise CryptoError("the certificate is neither PEM nor DER")
        info = crypto.cert_info(der)
        if info.public_key != public:
            raise CryptoError("the certificate's public key is not the signing key's: a mismatched pair would give a file no one can verify")
        sig["certificate"], sig["signer"] = der, info.subject
    else:
        sig["signer"] = "key " + fingerprint(public)[:23]
    sig["value"] = crypto.sign(key_pem, signed_part(header))
    return {**{k: v for k, v in header.items() if k != "signature"}, "signature": sig}


def validate_shape(sig: Any) -> None:
    """`ValueError` unless `sig` has the shape of section 11.1."""
    if not isinstance(sig, dict):
        raise ValueError("signature must be a map")
    if sig.get("alg") != ALG:
        raise ValueError(f"unsupported signature algorithm {sig.get('alg')!r}")
    if not isinstance(sig.get("signer"), str):
        raise ValueError("signature.signer must be a string")
    for key, size in (("public_key", 32), ("value", 64)):
        if not isinstance(sig.get(key), bytes) or len(sig[key]) != size:
            raise ValueError(f"signature.{key} must be {size} bytes")
    if "certificate" in sig and not isinstance(sig["certificate"], bytes):
        raise ValueError("signature.certificate must be bytes")
    extra = set(sig) - {"alg", "signer", "public_key", "value", "certificate"}
    if extra:
        raise ValueError(f"unknown signature keys {sorted(extra)}")


# ---------------------------------------------------------------- trust and revocation lists


@dataclass
class KeyList:
    """Pinned (trust) or revoked material from PEM files: Ed25519 public keys, certificates, CRLs."""

    keys: Dict[bytes, str] = field(default_factory=dict)
    certs: Dict[bytes, str] = field(default_factory=dict)
    crls: List[Tuple[bytes, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.keys or self.certs or self.crls)


def load_list(paths: Sequence[str], crypto=None) -> KeyList:
    """Read PEM files of public keys, certificates and CRLs. A file that is unreadable or holds something else is `CryptoError`."""
    out = KeyList()
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise CryptoError(f"{path}: {exc.strerror or exc}") from exc
        blocks = pem_blocks(data)
        if not blocks:
            raise CryptoError(f"{path}: no PEM public key, certificate or CRL found")
        for label, der in blocks:
            if label == "PUBLIC KEY":
                try:
                    out.keys[raw_of_spki(der)] = path
                except CryptoError as exc:
                    raise CryptoError(f"{path}: {exc}") from exc
            elif label == "CERTIFICATE":
                out.certs[der] = path
            elif label == "X509 CRL":
                out.crls.append((to_pem(label, der), path))
            else:
                raise CryptoError(f"{path}: unexpected PEM block {label!r} (public keys, certificates and CRLs only; never put a private key here)")
    return out


# ---------------------------------------------------------------- verification


@dataclass
class Verdict:
    status: str                      # unsigned | bad | untrusted | revoked | trusted
    signer: str = ""
    fingerprint: str = ""            # of the signing public key
    detail: str = ""
    validity: Optional[Tuple[str, str]] = None   # the certificate's dates, printed, never enforced
    notes: List[str] = field(default_factory=list)


def verify(header: Dict[str, Any], trust: Optional[KeyList] = None, revoked: Optional[KeyList] = None,
           allow_revoked: Sequence[str] = (), crypto=None) -> Verdict:
    """The verdict on `header`'s signature (section 11.3): `bad` when it does not verify; else `revoked` (wins over trust), else `trusted`
    when the key is pinned or its certificate is pinned or issued by a pinned certificate, else `untrusted`. `unsigned` has no `signature`."""
    sig = header.get("signature")
    if sig is None:
        return Verdict("unsigned")
    try:
        validate_shape(sig)
    except ValueError as exc:
        return Verdict("bad", detail=str(exc))
    crypto = crypto or backend()
    key = sig["public_key"]
    v = Verdict("untrusted", sig["signer"], fingerprint(key))
    if not crypto.verify(key, sig["value"], signed_part(header)):
        return Verdict("bad", v.signer, v.fingerprint, "the signature does not verify: the file was changed after signing")
    cert = sig.get("certificate")
    info = None
    if cert is not None:
        try:
            info = crypto.cert_info(cert)
        except CryptoError as exc:
            return Verdict("bad", v.signer, v.fingerprint, f"the embedded certificate is unusable: {exc}")
        if info.public_key != key:
            return Verdict("bad", v.signer, v.fingerprint, "the embedded certificate is for another key")
        v.validity = (info.not_before, info.not_after)
    exempt = set(allow_revoked)
    gone = _revoked(key, cert, revoked, trust, crypto, v.notes)
    if gone and not ({v.fingerprint, fingerprint(cert) if cert else ""} & exempt):
        return Verdict("revoked", v.signer, v.fingerprint, f"revoked ({gone})", v.validity, v.notes)
    if gone:
        v.notes.append(f"revoked ({gone}), exempted by allow-revoked")
    if trust is not None:
        if key in trust.keys:
            v.status, v.detail = "trusted", f"pinned key ({trust.keys[key]})"
        elif cert is not None and cert in trust.certs:
            v.status, v.detail = "trusted", f"pinned certificate ({trust.certs[cert]})"
        elif cert is not None:
            for ca, path in trust.certs.items():
                if crypto.issued_by(cert, ca):
                    v.status, v.detail = "trusted", f"issued by the pinned certificate {crypto.cert_info(ca).subject} ({path})"
                    break
    if v.status != "trusted":
        v.detail = "its key is not in the trust list"
    return v


def _revoked(key: bytes, cert: Optional[bytes], revoked: Optional[KeyList], trust: Optional[KeyList], crypto, notes: List[str]) -> str:
    """Why the key or certificate is revoked, '' when it is not (section 11.5). A CRL counts only when a *pinned* certificate issued and
    signed it; otherwise it is ignored with a note."""
    if revoked is None or revoked.empty:
        return ""
    if key in revoked.keys:
        return f"key listed in {revoked.keys[key]}"
    if cert is None:
        return ""
    if cert in revoked.certs:
        return f"certificate listed in {revoked.certs[cert]}"
    for ca, path in revoked.certs.items():
        if crypto.issued_by(cert, ca):
            return f"issued by a revoked certificate ({path})"
    for crl, path in revoked.crls:
        verdicts = [crypto.crl_revokes(cert, crl, ca) for ca in (trust.certs if trust is not None else {})]
        if not any(r is not None for r in verdicts):
            notes.append(f"the CRL {path} is not issued by a pinned certificate and was ignored")
        elif any(verdicts):
            return f"serial listed in the CRL {path}"
    return ""
