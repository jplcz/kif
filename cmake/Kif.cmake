# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
#
# CMake integration for the kif compiler (python3 -m kiflang.kifc). See docs/kif-cmake.md.
# Incremental: one kifc run per .kif and backend; imports are tracked with a per-file depfile.
#
#   include(Kif)
#   add_kif_library(<name> SOURCES a.kif b_if.m ... [BACKENDS cpp|freebsd|c ...] [INCLUDE_DIRS dir...]
#                   [KIFC_ARGS --target T --profile P ...] [OUTPUT_DIR dir] [LINK_LIBRARIES lib...] [CHECK_WITH target...] [WERROR])
#   add_kif_executable(<name> SOURCES main.kif ...)     # add_kif_library(... TYPE EXECUTABLE): the sources define `fn main`
#   (add_kif_library VISIBILITY default|hidden|protected|internal sets -fvisibility; see `#[visibility]` in docs/kif-proc-language.md)
#   (add_kif_library TYPE: STATIC | SHARED | OBJECT | EXECUTABLE; default: BUILD_SHARED_LIBS decides)
#   kif_target_sources(<target> SOURCES a.kif ... [BACKEND cpp|freebsd|c] [INCLUDE_DIRS dir...] [OUTPUT_DIR dir])
#   kif_install_headers(<target> DESTINATION <sdk-include-dir> [COMPONENT c])
#   kif_use_project([FILE kif.toml])      # read the project file (docs/kif-project.md) once, at configure time
#
# Per-target settings (win over the build-wide ones below): keywords of add_kif_library()/kif_target_sources(), and for an existing
# target the properties of the same name, read by kif_target_sources():
#   KIF_TARGET KIF_PROFILE KIF_SYSROOT KIF_SANITIZE KIF_CFG KIF_INCLUDE_DIRECTORIES KIF_WERROR
#   KIF_SMALL_STACK   (the default stack budget in bytes, or off; --small-stack, docs/kif-stack.md)
#   KIF_TRUST KIF_REVOKED KIF_SIGNING   (the signing policy for precompiled .kifm imports, docs/kif-module-contracts.md section 11.3)
#   set_target_properties(mod PROPERTIES KIF_TARGET freebsd-kernel KIF_CFG "smp")  then  kif_target_sources(mod SOURCES x.kif BACKEND c)
#
# Build-wide defaults for every `kifc` run (an explicit KIFC_ARGS wins; -D on the command line wins over kif.toml):
#   KIF_TARGET  KIF_PROFILE  KIF_SANITIZE  KIF_CFG  KIF_INCLUDE_DIRS  KIF_SYSROOT  KIF_WERROR
#   KIF_SMALL_STACK (bytes or off)  KIF_TRUST  KIF_REVOKED (PEM files, relative to the calling directory)  KIF_SIGNING (off|warn|require)  KIF_PROVER (any|same)
# The trust and revocation files are dependencies of every generated file, so changing one re-runs the checks.
# KIF_PROFILE defaults to a mapping of CMAKE_BUILD_TYPE: Debug -> debug, RelWithDebInfo -> perf, Release/MinSizeRel -> release.
# `kifc` is always run with --no-project: the build is described by CMake, never by whichever kif.toml is nearest to the build tree.

include_guard(GLOBAL)

find_package(Python3 REQUIRED COMPONENTS Interpreter)
set_property(GLOBAL PROPERTY KIF_PYTHON_EXECUTABLE "${Python3_EXECUTABLE}")

