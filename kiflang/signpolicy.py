# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

"""The consumer's signing policy for the `.kifm` files a build loads (docs/kif-module-contracts.md, section 11.3).

The policy is the consumer's: it comes from the command line and `[signing]` in `kif.toml` (CMake passes the same options), never from
a target and never from the vendor, and it is applied to every `.kifm` the import tree loads. Libraries that analyze without a policy
(the language server, tests) check nothing; the command line tools always build one, `warn` by default.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import modsign

MODES = ("off", "warn", "require")
PROVERS = ("any", "same")


@dataclass
class Policy:
    mode: str = "warn"                 # what an unsigned or untrusted module is: off | warn | require
    trust: Tuple[str, ...] = ()        # PEM files of pinned public keys and certificates
    revoked: Tuple[str, ...] = ()      # PEM files of revoked keys, certificates and CRLs
    allow_revoked: Tuple[str, ...] = ()
    prover: str = "any"                # any | same: KIFI041 as a warning or as an error
    _lists: Optional[Tuple[Any, Any]] = field(default=None, repr=False)
    verdicts: Dict[str, modsign.Verdict] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise modsign.CryptoError(f"signing mode {self.mode!r} is not one of {', '.join(MODES)}")
        if self.prover not in PROVERS:
            raise modsign.CryptoError(f"prover {self.prover!r} is not one of {', '.join(PROVERS)}")

    @property
    def idle(self) -> bool:
        """Nothing to enforce or check: no crypto is looked for at all."""
        return self.mode == "off" and not self.trust and not self.revoked

    def evaluate(self, key: str, header: Dict[str, Any]) -> List[Tuple[str, str, str]]:
        """`(severity, code, message)` for the `.kifm` `header` (cached per `key`): KIF018 a signature that does not verify, KIF019 unsigned or
        untrusted, KIF020 revoked, KIF021 no crypto on this host to decide."""
        if self.idle:
            return []
        try:
            if self._lists is None:
                self._lists = (modsign.load_list(self.trust) if self.trust else None, modsign.load_list(self.revoked) if self.revoked else None)
            v = modsign.verify(header, self._lists[0], self._lists[1], self.allow_revoked)
        except modsign.CryptoUnavailable as exc:
            return [("error", "KIF021", f"cannot check the signature: {exc}")]
        except modsign.CryptoError as exc:
            return [("error", "KIF021", f"signing policy: {exc}")]
        self.verdicts[key] = v
        who = f"{v.signer} ({v.fingerprint})"
        hard = "error" if self.mode == "require" else "warning"
        if v.status == "bad":
            return [("error", "KIF018", f"its signature by {who} is not valid: {v.detail}" if v.signer else f"its signature is not valid: {v.detail}")]
        if v.status == "revoked":
            return [("error", "KIF020", f"it is signed by {who}, which is {v.detail}")]
        if v.status == "unsigned":
            return [] if self.mode == "off" else [(hard, "KIF019", "it is not signed (see --signing and docs/kif-module-contracts.md section 11.3)")]
        if v.status == "untrusted":
            return [] if self.mode == "off" else [(hard, "KIF019", f"it is signed by {who}, whose key is not in the trust list: to accept it, pin the key with --trust")]
        return []


def add_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trust", action="append", default=[], metavar="PEM", help="pinned public keys and certificates a signed .kifm must come from (repeatable)")
    p.add_argument("--revoked", action="append", default=[], metavar="PEM", help="revoked keys, certificates and CRLs (repeatable)")
    p.add_argument("--allow-revoked", action="append", default=[], metavar="FINGERPRINT", help="accept this revoked key or certificate anyway (printed in the audit)")
    p.add_argument("--signing", choices=MODES, default="warn", help="what an unsigned or untrusted .kifm is (default: warn)")
    p.add_argument("--prover", choices=PROVERS, default="any", help="`same`: a .kifm proved by another compiler is an error (KIFI041)")


def from_args(args: argparse.Namespace) -> Policy:
    return Policy(getattr(args, "signing", None) or "warn", tuple(getattr(args, "trust", None) or ()), tuple(getattr(args, "revoked", None) or ()),
                  tuple(getattr(args, "allow_revoked", None) or ()), getattr(args, "prover", None) or "any")
