# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

cmake_minimum_required(VERSION 3.20)
project(jplcz_kif VERSION 0.1.0 LANGUAGES CXX)

if(CMAKE_SOURCE_DIR STREQUAL PROJECT_SOURCE_DIR)
    set(JPLCZ_KIF_IS_TOP_LEVEL ON)
else()
    set(JPLCZ_KIF_IS_TOP_LEVEL OFF)
endif()

option(JPLCZ_KIF_ENABLE_STRICT_WARNINGS
    "Enable comprehensive GCC and Clang warnings" ${JPLCZ_KIF_IS_TOP_LEVEL})
option(JPLCZ_KIF_ENABLE_WERROR
    "Enable build with -Werror" ${JPLCZ_KIF_IS_TOP_LEVEL})
option(JPLCZ_KIF_BUILD_TESTS
    "Register the Python test suite with ctest" ${JPLCZ_KIF_IS_TOP_LEVEL})
option(JPLCZ_KIF_BUILD_HEADER_CHECKS
    "Compile the public runtime headers under each supported C++ standard" ${JPLCZ_KIF_IS_TOP_LEVEL})
option(JPLCZ_KIF_CHECK_NO_STD_TYPES
    "Hard build-time check that the runtime headers avoid allocating/throwing/glibc-dependent std:: \
facilities (see scripts/check-no-std-types.py)" ${JPLCZ_KIF_IS_TOP_LEVEL})
option(JPLCZ_KIF_INSTALL
    "Install the runtime headers, the kifc compiler and the CMake package" ${JPLCZ_KIF_IS_TOP_LEVEL})
option(JPLCZ_KIF_INSTALL_HEADERS_ONLY
    "With JPLCZ_KIF_INSTALL, install only the runtime headers (no CMake package, docs or compiler). Needed when \
jplcz_kif is pulled in by a larger build that does its own consolidated install(EXPORT); it also forces the fetched \
jplcz_reloco and jplcz_microfmt dependencies to match." OFF)

option(JPLCZ_KIF_USE_SYSTEM_DEPS
    "Find jplcz_reloco and jplcz_microfmt with find_package() (installed, Conan or vcpkg) instead of fetching them" OFF)

include(GNUInstallDirs)
set(JPLCZ_KIF_INSTALL_INCLUDEDIR "${CMAKE_INSTALL_INCLUDEDIR}" CACHE STRING
    "Install destination for kif's runtime headers (relative to CMAKE_INSTALL_PREFIX unless absolute)")
set(JPLCZ_KIF_INSTALL_DOCDIR "${CMAKE_INSTALL_DOCDIR}" CACHE STRING
    "Install destination for kif's README/LICENSE/docs/ (relative to CMAKE_INSTALL_PREFIX unless absolute)")
set(JPLCZ_KIF_INSTALL_CMAKEDIR "${CMAKE_INSTALL_DATADIR}/cmake/jplcz_kif" CACHE STRING
    "Install destination for the jplcz_kif CMake package, Kif.cmake and the kiflang Python package")

if(JPLCZ_KIF_USE_SYSTEM_DEPS)
    if(NOT TARGET jplcz_reloco::reloco)
        find_package(jplcz_reloco CONFIG REQUIRED)
    endif()
    if(NOT TARGET jplcz_microfmt::microfmt)
        find_package(jplcz_microfmt CONFIG REQUIRED)
    endif()
endif()