# Directory holding the kiflang package. Override to use another checkout.
# Installed layout: kiflang sits in <Kif.cmake dir>/python; source tree: <repo root>.
if(NOT KIF_ROOT)
    if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/python/kiflang")
        set(KIF_ROOT "${CMAKE_CURRENT_LIST_DIR}/python")
    else()
        get_filename_component(KIF_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
    endif()
endif()
set_property(GLOBAL PROPERTY KIF_ROOT_DIR "${KIF_ROOT}")

# The kifc options every backend run shares: --no-project (CMake owns the configuration), then the KIF_* defaults. Sets <out>.
# Precedence, highest first: KIF_* (cache or -D), the project file (KIF_PROJECT_*), the CMAKE_BUILD_TYPE profile mapping.
function(_kif_base_args out)
    set(args --no-project)
    foreach(key IN ITEMS TARGET PROFILE SYSROOT)
        if(DEFINED KIF_${key})
            set(value "${KIF_${key}}")
        else()
            set(value "${KIF_PROJECT_${key}}")
        endif()
        if(key STREQUAL "PROFILE" AND value STREQUAL "")
            if(CMAKE_BUILD_TYPE STREQUAL "Debug")
                set(value debug)
            elseif(CMAKE_BUILD_TYPE STREQUAL "RelWithDebInfo")
                set(value perf)
            elseif(CMAKE_BUILD_TYPE MATCHES "^(Release|MinSizeRel)$")
                set(value release)
            endif()
        endif()
        string(TOLOWER "${key}" opt)
        string(REPLACE "_" "-" opt "${opt}")
        if(NOT value STREQUAL "")
            list(APPEND args --${opt} "${value}")
        endif()
    endforeach()
    foreach(key IN ITEMS SANITIZE CFG)
        set(values ${KIF_PROJECT_${key}} ${KIF_${key}})
        string(TOLOWER "${key}" opt)
        foreach(v IN LISTS values)
            list(APPEND args --${opt} "${v}")
        endforeach()
    endforeach()
    foreach(key IN ITEMS TRUST REVOKED)
        if(DEFINED KIF_${key})
            set(values ${KIF_${key}})
        else()
            set(values ${KIF_PROJECT_${key}})
        endif()
        string(TOLOWER "${key}" opt)
        foreach(v IN LISTS values)
            get_filename_component(v "${v}" ABSOLUTE)
            list(APPEND args --${opt} "${v}")
        endforeach()
    endforeach()
    foreach(key IN ITEMS SIGNING PROVER SMALL_STACK)
        if(DEFINED KIF_${key})
            set(value "${KIF_${key}}")
        else()
            set(value "${KIF_PROJECT_${key}}")
        endif()
        string(TOLOWER "${key}" opt)
        string(REPLACE "_" "-" opt "${opt}")
        if(NOT value STREQUAL "")
            list(APPEND args --${opt} "${value}")
        endif()
    endforeach()
    foreach(dir IN LISTS KIF_PROJECT_INCLUDE KIF_INCLUDE_DIRS)
        list(APPEND args -I "${dir}")
    endforeach()
    if(KIF_WERROR OR (NOT DEFINED KIF_WERROR AND KIF_PROJECT_WERROR))
        list(APPEND args --Werror)
    endif()
    set(${out} ${args} PARENT_SCOPE)
endfunction()

# Reads kif.toml (default: the one next to the calling CMakeLists.txt, else the nearest above) with `kif project --cmake` and
# turns its keys into KIF_PROJECT_* variables, which the functions below use as defaults. Re-run on change of the file.
# Sets KIF_PROJECT_SOURCES to the `[build] sources` files (globs expanded), so `add_kif_library(<name>)` needs no SOURCES.
function(kif_use_project)
    cmake_parse_arguments(A "" "FILE" "" ${ARGN})
    if(NOT A_FILE)
        set(dir "${CMAKE_CURRENT_SOURCE_DIR}")
        while(NOT EXISTS "${dir}/kif.toml")
            get_filename_component(parent "${dir}" DIRECTORY)
            if(parent STREQUAL dir)
                message(FATAL_ERROR "kif_use_project: no kif.toml found (pass FILE)")
            endif()
            set(dir "${parent}")
        endwhile()
        set(A_FILE "${dir}/kif.toml")
    endif()
    get_filename_component(A_FILE "${A_FILE}" ABSOLUTE)
    get_property(_kif_python GLOBAL PROPERTY KIF_PYTHON_EXECUTABLE)
    get_property(_kif_root GLOBAL PROPERTY KIF_ROOT_DIR)
    string(MD5 id "${A_FILE}")
    set(fragment "${CMAKE_CURRENT_BINARY_DIR}/kif-project-${id}.cmake")
    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${_kif_root}" "${_kif_python}" -m kiflang.kif project --cmake --project "${A_FILE}"
        OUTPUT_FILE "${fragment}" ERROR_VARIABLE err RESULT_VARIABLE rc)
    if(NOT rc EQUAL 0)
        message(FATAL_ERROR "kif_use_project: ${err}")
    endif()
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${A_FILE}")
    include("${fragment}")
    # include() ran in this function's scope: hand its variables to the caller.
    get_cmake_property(vars VARIABLES)
    foreach(v IN LISTS vars)
        if(v MATCHES "^KIF_PROJECT_")
            set(${v} "${${v}}" PARENT_SCOPE)
        endif()
    endforeach()
