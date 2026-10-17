# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: BSD-2-Clause

from conan import ConanFile
from conan.tools.cmake import CMake, CMakeDeps, CMakeToolchain
from conan.tools.files import copy


class JplczKifConan(ConanFile):
    name = "jplcz_kif"
    version = "0.1.0"
    package_type = "header-library"
    settings = "os", "arch", "compiler", "build_type"
    license = "BSD-2-Clause"
    url = "https://github.com/jplcz/kif"
    homepage = "https://github.com/jplcz/kif"
    description = (
        "Interface definition language and generators for kobj interfaces: header-only C++ runtime, "
        "kifc compiler and CMake functions"
    )
    topics = ("kobj", "idl", "embedded", "freebsd", "header-only")

    exports_sources = (
        "CMakeLists.txt", "cmake/**", "include/**", "kiflang/**", "docs/**", "README.md", "LICENSE",
    )

    def requirements(self):
        # Public headers include reloco's and microfmt's, so consumers need them on their include path too.
        self.requires("jplcz_reloco/[>=0.4.1]", transitive_headers=True)
        self.requires("jplcz_microfmt/[>=0.3.0]", transitive_headers=True)

    def generate(self):
        CMakeDeps(self).generate()
        tc = CMakeToolchain(self)
        tc.cache_variables["JPLCZ_KIF_USE_SYSTEM_DEPS"] = True
        tc.cache_variables["JPLCZ_KIF_BUILD_TESTS"] = False
        tc.cache_variables["JPLCZ_KIF_BUILD_HEADER_CHECKS"] = False
        tc.cache_variables["JPLCZ_KIF_CHECK_NO_STD_TYPES"] = False
        tc.cache_variables["JPLCZ_KIF_ENABLE_STRICT_WARNINGS"] = False
        tc.cache_variables["JPLCZ_KIF_ENABLE_WERROR"] = False
        tc.cache_variables["JPLCZ_KIF_INSTALL"] = True
        tc.generate()

    def build(self):
        cmake = CMake(self)
        cmake.configure()

    def package(self):
        cmake = CMake(self)
        cmake.install()
        copy(self, "LICENSE", src=self.source_folder, dst="licenses")

    def package_id(self):
        self.info.clear()

    def package_info(self):
        self.cpp_info.bindirs = ["bin"]
        self.cpp_info.libdirs = []
        # The package ships its own config (jplcz_kifConfig.cmake: target + add_kif_library()); Conan only has to
        # put it on CMAKE_PREFIX_PATH.
        self.cpp_info.set_property("cmake_find_mode", "none")
        self.cpp_info.builddirs = ["share/cmake/jplcz_kif"]