# jplcz_reloco dependency: the kobj runtime is built on reloco's containers, allocators, results and lifetime
# annotations. Point JPLCZ_KIF_RELOCO_SOURCE_DIR at a local checkout (or set it in the environment); otherwise
# reloco is fetched from GitHub. If a parent project already provides jplcz_reloco::reloco, that target is reused.
if(NOT TARGET jplcz_reloco::reloco)
    set(JPLCZ_KIF_RELOCO_SOURCE_DIR "" CACHE PATH
        "Path to a local jplcz_reloco checkout to use instead of fetching it")
    if(NOT JPLCZ_KIF_RELOCO_SOURCE_DIR AND DEFINED ENV{JPLCZ_KIF_RELOCO_SOURCE_DIR})
        set(JPLCZ_KIF_RELOCO_SOURCE_DIR "$ENV{JPLCZ_KIF_RELOCO_SOURCE_DIR}" CACHE PATH
            "Path to a local jplcz_reloco checkout to use instead of fetching it" FORCE)
    endif()
    set(JPLCZ_KIF_RELOCO_GIT_REPOSITORY "https://github.com/jplcz/reloco.git" CACHE STRING
        "Git repository to fetch jplcz_reloco from when JPLCZ_KIF_RELOCO_SOURCE_DIR is unset")
    set(JPLCZ_KIF_RELOCO_GIT_TAG "master" CACHE STRING
        "Git tag or commit to fetch jplcz_reloco from when JPLCZ_KIF_RELOCO_SOURCE_DIR is unset")

    include(FetchContent)
    if(JPLCZ_KIF_RELOCO_SOURCE_DIR)
        FetchContent_Declare(jplcz_reloco SOURCE_DIR "${JPLCZ_KIF_RELOCO_SOURCE_DIR}")
    else()
        FetchContent_Declare(jplcz_reloco
            GIT_REPOSITORY "${JPLCZ_KIF_RELOCO_GIT_REPOSITORY}"
            GIT_TAG "${JPLCZ_KIF_RELOCO_GIT_TAG}")
    endif()
    # Consumed as a header-only build dependency: keep its development targets out of this build.
    set(JPLCZ_RELOCO_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_RELOCO_BUILD_DEMOS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_RELOCO_BUILD_HEADER_CHECKS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_RELOCO_ENABLE_STRICT_WARNINGS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_RELOCO_INSTALL ${JPLCZ_KIF_INSTALL} CACHE BOOL "" FORCE)
    set(JPLCZ_RELOCO_INSTALL_HEADERS_ONLY ${JPLCZ_KIF_INSTALL_HEADERS_ONLY} CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(jplcz_reloco)
endif()

# jplcz_microfmt dependency: call tracing (include/kif/kobj/trace.hpp) formats its records and writes them to a
# microfmt logger. Same sourcing rules as reloco: JPLCZ_KIF_MICROFMT_SOURCE_DIR (or the environment variable of the
# same name) for a local checkout, otherwise fetched from GitHub; a parent's jplcz_microfmt::microfmt is reused.
if(NOT TARGET jplcz_microfmt::microfmt)
    set(JPLCZ_KIF_MICROFMT_SOURCE_DIR "" CACHE PATH
        "Path to a local jplcz_microfmt checkout to use instead of fetching it")
    if(NOT JPLCZ_KIF_MICROFMT_SOURCE_DIR AND DEFINED ENV{JPLCZ_KIF_MICROFMT_SOURCE_DIR})
        set(JPLCZ_KIF_MICROFMT_SOURCE_DIR "$ENV{JPLCZ_KIF_MICROFMT_SOURCE_DIR}" CACHE PATH
            "Path to a local jplcz_microfmt checkout to use instead of fetching it" FORCE)
    endif()
    set(JPLCZ_KIF_MICROFMT_GIT_REPOSITORY "https://github.com/jplcz/microfmt.git" CACHE STRING
        "Git repository to fetch jplcz_microfmt from when JPLCZ_KIF_MICROFMT_SOURCE_DIR is unset")
    set(JPLCZ_KIF_MICROFMT_GIT_TAG "master" CACHE STRING
        "Git tag or commit to fetch jplcz_microfmt from when JPLCZ_KIF_MICROFMT_SOURCE_DIR is unset")

    include(FetchContent)
    if(JPLCZ_KIF_MICROFMT_SOURCE_DIR)
        FetchContent_Declare(jplcz_microfmt SOURCE_DIR "${JPLCZ_KIF_MICROFMT_SOURCE_DIR}")
    else()
        FetchContent_Declare(jplcz_microfmt
            GIT_REPOSITORY "${JPLCZ_KIF_MICROFMT_GIT_REPOSITORY}"
            GIT_TAG "${JPLCZ_KIF_MICROFMT_GIT_TAG}")
    endif()
    # Consumed as a header-only build dependency: keep its development targets out of this build.
    set(JPLCZ_MICROFMT_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_MICROFMT_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
    set(JPLCZ_MICROFMT_BUILD_BENCHMARKS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_MICROFMT_BUILD_HEADER_CHECKS OFF CACHE BOOL "" FORCE)
    set(JPLCZ_MICROFMT_CHECK_NO_STD_TYPES OFF CACHE BOOL "" FORCE)
    set(JPLCZ_MICROFMT_INSTALL ${JPLCZ_KIF_INSTALL} CACHE BOOL "" FORCE)
    set(JPLCZ_MICROFMT_INSTALL_HEADERS_ONLY ${JPLCZ_KIF_INSTALL_HEADERS_ONLY} CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(jplcz_microfmt)
endif()

# The kifc compiler's CMake functions (add_kif_library, kif_target_sources, kif_install_headers).
list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")
include(Kif)

# Header-only runtime (C++17).
add_library(jplcz_kif INTERFACE)
target_compile_features(jplcz_kif INTERFACE cxx_std_17)
set_target_properties(jplcz_kif PROPERTIES EXPORT_NAME kif)
target_include_directories(jplcz_kif INTERFACE
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${JPLCZ_KIF_INSTALL_INCLUDEDIR}>)
target_link_libraries(jplcz_kif INTERFACE jplcz_reloco::reloco jplcz_microfmt::microfmt)
add_library(jplcz_kif::kif ALIAS jplcz_kif)

add_library(jplcz_kif_warnings INTERFACE)
if(JPLCZ_KIF_ENABLE_STRICT_WARNINGS)
    if(MSVC)
        target_compile_options(jplcz_kif_warnings INTERFACE /W4)
    else()
        target_compile_options(jplcz_kif_warnings INTERFACE -Wall -Wextra -Wpedantic -Wshadow -Wconversion)
    endif()
endif()
if(JPLCZ_KIF_ENABLE_WERROR)
    target_compile_options(jplcz_kif_warnings INTERFACE $<IF:$<BOOL:${MSVC}>,/WX,-Werror>)
endif()

if(JPLCZ_KIF_BUILD_TESTS)
    enable_testing()
    # Front end, tools and language server tests (need pytest).
    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_FOUND)
        add_test(NAME Kif.python_tests
            COMMAND "${Python3_EXECUTABLE}" -m pytest tests -q
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    endif()
endif()

if(JPLCZ_KIF_BUILD_HEADER_CHECKS)
    # One translation unit per C++ standard that includes every public runtime header (sorted).
    file(GLOB JPLCZ_KIF_PUBLIC_HEADERS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/include/kif/kobj/*.hpp"
         "${CMAKE_CURRENT_SOURCE_DIR}/include/kif/*.hpp")
    # trace.hpp exists only with -DKIF_KOBJ_TRACE and needs C++20 (microfmt's logger): checked separately below.
    list(REMOVE_ITEM JPLCZ_KIF_PUBLIC_HEADERS "${CMAKE_CURRENT_SOURCE_DIR}/include/kif/kobj/trace.hpp")
    list(SORT JPLCZ_KIF_PUBLIC_HEADERS)

    include(CheckCXXCompilerFlag)
    if(MSVC)
        check_cxx_compiler_flag("/std:c++latest" JPLCZ_KIF_COMPILER_SUPPORTS_CXX23)
    else()
        check_cxx_compiler_flag("-std=c++23" JPLCZ_KIF_COMPILER_SUPPORTS_CXX23)
    endif()

    function(add_jplcz_kif_header_check std)
        set(check_dir "${CMAKE_CURRENT_BINARY_DIR}/header_check_cxx${std}")
        file(MAKE_DIRECTORY "${check_dir}")
        set(content "")
        foreach(header ${JPLCZ_KIF_PUBLIC_HEADERS})
            file(RELATIVE_PATH rel_header "${CMAKE_CURRENT_SOURCE_DIR}/include/kif" "${header}")
            string(APPEND content "#include <kif/${rel_header}>\n")
        endforeach()
        if(std GREATER_EQUAL 20)
            string(APPEND content "#include <kif/kobj/trace.hpp>\n")
        endif()
        file(WRITE "${check_dir}/kobj.cpp" "${content}")
        add_library(jplcz_kif_headers_cxx${std} OBJECT "${check_dir}/kobj.cpp")
        if(std GREATER_EQUAL 20)
            target_compile_definitions(jplcz_kif_headers_cxx${std} PRIVATE KIF_KOBJ_TRACE)
        endif()
        target_link_libraries(jplcz_kif_headers_cxx${std} PRIVATE jplcz_kif jplcz_kif_warnings)
        target_compile_features(jplcz_kif_headers_cxx${std} PRIVATE cxx_std_${std})
    endfunction()

    set(JPLCZ_KIF_HEADER_CHECK_TARGETS jplcz_kif_headers_cxx17 jplcz_kif_headers_cxx20)
    add_jplcz_kif_header_check(17)
    add_jplcz_kif_header_check(20)
    if(JPLCZ_KIF_COMPILER_SUPPORTS_CXX23)
        add_jplcz_kif_header_check(23)
        list(APPEND JPLCZ_KIF_HEADER_CHECK_TARGETS jplcz_kif_headers_cxx23)
    endif()
    add_custom_target(jplcz_kif_check_public_headers ALL DEPENDS ${JPLCZ_KIF_HEADER_CHECK_TARGETS})
endif()

if(JPLCZ_KIF_CHECK_NO_STD_TYPES)
    find_package(Python3 COMPONENTS Interpreter REQUIRED)
    file(GLOB_RECURSE JPLCZ_KIF_ALL_HEADERS CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/include/*.hpp" "${CMAKE_CURRENT_SOURCE_DIR}/include/*.ipp")
    add_custom_command(
        OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kif_check_no_std_types.stamp"
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check-no-std-types.py"
            --include-dir "${CMAKE_CURRENT_SOURCE_DIR}/include"
        COMMAND "${CMAKE_COMMAND}" -E touch "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kif_check_no_std_types.stamp"
        DEPENDS ${JPLCZ_KIF_ALL_HEADERS} "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check-no-std-types.py"
        COMMENT "Checking include/ for disallowed std:: usage (scripts/check-no-std-types.py)"
        VERBATIM)
    add_custom_target(jplcz_kif_check_no_std_types ALL
        DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kif_check_no_std_types.stamp")
endif()

if(JPLCZ_KIF_INSTALL)
    include(CMakePackageConfigHelpers)

    install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/include/"
        DESTINATION "${JPLCZ_KIF_INSTALL_INCLUDEDIR}"
        COMPONENT Development)

    if(NOT JPLCZ_KIF_INSTALL_HEADERS_ONLY)
        install(TARGETS jplcz_kif
            EXPORT jplcz_kifTargets
            INCLUDES DESTINATION "${JPLCZ_KIF_INSTALL_INCLUDEDIR}")

        install(FILES "${CMAKE_CURRENT_SOURCE_DIR}/README.md" "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE"
            DESTINATION "${JPLCZ_KIF_INSTALL_DOCDIR}"
            COMPONENT Documentation)
        install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/docs/"
            DESTINATION "${JPLCZ_KIF_INSTALL_DOCDIR}/docs"
            COMPONENT Documentation)

        configure_package_config_file(
            "${CMAKE_CURRENT_SOURCE_DIR}/cmake/jplcz_kifConfig.cmake.in"
            "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kifConfig.cmake"
            INSTALL_DESTINATION "${JPLCZ_KIF_INSTALL_CMAKEDIR}"
            NO_SET_AND_CHECK_MACRO
            NO_CHECK_REQUIRED_COMPONENTS_MACRO)
        write_basic_package_version_file(
            "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kifConfigVersion.cmake"
            VERSION "${PROJECT_VERSION}"
            COMPATIBILITY SameMajorVersion
            ARCH_INDEPENDENT)
        install(EXPORT jplcz_kifTargets
            FILE jplcz_kifTargets.cmake
            NAMESPACE jplcz_kif::
            DESTINATION "${JPLCZ_KIF_INSTALL_CMAKEDIR}"
            COMPONENT Development)
        install(FILES
                "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kifConfig.cmake"
                "${CMAKE_CURRENT_BINARY_DIR}/jplcz_kifConfigVersion.cmake"
                cmake/Kif.cmake
            DESTINATION "${JPLCZ_KIF_INSTALL_CMAKEDIR}"
            COMPONENT Development)
        # The kifc compiler Python package (standard library only); Kif.cmake finds it in <cmake dir>/python.
        install(DIRECTORY kiflang
            DESTINATION "${JPLCZ_KIF_INSTALL_CMAKEDIR}/python"
            COMPONENT Development
            FILES_MATCHING PATTERN "*.py" PATTERN "*.kif" PATTERN "*.json")  # prelude (default sysroot) and built-in targets

        # Command-line launchers (kifc, kif, kif-check, kif-mimport, kif-lsp) that run the installed Python package.
        file(RELATIVE_PATH KIF_PY_RELDIR "/${CMAKE_INSTALL_BINDIR}" "/${JPLCZ_KIF_INSTALL_CMAKEDIR}/python")
        foreach(tool IN ITEMS kifc:kiflang.kifc kif:kiflang.kif kif-check:kiflang kif-mimport:kiflang.mimport
                              kif-lsp:kiflang.lsp)
            string(REPLACE ":" ";" parts "${tool}")
            list(GET parts 0 KIF_TOOL_NAME)
            list(GET parts 1 KIF_TOOL_MODULE)
            configure_file(cmake/kif-tool.sh.in "${CMAKE_CURRENT_BINARY_DIR}/launchers/${KIF_TOOL_NAME}" @ONLY)
            install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/launchers/${KIF_TOOL_NAME}"
                DESTINATION "${CMAKE_INSTALL_BINDIR}"
                COMPONENT Tools)
        endforeach()
    endif()

    if(PROJECT_IS_TOP_LEVEL AND NOT JPLCZ_KIF_INSTALL_HEADERS_ONLY)
        # Packages: `cpack --config build/CPackConfig.cmake -G TGZ|DEB|RPM` (see docs/kif-packaging.md).
        set(CPACK_PACKAGE_NAME "jplcz-kif")
        set(CPACK_PACKAGE_VENDOR "Jarosław Pelczar")
        set(CPACK_PACKAGE_CONTACT "Jarosław Pelczar <jarek@jpelczar.com>")
        set(CPACK_PACKAGE_DESCRIPTION_SUMMARY "kif: interface definition language and generators for kobj interfaces")
        set(CPACK_PACKAGE_VERSION "${PROJECT_VERSION}")
        set(CPACK_RESOURCE_FILE_LICENSE "${CMAKE_CURRENT_SOURCE_DIR}/LICENSE")
        set(CPACK_GENERATOR "TGZ")
        set(CPACK_SOURCE_GENERATOR "TGZ")
        set(CPACK_SOURCE_IGNORE_FILES "/build.*/;/\\.git/;/__pycache__/;\\.pyc$;/\\.venv/;\\.egg-info")
        set(CPACK_DEBIAN_PACKAGE_DEPENDS "python3 (>= 3.8)")
        set(CPACK_DEBIAN_PACKAGE_SECTION "devel")
        set(CPACK_DEBIAN_FILE_NAME DEB-DEFAULT)
        set(CPACK_RPM_PACKAGE_REQUIRES "python3 >= 3.8")
        set(CPACK_RPM_PACKAGE_LICENSE "BSD-2-Clause")
        set(CPACK_RPM_FILE_NAME RPM-DEFAULT)
        set(CPACK_ARCHIVE_COMPONENT_INSTALL OFF)
        set(CPACK_PACKAGE_ARCHITECTURE "all")
        set(CPACK_DEBIAN_PACKAGE_ARCHITECTURE "all")
        set(CPACK_RPM_PACKAGE_ARCHITECTURE "noarch")
        include(CPack)
    endif()
endif()