endfunction()

# SOURCES may mix .kif and FreeBSD kobj .m files; a .m file is converted with kiflang.mimport first.
# Runs `kifc` once PER SOURCE FILE and backend, so a changed .kif regenerates only its own outputs, plus those of the
# files that import it (found through the per-file depfile). Generated files go to <out>/<backend>.
# MANIFESTS also writes a per-file manifest <out>/manifests/<stem>.json.
# Sets <prefix>_OUTPUTS (all files), <prefix>_HEADERS, <prefix>_SOURCES, <prefix>_MANIFESTS and <prefix>_INCLUDE_DIR.
# The kifc options of one target (appended after the build-wide ones, so they win for target/profile/sysroot and add to the lists).
# Each setting is a keyword of add_kif_library()/kif_target_sources() or, when `tgt` already exists, the target property of the same
# name (set_target_properties(tgt PROPERTIES KIF_TARGET freebsd-kernel KIF_CFG "smp;debug_locks")). A keyword beats the property.
function(_kif_property_args out tgt)
    cmake_parse_arguments(P "KIF_WERROR" "KIF_TARGET;KIF_PROFILE;KIF_SYSROOT;KIF_SIGNING;KIF_SMALL_STACK" "KIF_SANITIZE;KIF_CFG;KIF_INCLUDE_DIRECTORIES;KIF_TRUST;KIF_REVOKED" ${ARGN})
    set(args)
    foreach(key IN ITEMS KIF_TARGET KIF_PROFILE KIF_SYSROOT KIF_SANITIZE KIF_CFG KIF_INCLUDE_DIRECTORIES KIF_TRUST KIF_REVOKED KIF_SIGNING KIF_SMALL_STACK)
        set(values "${P_${key}}")
        if(values STREQUAL "" AND tgt AND TARGET ${tgt})
            get_target_property(values ${tgt} ${key})
            if(NOT values)
                set(values)
            endif()
        endif()
        if(key STREQUAL "KIF_INCLUDE_DIRECTORIES")
            set(opt -I)
        else()
            string(REPLACE "KIF_" "--" opt "${key}")
            string(TOLOWER "${opt}" opt)
            string(REPLACE "_" "-" opt "${opt}")
        endif()
        foreach(v IN LISTS values)
            if(key MATCHES "^KIF_(TRUST|REVOKED)$")
                get_filename_component(v "${v}" ABSOLUTE)
            endif()
            list(APPEND args ${opt} "${v}")
        endforeach()
    endforeach()
    set(werror ${P_KIF_WERROR})
    if(NOT werror AND tgt AND TARGET ${tgt})
        get_target_property(werror ${tgt} KIF_WERROR)
    endif()
    if(werror)
        list(APPEND args --Werror)
    endif()
    set(${out} ${args} PARENT_SCOPE)
endfunction()

