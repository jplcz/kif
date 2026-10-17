# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""Signing and verification primitives (docs/kif-module-contracts.md, section 11): each crypto back end present on the host runs the same
corpus and must give the same verdicts. The test keys, certificates and CRLs are made with the `openssl` tool (test fixtures only)."""

import shutil
import subprocess

import pytest

from kiflang import modsign

OPENSSL = shutil.which("openssl")
pytestmark = pytest.mark.skipif(not OPENSSL, reason="the openssl tool makes the test PKI")
BACKENDS = modsign.available()


def run(*args, cwd):
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    for n in ("ca", "ca2", "vendor", "other"):
        run("genpkey", "-algorithm", "ed25519", "-out", f"{n}.pem", cwd=d)
    run("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", "rsa.pem", cwd=d)
    for ca in ("ca", "ca2"):
        run("req", "-x509", "-new", "-key", f"{ca}.pem", "-subj", f"/CN=Test {ca}", "-days", "3650", "-addext", "basicConstraints=critical,CA:TRUE",
            "-out", f"{ca}.crt", cwd=d)
    run("req", "-new", "-key", "vendor.pem", "-subj", "/CN=Acme Drivers", "-out", "v.csr", cwd=d)
    run("x509", "-req", "-in", "v.csr", "-CA", "ca.crt", "-CAkey", "ca.pem", "-set_serial", "7", "-days", "3650", "-out", "v.crt", cwd=d)
    run("x509", "-req", "-in", "v.csr", "-CA", "ca.crt", "-CAkey", "ca.pem", "-set_serial", "8", "-not_before", "20200101000000Z",
        "-not_after", "20210101000000Z", "-out", "old.crt", cwd=d)
    (d / "c.cnf").write_text("[ca]\ndefault_ca=d\n[d]\ndatabase=index.txt\ncrlnumber=crlnumber\ndefault_md=default\ndefault_crl_days=30\n")
    for ca in ("ca", "ca2"):
        (d / "index.txt").write_text("")
        (d / "crlnumber").write_text("01\n")
        run("ca", "-config", "c.cnf", "-cert", f"{ca}.crt", "-keyfile", f"{ca}.pem", "-revoke", "v.crt", cwd=d)
        run("ca", "-config", "c.cnf", "-cert", f"{ca}.crt", "-keyfile", f"{ca}.pem", "-gencrl", "-out", f"{ca}.crl", cwd=d)
    for n in ("vendor", "other"):
        run("pkey", "-in", f"{n}.pem", "-pubout", "-out", f"{n}.pub", cwd=d)
    return d


def read(pki, name):
    return (pki / name).read_bytes()


def lst(pki, *names, crypto=None):
    return modsign.load_list([str(pki / n) for n in names])


HEADER = {"magic": "KIFM", "name": "ddk", "text": "namespace ddk;", "security": {"target": "c"}}


@pytest.fixture(params=BACKENDS)
def crypto(request):
    return modsign.backend(request.param)


def signed(pki, crypto, key="vendor.pem", cert=None, header=HEADER):
    return modsign.sign(header, read(pki, key), read(pki, cert) if cert else None, crypto)


def test_at_least_one_backend_is_present():
    assert BACKENDS


def test_sign_and_verify_with_a_pinned_key(pki, crypto):
    v = modsign.verify(signed(pki, crypto), lst(pki, "vendor.pub"), None, crypto=crypto)
    assert v.status == "trusted" and v.fingerprint.startswith("sha256:") and "pinned key" in v.detail
    assert v.signer.startswith("key sha256:")


def test_certificate_signed_by_a_pinned_ca_and_a_pinned_certificate(pki, crypto):
    h = signed(pki, crypto, cert="v.crt")
    via_ca = modsign.verify(h, lst(pki, "ca.crt"), None, crypto=crypto)
    assert via_ca.status == "trusted" and "issued by the pinned certificate" in via_ca.detail and via_ca.signer == "CN=Acme Drivers"
    assert modsign.verify(h, lst(pki, "v.crt"), None, crypto=crypto).status == "trusted"
    assert modsign.verify(h, lst(pki, "ca2.crt"), None, crypto=crypto).status == "untrusted"   # another CA does not vouch for it


def test_a_changed_byte_is_a_bad_signature(pki, crypto):
    h = signed(pki, crypto)
    trust = lst(pki, "vendor.pub")
    for edit in (lambda x: x.update(text="namespace evil;"), lambda x: x["security"].update(target="freebsd-kernel"),
                 lambda x: x.update(extra=1), lambda x: x["signature"].update(value=bytes(64))):
        import copy
        m = copy.deepcopy(h)
        edit(m)
        assert modsign.verify(m, trust, None, crypto=crypto).status == "bad"


def test_valid_signature_by_an_unpinned_key_and_no_trust_list(pki, crypto):
    h = signed(pki, crypto, key="other.pem")
    assert modsign.verify(h, lst(pki, "vendor.pub"), None, crypto=crypto).status == "untrusted"
    assert modsign.verify(h, None, None, crypto=crypto).status == "untrusted"


def test_unsigned_and_a_deleted_signature_are_the_same(pki, crypto):
    h = {k: v for k, v in signed(pki, crypto).items() if k != "signature"}
    assert modsign.verify(h, lst(pki, "vendor.pub"), None, crypto=crypto).status == "unsigned"


def test_refused_inputs(pki, crypto):
    with pytest.raises(modsign.CryptoError, match="mismatched"):
        modsign.sign(HEADER, read(pki, "other.pem"), read(pki, "v.crt"), crypto)
    with pytest.raises(modsign.CryptoError, match="Ed25519"):
        modsign.sign(HEADER, read(pki, "rsa.pem"), None, crypto)
    with pytest.raises(modsign.CryptoError):
        modsign.sign(HEADER, b"not a key", None, crypto)
    with pytest.raises(modsign.CryptoError, match="private key"):
        modsign.load_list([str(pki / "vendor.pem")])


def test_expired_certificate_is_accepted_and_the_dates_are_reported(pki, crypto):
    h = signed(pki, crypto, cert="old.crt")
    v = modsign.verify(h, lst(pki, "ca.crt"), None, crypto=crypto)
    assert v.status == "trusted" and v.validity == ("2020-01-01", "2021-01-01")
    assert modsign.verify(h, lst(pki, "vendor.pub"), None, crypto=crypto).status == "trusted"   # the key itself is pinned
    assert modsign.verify(h, None, None, crypto=crypto).status == "untrusted"                  # removed from the trust list


def test_revoked_key_wins_over_the_trust_list(pki, crypto):
    h = signed(pki, crypto)
    v = modsign.verify(h, lst(pki, "vendor.pub"), lst(pki, "vendor.pub"), crypto=crypto)
    assert v.status == "revoked" and "key listed" in v.detail
    ok = modsign.verify(h, lst(pki, "vendor.pub"), lst(pki, "vendor.pub"), allow_revoked=[v.fingerprint], crypto=crypto)
    assert ok.status == "trusted" and any("exempted" in n for n in ok.notes)


def test_revoked_certificate_by_listing_by_issuer_and_by_crl(pki, crypto):
    h = signed(pki, crypto, cert="v.crt")
    trust = lst(pki, "ca.crt")
    assert modsign.verify(h, trust, lst(pki, "v.crt"), crypto=crypto).status == "revoked"
    assert modsign.verify(h, lst(pki, "vendor.pub"), lst(pki, "ca.crt"), crypto=crypto).status == "revoked"   # its CA is revoked
    by_crl = modsign.verify(h, trust, lst(pki, "ca.crl"), crypto=crypto)
    assert by_crl.status == "revoked" and "CRL" in by_crl.detail


def test_crl_not_signed_by_a_pinned_ca_is_ignored_with_a_note(pki, crypto):
    h = signed(pki, crypto, cert="v.crt")
    v = modsign.verify(h, lst(pki, "ca.crt"), lst(pki, "ca2.crl"), crypto=crypto)
    assert v.status == "trusted" and any("ignored" in n for n in v.notes)


def test_signing_is_deterministic(pki, crypto):
    assert signed(pki, crypto) == signed(pki, crypto)


@pytest.mark.skipif(len(BACKENDS) < 2, reason="needs both back ends")
def test_backends_make_the_same_signature_and_cross_verify(pki):
    a, b = (modsign.backend(n) for n in BACKENDS)
    ha = signed(pki, a, cert="v.crt")
    assert ha == signed(pki, b, cert="v.crt")
    assert modsign.verify(ha, lst(pki, "ca.crt"), lst(pki, "ca.crl"), crypto=b).status == modsign.verify(ha, lst(pki, "ca.crt"), lst(pki, "ca.crl"), crypto=a).status


def test_no_crypto_stops_with_the_install_message(monkeypatch):
    monkeypatch.setenv("KIF_CRYPTO", "nonsense")
    with pytest.raises(modsign.CryptoUnavailable, match="install"):
        modsign.backend()
    monkeypatch.setenv("KIF_CRYPTO", "openssl")
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(modsign.CryptoUnavailable, match="openssl"):
        modsign.backend()


# ---------------------------------------------------------------- the .kifm and the command line

VENDOR = "namespace ddk;\nfn id(x: u32) -> u32 { x }\n"


def kifm(pki, tmp_path, *extra, backend_name=None, env=None):
    from kiflang.__main__ import main

    src = tmp_path / "ddk.kif"
    src.write_text(VENDOR)
    out = tmp_path / "ddk.kifm"
    rc = main(["export", str(src), "-o", str(out), *extra])
    return rc, out


@pytest.fixture(params=BACKENDS)
def forced(request, monkeypatch):
    monkeypatch.setenv("KIF_CRYPTO", request.param)
    return request.param


def test_export_signs_and_verify_reports(pki, tmp_path, forced, capsys):
    from kiflang.__main__ import main

    rc, out = kifm(pki, tmp_path, "--sign", str(pki / "vendor.pem"), "--cert", str(pki / "v.crt"))
    assert rc == 0
    capsys.readouterr()
    assert main(["verify", str(out), "--trust", str(pki / "ca.crt")]) == 0
    text = capsys.readouterr().out
    assert "trusted: signed by CN=Acme Drivers" in text and "certificate valid" in text
    assert main(["verify", str(out), "--trust", str(pki / "other.pub")]) == 1
    assert "untrusted" in capsys.readouterr().out
    assert main(["verify", str(out), "--trust", str(pki / "ca.crt"), "--revoked", str(pki / "ca.crl")]) == 1
    assert "revoked" in capsys.readouterr().out


def test_signed_export_is_byte_identical_and_unsigned_is_reported(pki, tmp_path, forced, capsys):
    from kiflang.__main__ import main

    a = kifm(pki, tmp_path, "--sign", str(pki / "vendor.pem"))[1].read_bytes()
    assert kifm(pki, tmp_path, "--sign", str(pki / "vendor.pem"))[1].read_bytes() == a
    plain = kifm(pki, tmp_path)[1]
    assert main(["verify", str(plain)]) == 1 and "unsigned" in capsys.readouterr().out
    assert main(["inspect", str(plain)]) == 0 and "none (unsigned)" in capsys.readouterr().out


def test_export_refuses_bad_key_material(pki, tmp_path, forced, capsys):
    assert kifm(pki, tmp_path, "--sign", str(pki / "other.pem"), "--cert", str(pki / "v.crt"))[0] == 2
    assert kifm(pki, tmp_path, "--sign", str(pki / "rsa.pem"))[0] == 2
    assert kifm(pki, tmp_path, "--cert", str(pki / "v.crt"))[0] == 2
    assert kifm(pki, tmp_path, "--sign", str(tmp_path / "missing.pem"))[0] == 2
    assert not (tmp_path / "ddk.kifm").exists()


def test_a_flipped_byte_in_a_signed_kifm_is_bad_or_malformed(pki, tmp_path, forced, capsys):
    from kiflang import cbor
    from kiflang.__main__ import main

    out = kifm(pki, tmp_path, "--sign", str(pki / "vendor.pem"))[1]
    h = cbor.loads(out.read_bytes())
    h["deps"] = {"x.kif": "sha256:00"}
    out.write_bytes(cbor.dumps(h))
    assert main(["verify", str(out), "--trust", str(pki / "vendor.pub")]) == 1
    assert "does not verify" in capsys.readouterr().out
    h["signature"]["value"] = b"short"
    out.write_bytes(cbor.dumps(h))
    assert main(["verify", str(out)]) == 1
    assert "signature.value" in capsys.readouterr().err


def test_verify_without_crypto_exits_2(pki, tmp_path, monkeypatch, capsys):
    from kiflang.__main__ import main

    monkeypatch.setenv("KIF_CRYPTO", "cryptography")
    out = kifm(pki, tmp_path, "--sign", str(pki / "vendor.pem"))[1]
    monkeypatch.setenv("KIF_CRYPTO", "nonsense")
    assert main(["verify", str(out)]) == 2
    assert "install" in capsys.readouterr().err


# ---------------------------------------------------------------- the consumer's policy over the import tree

def tree(pki, tmp_path, sign_b=True, sign_c=False, key="vendor.pem"):
    """A sysroot holding b.kifm (imports c) and c.kifm, optionally signed, and no source of either."""
    from kiflang import modfile
    from kiflang.symbols import analyze
    from kiflang.sysroot import Sysroot

    root = tmp_path / "sysroot"
    root.mkdir()
    for n in ("reloco.kif", "microfmt.kif"):
        shutil.copy(__import__("os").path.join(__import__("os").path.dirname(__import__("kiflang").__file__), "prelude", n), root / n)
    (root / "c.kif").write_text("namespace c;\nfn two() -> u32 { 2 }\n")
    (root / "b.kif").write_text('namespace b;\nimport "c.kif";\nfn four() -> u32 { c::two() + 2 }\n')
    k = (pki / key).read_bytes()
    out = {}
    for name, signed in (("c", sign_c), ("b", sign_b)):
        mod = analyze(str(root / f"{name}.kif"), (root / f"{name}.kif").read_text(), sysroot=Sysroot(str(root)))
        assert not [d for d in mod.diagnostics if d.severity == 1], mod.diagnostics
        out[name] = modfile.export(mod, "c", None, (k, None) if signed else None)
    for name, data in out.items():   # written only now: source and `.kifm` side by side would be ambiguous
        (root / f"{name}.kifm").write_bytes(data)
    (root / "b.kif").unlink()
    (root / "c.kif").unlink()
    return root


def check(root, src, **policy):
    from kiflang.signpolicy import Policy
    from kiflang.symbols import analyze
    from kiflang.sysroot import Sysroot

    mod = analyze("/p/m.kif", src, sysroot=Sysroot(str(root), Policy(**policy)))
    return [(d.code, "error" if d.severity == 1 else "warning", d.message) for d in mod.diagnostics]


ROOT_B = 'import "b.kif";'


def test_unsigned_is_a_warning_an_error_under_require_and_accepted_when_off(pki, tmp_path, forced):
    root = tree(pki, tmp_path, sign_b=False)
    got = check(root, ROOT_B)
    assert [(c, s) for c, s, _ in got] == [("KIF019", "warning")] * 2          # b and, through it, c
    assert check(root, ROOT_B, mode="require")[0][:2] == ("KIF019", "error")
    assert check(root, ROOT_B, mode="off") == []


def test_signed_trusted_untrusted_and_the_tree(pki, tmp_path, forced):
    root = tree(pki, tmp_path)
    trust = (str(pki / "vendor.pub"),)
    got = check(root, ROOT_B, trust=trust)
    assert len(got) == 1 and got[0][:2] == ("KIF019", "warning")             # b is trusted; c is unsigned
    assert "'b.kif': 'c.kif'" in got[0][2] and "not signed" in got[0][2]      # the diagnostic names c and the import path through b
    other = check(root, ROOT_B, trust=(str(pki / "other.pub"),))
    assert any("sha256:" in m and "--trust" in m for _, _, m in other)
    assert check(root, ROOT_B, trust=(str(pki / "other.pub"),), mode="require")[0][1] == "error"


def test_tampered_signature_is_kif018_even_when_off_and_revoked_is_kif020(pki, tmp_path, forced):
    from kiflang import cbor

    root = tree(pki, tmp_path, sign_c=True)
    h = cbor.loads((root / "c.kifm").read_bytes())
    h["deps"] = {"x.kif": "sha256:00"}
    (root / "c.kifm").write_bytes(cbor.dumps(h))
    got = check(root, ROOT_B, mode="off", trust=(str(pki / "vendor.pub"),))
    assert [c for c, _, _ in got] == ["KIF018"] and got[0][1] == "error"
    (tmp_path / "r").mkdir()
    root2 = tree(pki, tmp_path / "r", sign_c=True)
    rev = check(root2, ROOT_B, trust=(str(pki / "vendor.pub"),), revoked=(str(pki / "vendor.pub"),))
    assert [c for c, s, _ in rev if s == "error"] == ["KIF020", "KIF020"]       # b and c are both signed by the revoked key


def test_a_key_inside_the_sysroot_is_not_a_trust_list(pki, tmp_path, forced):
    root = tree(pki, tmp_path)
    shutil.copy(pki / "vendor.pub", root / "vendor.pub")
    got = check(root, ROOT_B)
    assert any("not in the trust list" in m for _, _, m in got)


def test_idle_policy_looks_for_no_crypto(pki, tmp_path, monkeypatch):
    monkeypatch.setenv("KIF_CRYPTO", "cryptography")
    root = tree(pki, tmp_path)
    monkeypatch.setenv("KIF_CRYPTO", "nonsense")
    assert check(root, ROOT_B, mode="off") == []
    got = check(root, ROOT_B)
    assert any(c == "KIF021" and s == "error" and "install" in m for c, s, m in got)   # the unsigned c needs no crypto; the signed b does


def test_project_signing_section_and_its_validation(tmp_path):
    from kiflang import project

    (tmp_path / "kif.toml").write_text('[signing]\ntrust = ["keys/v.pem"]\nmode = "require"\nprover = "same"\nallow-revoked = ["sha256:ab"]\n')
    d = project.load(str(tmp_path / "kif.toml")).defaults("check", "signing")
    assert d["trust"] == [str(tmp_path / "keys" / "v.pem")] and d["signing"] == "require" and d["prover"] == "same" and d["allow_revoked"] == ["sha256:ab"]
    (tmp_path / "kif.toml").write_text('[signing]\nmode = "sometimes"\n')
    with pytest.raises(project.ProjectError, match="mode"):
        project.load(str(tmp_path / "kif.toml"))


def test_cli_check_applies_the_policy(pki, tmp_path, forced, capsys):
    from kiflang.__main__ import main

    root = tree(pki, tmp_path, sign_b=False)
    (tmp_path / "m.kif").write_text(ROOT_B)
    assert main(["check", str(tmp_path / "m.kif"), "--sysroot", str(root), "--no-project"]) == 0
    out = capsys.readouterr()
    assert "KIF019" in out.out + out.err
    assert main(["check", str(tmp_path / "m.kif"), "--sysroot", str(root), "--signing", "require"]) == 1
    assert main(["check", str(tmp_path / "m.kif"), "--sysroot", str(root), "--signing", "off"]) == 0


# ---------------------------------------------------------------- .kifa (docs/kif-module-contracts.md section 13.2, item 4)

SRC = "fn twice(x: u32) -> u32 { 2 * x }\nfn keep(x: u32) -> u32 { x }\n"


def _attest(tmp_path, pki, src=SRC, sign=True):
    from kiflang import attest
    from kiflang.symbols import analyze

    mod = analyze("a.kif", src)
    return attest.build(mod, "c", None, (read(pki, "vendor.pem"), None) if sign else None), mod


def _verify(args):
    from kiflang import __main__ as m

    return m.main(["verify", *args])


def test_kifa_is_deterministic_and_matches_the_ir_hash(pki, tmp_path, forced):
    from kiflang import attest, irhash, passes
    from kiflang.symbols import analyze

    a, mod = _attest(tmp_path, pki)
    b, _ = _attest(tmp_path, pki)
    assert a == b
    h = attest.decode(a)
    assert h["functions"] == dict(irhash.module_hashes(passes.run_module(mod.tree, []).module))
    both = attest.hashes(analyze("a.kif", "struct a { n: u32 = 0 }\nstruct b { n: u32 = 0 }\nimpl a { fn f(&self) -> u32 { 1 } }\nimpl b { fn f(&self) -> u32 { 2 } }\n"))
    assert set(both) == {"a::f", "b::f"} and both["a::f"] != both["b::f"]
    assert h["target"] == "c" and "signature" in h


def test_kifa_reports_match_changed_added_removed(pki, tmp_path, forced):
    from kiflang import attest
    from kiflang.symbols import analyze

    data, _ = _attest(tmp_path, pki)
    now = analyze("a.kif", "fn twice(x: u32) -> u32 { 3 * x }\nfn keep(x: u32) -> u32 { x }\nfn new(x: u32) -> u32 { x }\n")
    assert dict(attest.compare(attest.decode(data), now)) == {"twice": "changed", "keep": "match", "new": "added"}
    only = analyze("a.kif", "fn twice(x: u32) -> u32 { 2 * x }\n")
    assert dict(attest.compare(attest.decode(data), only)) == {"twice": "match", "keep": "removed"}


def test_kifa_verify_command_trust_and_tamper(pki, tmp_path, forced, capsys):
    data, _ = _attest(tmp_path, pki)
    (tmp_path / "a.kifa").write_bytes(data)
    (tmp_path / "a.kif").write_text(SRC)
    base = [str(tmp_path / "a.kifa")]
    assert _verify([*base, "--source", str(tmp_path / "a.kif"), "--trust", str(pki / "vendor.pub")]) == 0
    assert _verify([*base, "--trust", str(pki / "other.pub")]) == 1  # untrusted
    (tmp_path / "a.kif").write_text(SRC.replace("2 * x", "3 * x"))
    assert _verify([*base, "--source", str(tmp_path / "a.kif"), "--trust", str(pki / "vendor.pub")]) == 1
    assert "changed" in capsys.readouterr().out
    flipped = bytearray(data)
    flipped[-5] ^= 1
    (tmp_path / "bad.kifa").write_bytes(bytes(flipped))
    assert _verify([str(tmp_path / "bad.kifa"), "--trust", str(pki / "vendor.pub")]) == 1
    assert "bad:" in capsys.readouterr().out
    unsigned, _ = _attest(tmp_path, pki, sign=False)
    (tmp_path / "u.kifa").write_bytes(unsigned)
    assert _verify([str(tmp_path / "u.kifa")]) == 1
    assert "unsigned" in capsys.readouterr().out


def test_attest_section_emits_the_hash_into_the_c(tmp_path):
    import json

    from kiflang import kifc

    (tmp_path / "t.json").write_text(json.dumps({"extends": "c", "name": "att", "attest": {"section": "kif_attest"}}))
    (tmp_path / "a.kif").write_text(SRC)
    assert kifc.main(["--c", str(tmp_path / "o"), "--target", str(tmp_path / "t.json"), "--no-project", str(tmp_path / "a.kif")]) == 0
    c = (tmp_path / "o" / "a_proc.c").read_text()
    assert 'section("kif_attest")' in c and '" "' not in c
    assert " twice\";" in c and " keep\";" in c
