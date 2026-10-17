# SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

#
"""Target definitions (kiflang/target.py, docs/kif-targets.md)."""

import json
import os
import re

import pytest

from kiflang import kif, target

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return str(p)


def child(**over):
    return {"format": "kif-target", "version": 1, "name": "mine", "extends": "freebsd-kernel", **over}


def test_builtins_load_and_only_freebsd_has_kobj():
    assert target.builtin_names() == ["c", "cxx-kernel", "cxx-user", "freebsd-kernel", "linux-kernel"]
    got = {n: target.load(n).supports("kobj") for n in target.builtin_names()}
    assert got == {"c": False, "cxx-kernel": False, "cxx-user": False, "freebsd-kernel": True, "linux-kernel": False}
    assert {n: target.load(n).language for n in got} == {
        "c": "c", "cxx-kernel": "cxx", "cxx-user": "cxx", "freebsd-kernel": "c", "linux-kernel": "c"}


def test_alloc_section(tmp_path):
    t = target.load("linux-kernel")
    assert t.supports("alloc") and t.alloc("realloc", ptr="p", size="n") == "krealloc(p, n, GFP_KERNEL)"
    assert not target.load("cxx-user").supports("alloc")
    bad = tmp_path / "b.json"
    bad.write_text('{"format": "kif-target", "version": 1, "name": "b", "extends": "cxx-user", "alloc": {"malloc": "m({size})", "free": "f({ptr})"}}')
    assert any("reloco" in m for m in target.check_file(str(bad)))
    bad.write_text('{"format": "kif-target", "version": 1, "name": "b", "extends": "cxx-user", "panic": {"macro": "x"}}')
    assert any("reloco" in m for m in target.check_file(str(bad)))
    bad.write_text('{"format": "kif-target", "version": 1, "name": "b", "extends": "c", "alloc": {"malloc": "m({sz})"}}')
    msgs = target.check_file(str(bad))
    assert any("placeholder" in m for m in msgs)


def test_cxx_targets_can_be_derived(tmp_path):
    p = tmp_path / "my-cxx.json"
    p.write_text('{"format": "kif-target", "version": 1, "name": "my-cxx", "extends": "cxx-kernel",'
                 ' "cfg": {"arch": "arm64"}, "features": {"smp": true}}')
    t = target.load(str(p))
    assert t.language == "cxx" and t.get("hosted") is False and t.get("panic") is None
    env = t.cfg_env()
    assert "cxx" in env.flags and env.values["arch"] == {"arm64"} and "smp" in env.flags


def test_kobj_header_name_and_quoting_come_from_the_target(tmp_path):
    assert target.load("freebsd-kernel").kobj_include() == "<sys/kobj.h>"
    t = target.load(write(tmp_path, "t.json", child(kobj={"header": "bsd/kobj.h", "quote": "quote", "include_dir": "inc"})))
    assert t.kobj_include() == '"bsd/kobj.h"'
    assert t.include_dirs() == [str(tmp_path / "inc")]
    assert t.includes("header")[-2:] == ['"bsd/kobj.h"', "<sys/bus.h>"]  # the glue headers follow the kobj header


def test_kobj_can_be_removed_by_a_child(tmp_path):
    t = target.load(write(tmp_path, "t.json", child(kobj=None)))
    assert not t.supports("kobj") and t.kobj_include() is None


def test_custom_includes_and_feature_flags(tmp_path):
    t = target.load(write(tmp_path, "t.json", child(
        features={"has_pmap": True, "smp": False, "tag": "x"},
        includes={"header": ["<mine.h>"], "when": {"has_pmap": {"header": ["<vm/pmap.h>"]}, "smp": {"header": ["<smp.h>"]}}})))
    assert t.includes("header")[:2] == ["<mine.h>", "<vm/pmap.h>"] and "<smp.h>" not in t.includes("header")
    assert t.flag("has_pmap") is True and t.flag("tag") == "x" and t.flag("missing") is False


def test_unknown_key_wrong_type_and_missing_header_are_reported(tmp_path):
    p = write(tmp_path, "t.json", child(typo=1, hosted="yes", errno={"style": "sideways"}))
    problems = target.check_file(p)
    assert any(m.startswith("typo: unknown key") for m in problems)
    assert any(m.startswith("hosted:") for m in problems) and any(m.startswith("errno.style:") for m in problems)
    with pytest.raises(target.TargetError, match="kobj.header: required"):
        target.load(write(tmp_path, "u.json", {"format": "kif-target", "version": 1, "name": "x", "language": "c", "kobj": {"flavor": "freebsd"}}))
    with pytest.raises(target.TargetError, match="includes.when.nope"):
        target.load(write(tmp_path, "v.json", child(includes={"when": {"nope": {"header": []}}})))


def test_only_the_freebsd_flavor_exists():
    assert target.KOBJ_FLAVORS == ("freebsd",)


def test_extends_loop_and_unknown_base(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(child(extends="b.json", name="a")))
    b.write_text(json.dumps(child(extends="a.json", name="b")))
    with pytest.raises(target.TargetError, match="loop"):
        target.load(str(a))
    with pytest.raises(target.TargetError, match="unknown target"):
        target.load(write(tmp_path, "c.json", child(extends="nosuch")))


def test_macros_and_types():
    t = target.load("linux-kernel")
    assert "BUG()" in t.macro("panic", file="f", line="1", msg="m") and "{" in t.macro("panic", file="f", line="1", msg="m")
    assert t.c_type("i32") == "s32" and target.load("c").c_type("i32") == "int32_t"
    assert target.validate({"format": "kif-target", "version": 1, "name": "x", "language": "c", "panic": {"macro": "{nope}"}})


def test_invalid_json_names_the_line(tmp_path):
    with pytest.raises(target.TargetError, match=r":1:\d+: invalid JSON"):
        target.load(write(tmp_path, "bad.json", "{"))


def test_cli(tmp_path, capsys):
    assert kif.main(["target", "list"]) == 0 and "freebsd-kernel" in capsys.readouterr().out
    assert kif.main(["target", "show", "c"]) == 0 and json.loads(capsys.readouterr().out)["name"] == "c"
    assert kif.main(["target", "check", "c", "linux-kernel", "freebsd-kernel"]) == 0
    assert kif.main(["target", "check", write(tmp_path, "t.json", child(typo=1))]) == 1
    assert kif.main(["target", "check", "nosuch"]) == 1


def test_every_key_is_documented():
    doc = open(os.path.join(ROOT, "docs", "kif-targets.md"), encoding="utf-8").read()
    keys = []

    def walk(spec, path):
        for k, v in spec.items():
            if k == "*":
                continue
            here = f"{path}.{k}" if path else k
            if isinstance(v, dict):
                keys.append(here)
                walk(v, here)
            else:
                keys.append(here)

    walk(target._SPEC, "")
    missing = [k for k in keys if not re.search(r"\| `" + re.escape(k) + r"` \|", doc)]
    assert not missing, missing


def test_panic_must_be_noreturn():
    base = {"format": "kif-target", "version": 1, "name": "x", "language": "c"}
    assert any("noreturn" in e for e in target.validate({**base, "panic": {"macro": "log()"}}))
    assert not any("noreturn" in e for e in target.validate({**base, "panic": {"macro": "trap()", "noreturn": True}}))