function(_kif_generate prefix backend out_dir)
    cmake_parse_arguments(A "WERROR;MANIFESTS" "" "SOURCES;INCLUDE_DIRS;CHECK_WITH;TARGET_ARGS" ${ARGN})

    set(hdr_suffix _if)
    set(extra_flags)
    if(backend STREQUAL "cpp")
        set(flag --cpp)
        set(hdr_ext hpp)
        set(src_ext cpp)
    elseif(backend STREQUAL "c")
        # Plain C: <stem>_proc.h/.c, which include the per-target kif_c.h (written once into the same directory).
        set(flag --c)
        set(hdr_ext h)
        set(src_ext c)
        set(hdr_suffix _proc)
        set(extra_flags --emit-source)
    else()
        # FreeBSD kobj: <iface>_if.h/.c, plus <stem>_kobj.c (the class of `impl <kobj iface> for S`; a stub when there is none).
        set(flag --freebsd)
        set(hdr_ext h)
        set(src_ext c)
        set(extra_flags --emit-source)
    endif()
    set(gen_dir "${out_dir}/${backend}")

    set(common_args)
    foreach(dir IN LISTS A_INCLUDE_DIRS)
        list(APPEND common_args -I "${dir}")
    endforeach()
    _kif_base_args(base_args)
    list(APPEND common_args ${base_args} ${A_TARGET_ARGS})
    if(A_WERROR)
        list(APPEND common_args --Werror)
    endif()
    set(extra_depends)
    # the trust and revocation files are inputs of every check: a changed key re-runs them
    set(prev)
    foreach(a IN LISTS common_args)
        if(prev MATCHES "^--(trust|revoked)$")
            list(APPEND extra_depends "${a}")
        endif()
        set(prev "${a}")
    endforeach()
    foreach(m IN LISTS A_CHECK_WITH)
        list(APPEND common_args --check-with "${m}")
        list(APPEND extra_depends "${m}")
    endforeach()

    get_property(_kif_python GLOBAL PROPERTY KIF_PYTHON_EXECUTABLE)
    get_property(KIF_ROOT GLOBAL PROPERTY KIF_ROOT_DIR)
    # Changing the compiler itself regenerates everything.
    file(GLOB_RECURSE kif_python CONFIGURE_DEPENDS "${KIF_ROOT}/kiflang/*.py")

    set(outputs)
    set(headers)
    set(sources)
    set(manifests)
    set(c_common)
    if(backend STREQUAL "c")
        # kif_c.h depends only on the target (and profile), so one rule serves every file; the files include it.
        set(c_common "${gen_dir}/kif_c.h")
        add_custom_command(
            OUTPUT "${c_common}"
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${gen_dir}"
            COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${KIF_ROOT}"
                    "${_kif_python}" -m kiflang.kifc --c-common "${gen_dir}" ${base_args} ${A_TARGET_ARGS}
            DEPENDS ${kif_python}
            COMMENT "kifc (c): kif_c.h"
            VERBATIM)
        list(APPEND headers "${c_common}")
        list(APPEND outputs "${c_common}")
        list(APPEND extra_depends "${c_common}")
    endif()
    foreach(src IN LISTS A_SOURCES)
        get_filename_component(abs "${src}" ABSOLUTE)
        get_filename_component(stem "${abs}" NAME_WE)
        if(abs MATCHES "\\.m$")
            # FreeBSD kobj interface: converted to <out>/m/<name>.kif first (once, whatever the backend count).
            set(converted "${out_dir}/m/${stem}.kif")
            get_property(_declared GLOBAL PROPERTY _KIF_M_RULES)
            if(NOT converted IN_LIST _declared)
                add_custom_command(
                    OUTPUT "${converted}"
                    COMMAND "${CMAKE_COMMAND}" -E make_directory "${out_dir}/m"
                    COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${KIF_ROOT}"
                            "${_kif_python}" -m kiflang.mimport -o "${converted}" "${abs}"
                    DEPENDS "${abs}" ${kif_python}
                    COMMENT "kif-mimport: ${src}"
                    VERBATIM)
                set_property(GLOBAL APPEND PROPERTY _KIF_M_RULES "${converted}")
            endif()
            set(abs "${converted}")
        endif()
        string(REGEX REPLACE "_if$" "" stem "${stem}")
        set(hdr "${gen_dir}/${stem}${hdr_suffix}.${hdr_ext}")
        set(cc "${gen_dir}/${stem}${hdr_suffix}.${src_ext}")
        set(file_outputs "${hdr}" "${cc}")
        if(backend STREQUAL "freebsd")
            set(kobj_cc "${gen_dir}/${stem}_kobj.c")
            list(APPEND file_outputs "${kobj_cc}")
            list(APPEND sources "${kobj_cc}")
        endif()
        set(args ${flag} "${gen_dir}" ${extra_flags} ${common_args})
        if(A_MANIFESTS)
            set(man "${out_dir}/manifests/${stem}.json")
            list(APPEND args --manifest "${man}")
            list(APPEND file_outputs "${man}")
            list(APPEND manifests "${man}")
        endif()

        # DEPFILE is only supported by Ninja and Makefile generators; others fall back to the explicit source.
        set(depfile_arg)
        if(CMAKE_GENERATOR MATCHES "Ninja|Makefiles")
            set(depfile "${gen_dir}/${stem}.d")
            list(APPEND args --depfile "${depfile}")
            set(depfile_arg DEPFILE "${depfile}")
        endif()

        add_custom_command(
            OUTPUT ${file_outputs}
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${gen_dir}"
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${out_dir}/manifests"
            COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${KIF_ROOT}"
                    "${_kif_python}" -m kiflang.kifc ${args} "${abs}"
            DEPENDS "${abs}" ${kif_python} ${extra_depends}
            ${depfile_arg}
            COMMENT "kifc (${backend}): ${src}"
            VERBATIM)
        list(APPEND headers "${hdr}")
        list(APPEND sources "${cc}")
        list(APPEND outputs ${file_outputs})
    endforeach()

    set(${prefix}_OUTPUTS ${outputs} PARENT_SCOPE)
    set(${prefix}_HEADERS ${headers} PARENT_SCOPE)
    set(${prefix}_SOURCES ${sources} PARENT_SCOPE)
    set(${prefix}_MANIFESTS ${manifests} PARENT_SCOPE)
    set(${prefix}_INCLUDE_DIR "${gen_dir}" PARENT_SCOPE)
