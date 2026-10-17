<!--
SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF

SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
-->

# targets

Built-in target definitions (JSON, format `kif-target`) read by `kiflang/target.py`: `c` (plain hosted C), `cxx-user` (hosted C++20) and `cxx-kernel` (freestanding C++, derived from `cxx-user`; the base for
your own C++ kernel targets), `linux-kernel`
(GNU C, no kobj) and `freebsd-kernel` (the only one with a kobj emitter). The kernel targets `extends` `c`. Every key
is described in `docs/kif-targets.md`; `kif target list|show|check` inspects them.