endfunction()

# Compiles the sources of a library. The C++ backend yields `<stem>_if.hpp/.cpp`; FreeBSD yields `<stem>_if.h/.c` and `<stem>_enums.h`.
function(add_kif_library name)
    cmake_parse_arguments(A "WERROR;KIF_WERROR" "OUTPUT_DIR;TYPE;VISIBILITY;KIF_TARGET;KIF_PROFILE;KIF_SYSROOT;KIF_SIGNING;KIF_SMALL_STACK"
                          "SOURCES;BACKENDS;INCLUDE_DIRS;LINK_LIBRARIES;CHECK_WITH;KIFC_ARGS;KIF_SANITIZE;KIF_CFG;KIF_INCLUDE_DIRECTORIES;KIF_TRUST;KIF_REVOKED" ${ARGN})
    set(prop_args KIF_TARGET "${A_KIF_TARGET}" KIF_PROFILE "${A_KIF_PROFILE}" KIF_SYSROOT "${A_KIF_SYSROOT}"
        KIF_SANITIZE ${A_KIF_SANITIZE} KIF_CFG ${A_KIF_CFG} KIF_INCLUDE_DIRECTORIES ${A_KIF_INCLUDE_DIRECTORIES}
        KIF_TRUST ${A_KIF_TRUST} KIF_REVOKED ${A_KIF_REVOKED} KIF_SIGNING "${A_KIF_SIGNING}" KIF_SMALL_STACK "${A_KIF_SMALL_STACK}")
    if(A_KIF_WERROR)
        list(APPEND prop_args KIF_WERROR)
    endif()
    _kif_property_args(target_args "" ${prop_args})
    if(NOT A_SOURCES)
        set(A_SOURCES ${KIF_PROJECT_SOURCES})  # `[build] sources` of kif.toml (kif_use_project)
    endif()
    if(NOT A_SOURCES)
        message(FATAL_ERROR "add_kif_library(${name}): SOURCES is required (or kif_use_project() with [build] sources)")
    endif()
    if(NOT A_BACKENDS)
        set(A_BACKENDS cpp)
    endif()
    if(NOT A_OUTPUT_DIR)
        set(A_OUTPUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/kif/${name}")
    endif()

    # Manifests of the libraries this one must not collide with (see --check-with).
    set(check_manifests)
    foreach(other IN LISTS A_CHECK_WITH)
        get_target_property(m ${other} KIF_MANIFEST)
        if(m)
            list(APPEND check_manifests "${m}")
        endif()
    endforeach()
    set(manifest "${A_OUTPUT_DIR}/${name}.manifest.json")
    set(file_manifests)

    set(all_sources)
    set(all_includes)
    set(all_headers)
    set(first TRUE)
    foreach(backend IN LISTS A_BACKENDS)
        if(NOT backend MATCHES "^(cpp|freebsd|c)$")
            message(FATAL_ERROR "add_kif_library(${name}): unknown backend '${backend}' (cpp, freebsd or c)")
        endif()
        set(opts SOURCES ${A_SOURCES} INCLUDE_DIRS ${A_INCLUDE_DIRS} CHECK_WITH ${check_manifests})
        if(backend STREQUAL "c" OR backend STREQUAL "freebsd")
            list(APPEND opts TARGET_ARGS ${target_args} ${A_KIFC_ARGS})
        endif()
        if(A_WERROR)
            list(APPEND opts WERROR)
        endif()
        if(first)
            # The first backend also writes one manifest per source file.
            list(APPEND opts MANIFESTS)
            set(first FALSE)
        endif()
        _kif_generate(gen ${backend} "${A_OUTPUT_DIR}" ${opts})
        list(APPEND file_manifests ${gen_MANIFESTS})
        list(APPEND all_sources ${gen_SOURCES})
        list(APPEND all_headers ${gen_HEADERS})
        list(APPEND all_includes "${gen_INCLUDE_DIR}")
        set_property(GLOBAL APPEND PROPERTY _KIF_${name}_HEADERS ${gen_HEADERS})
    endforeach()

    # The library manifest merges the per-file ones (and checks the files of the library against each other), so
    # changing one file rewrites it, and its content only changes if an interface changed.
    get_property(_kif_python GLOBAL PROPERTY KIF_PYTHON_EXECUTABLE)
    get_property(_kif_root GLOBAL PROPERTY KIF_ROOT_DIR)
    add_custom_command(
        OUTPUT "${manifest}"
        COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${_kif_root}"
                "${_kif_python}" -m kiflang.kifc --no-project --merge-manifest "${manifest}" ${file_manifests}
        DEPENDS ${file_manifests}
        COMMENT "kifc (manifest): ${name}"
        VERBATIM)
    set_source_files_properties("${manifest}" PROPERTIES GENERATED TRUE HEADER_FILE_ONLY TRUE)

    if(A_TYPE STREQUAL "EXECUTABLE")
        add_executable(${name} ${all_sources} ${all_headers} "${manifest}")
    elseif(NOT A_TYPE OR A_TYPE MATCHES "^(STATIC|SHARED|OBJECT)$")
        add_library(${name} ${A_TYPE} ${all_sources} ${all_headers} "${manifest}")
    else()
        message(FATAL_ERROR "add_kif_library(${name}): TYPE is STATIC, SHARED, OBJECT or EXECUTABLE (default: BUILD_SHARED_LIBS decides)")
    endif()
    foreach(other IN LISTS A_CHECK_WITH)
        if(TARGET ${other})
            add_dependencies(${name} ${other})  # its manifest must exist before ours is checked against it
        endif()
    endforeach()
    target_include_directories(${name} PUBLIC "$<BUILD_INTERFACE:${all_includes}>")
    if(A_VISIBILITY)
        # -fvisibility=<v> for the generated C: with `hidden`, only `#[visibility(default)]` functions are in the .so
        set_target_properties(${name} PROPERTIES C_VISIBILITY_PRESET ${A_VISIBILITY} CXX_VISIBILITY_PRESET ${A_VISIBILITY})
    endif()
    if(A_LINK_LIBRARIES)
        target_link_libraries(${name} PUBLIC ${A_LINK_LIBRARIES})
    endif()
    if("cpp" IN_LIST A_BACKENDS AND TARGET jplcz_kif::kif)
        # The generated C++ includes <kif/kobj/kobj.hpp>.
        target_link_libraries(${name} PUBLIC jplcz_kif::kif)
    endif()
    set_target_properties(${name} PROPERTIES
        KIF_MANIFEST "${manifest}"
        KIF_TARGET "${A_KIF_TARGET}" KIF_PROFILE "${A_KIF_PROFILE}" KIF_SYSROOT "${A_KIF_SYSROOT}"
        KIF_SANITIZE "${A_KIF_SANITIZE}" KIF_CFG "${A_KIF_CFG}" KIF_INCLUDE_DIRECTORIES "${A_KIF_INCLUDE_DIRECTORIES}"
        KIF_TRUST "${A_KIF_TRUST}" KIF_REVOKED "${A_KIF_REVOKED}" KIF_SIGNING "${A_KIF_SIGNING}" KIF_SMALL_STACK "${A_KIF_SMALL_STACK}"
        KIF_FILE_MANIFESTS "${file_manifests}"
        KIF_GENERATED_HEADERS "${all_headers}"
        LINKER_LANGUAGE CXX)
    if(NOT A_BACKENDS STREQUAL "cpp")
        # FreeBSD and C output is C; make sure C is enabled for it.
        enable_language(C)
        if(A_BACKENDS STREQUAL "c")
            set_target_properties(${name} PROPERTIES LINKER_LANGUAGE C)
        else()
            set_target_properties(${name} PROPERTIES LINKER_LANGUAGE CXX)
        endif()
    endif()
endfunction()

# Adds generated files for the given sources to an existing target (which keeps its own name and properties).
# A program: the sources define `fn main` (docs/kif-proc-language.md, Entry point). Same arguments as add_kif_library().
function(add_kif_executable name)
    add_kif_library(${name} TYPE EXECUTABLE ${ARGN})
endfunction()

function(kif_target_sources target)
    cmake_parse_arguments(A "WERROR;KIF_WERROR" "BACKEND;OUTPUT_DIR;KIF_TARGET;KIF_PROFILE;KIF_SYSROOT;KIF_SIGNING;KIF_SMALL_STACK"
                          "SOURCES;INCLUDE_DIRS;KIFC_ARGS;KIF_SANITIZE;KIF_CFG;KIF_INCLUDE_DIRECTORIES;KIF_TRUST;KIF_REVOKED" ${ARGN})
    set(prop_args KIF_TARGET "${A_KIF_TARGET}" KIF_PROFILE "${A_KIF_PROFILE}" KIF_SYSROOT "${A_KIF_SYSROOT}"
        KIF_SANITIZE ${A_KIF_SANITIZE} KIF_CFG ${A_KIF_CFG} KIF_INCLUDE_DIRECTORIES ${A_KIF_INCLUDE_DIRECTORIES}
        KIF_TRUST ${A_KIF_TRUST} KIF_REVOKED ${A_KIF_REVOKED} KIF_SIGNING "${A_KIF_SIGNING}" KIF_SMALL_STACK "${A_KIF_SMALL_STACK}")
    if(A_KIF_WERROR)
        list(APPEND prop_args KIF_WERROR)
    endif()
    _kif_property_args(target_args ${target} ${prop_args})
    if(NOT A_SOURCES)
        message(FATAL_ERROR "kif_target_sources(${target}): SOURCES is required")
    endif()
    if(NOT A_BACKEND)
        set(A_BACKEND cpp)
    endif()
    if(NOT A_OUTPUT_DIR)
        set(A_OUTPUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/kif/${target}")
    endif()
    set(opts SOURCES ${A_SOURCES} INCLUDE_DIRS ${A_INCLUDE_DIRS})
    if(A_BACKEND STREQUAL "c" OR A_BACKEND STREQUAL "freebsd")
        list(APPEND opts TARGET_ARGS ${target_args} ${A_KIFC_ARGS})
    endif()
    if(A_WERROR)
        list(APPEND opts WERROR)
    endif()
    _kif_generate(gen ${A_BACKEND} "${A_OUTPUT_DIR}" ${opts})
    target_sources(${target} PRIVATE ${gen_SOURCES} ${gen_HEADERS})
    target_include_directories(${target} PUBLIC "$<BUILD_INTERFACE:${gen_INCLUDE_DIR}>")
    set_property(TARGET ${target} APPEND PROPERTY KIF_GENERATED_HEADERS ${gen_HEADERS})
    if(A_BACKEND STREQUAL "cpp" AND TARGET jplcz_kif::kif)
        target_link_libraries(${target} PUBLIC jplcz_kif::kif)
    endif()
endfunction()

# Compares the compiler's stack frames with kif's estimate (docs/kif-stack.md, `kif stack-check`): adds the custom target <name>, which fails
# when the compiler used more stack than the estimate says. SOURCES are the .kif files (the same ones the library was built from).
#   TARGET t   the CMake target that compiles the generated C: `-fstack-usage` is added to its C compile options (GCC and clang write
#              `<object>.su` next to each object) and the check reads those files, so the frames are the ones of YOUR compiler and flags.
#              Build `t` first (the custom target depends on it); a `<name>` run re-reads the files of the last build.
#   (no TARGET) the check lowers and compiles the C itself with CMAKE_C_COMPILER and CMAKE_C_FLAGS plus the build type's, plus FLAGS.
#   FLAGS f..  extra compiler flags for that private compile; ALL adds the check to the default build.
# The kif options (target, profile, cfg, sanitize, include dirs) come from the properties of TARGET and the build-wide KIF_* settings.
function(kif_stack_check name)
    cmake_parse_arguments(A "ALL" "TARGET" "SOURCES;FLAGS" ${ARGN})
    if(NOT A_SOURCES)
        message(FATAL_ERROR "kif_stack_check(${name}): SOURCES is required")
    endif()
    _kif_base_args(base_args)
    if(A_TARGET)
        if(NOT TARGET ${A_TARGET})
            message(FATAL_ERROR "kif_stack_check(${name}): TARGET ${A_TARGET} is not a target")
        endif()
        _kif_property_args(target_args ${A_TARGET})
    else()
        set(target_args)
    endif()
    # stack-check takes only the options that decide what the lowered code is
    set(keep --target --profile --cfg --sanitize -I)
    set(args)
    set(all_args ${base_args} ${target_args})
    list(LENGTH all_args n)
    set(i 0)
    while(i LESS n)
        list(GET all_args ${i} opt)
        if(opt STREQUAL "--Werror")
            math(EXPR i "${i} + 1")
        else()
            math(EXPR j "${i} + 1")
            if(j LESS n AND opt IN_LIST keep)
                list(GET all_args ${j} val)
                list(APPEND args ${opt} "${val}")
            endif()
            math(EXPR i "${i} + 2")
        endif()
    endwhile()
    get_property(_kif_python GLOBAL PROPERTY KIF_PYTHON_EXECUTABLE)
    get_property(_kif_root GLOBAL PROPERTY KIF_ROOT_DIR)
    set(mode_args)
    if(A_TARGET)
        target_compile_options(${A_TARGET} PRIVATE
            "$<$<AND:$<COMPILE_LANGUAGE:C>,$<OR:$<C_COMPILER_ID:GNU>,$<C_COMPILER_ID:Clang>,$<C_COMPILER_ID:AppleClang>>>:-fstack-usage>")
        get_target_property(bin_dir ${A_TARGET} BINARY_DIR)
        list(APPEND mode_args --su "${bin_dir}/CMakeFiles/${A_TARGET}.dir")
    else()
        enable_language(C)
        string(TOUPPER "${CMAKE_BUILD_TYPE}" cfg_upper)
        string(STRIP "${CMAKE_C_FLAGS} ${CMAKE_C_FLAGS_${cfg_upper}} ${A_FLAGS}" flags)
        list(APPEND mode_args --cc "${CMAKE_C_COMPILER}")
        if(NOT flags STREQUAL "")
            list(APPEND mode_args "--flags=${flags}")
        endif()
    endif()
    set(commands)
    foreach(src IN LISTS A_SOURCES)
        get_filename_component(abs "${src}" ABSOLUTE)
        list(APPEND commands COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${_kif_root}"
             "${_kif_python}" -m kiflang.kif stack-check "${abs}" ${args} ${mode_args})
    endforeach()
    set(all)
    if(A_ALL)
        set(all ALL)
    endif()
    add_custom_target(${name} ${all} ${commands} COMMENT "kif stack-check: ${name}" VERBATIM)
    if(A_TARGET)
        add_dependencies(${name} ${A_TARGET})
    endif()
endfunction()

# Installs the generated headers of a target into an SDK include directory.
function(kif_install_headers target)
    cmake_parse_arguments(A "" "DESTINATION;COMPONENT" "" ${ARGN})
    if(NOT A_DESTINATION)
        message(FATAL_ERROR "kif_install_headers(${target}): DESTINATION is required")
    endif()
    get_target_property(headers ${target} KIF_GENERATED_HEADERS)
    if(NOT headers)
        message(FATAL_ERROR "kif_install_headers(${target}): target has no generated headers")
    endif()
    set(comp)
    if(A_COMPONENT)
        set(comp COMPONENT ${A_COMPONENT})
    endif()
    install(FILES ${headers} DESTINATION "${A_DESTINATION}" ${comp})
endfunction()
