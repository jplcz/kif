# SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception

set(SOURCE_PATH "${CURRENT_PORT_DIR}/../../../..")

vcpkg_cmake_configure(
    SOURCE_PATH "${SOURCE_PATH}"
    OPTIONS
        -DJPLCZ_KIF_USE_SYSTEM_DEPS=ON
        -DJPLCZ_KIF_INSTALL=ON
        -DJPLCZ_KIF_BUILD_TESTS=OFF
        -DJPLCZ_KIF_BUILD_HEADER_CHECKS=OFF
        -DJPLCZ_KIF_CHECK_NO_STD_TYPES=OFF
        -DJPLCZ_KIF_ENABLE_STRICT_WARNINGS=OFF
        -DJPLCZ_KIF_ENABLE_WERROR=OFF
)

vcpkg_cmake_install()
vcpkg_cmake_config_fixup(
    PACKAGE_NAME jplcz_kif
    CONFIG_PATH share/cmake/jplcz_kif
)

file(REMOVE_RECURSE
    "${CURRENT_PACKAGES_DIR}/debug"
    "${CURRENT_PACKAGES_DIR}/lib"
)
file(MAKE_DIRECTORY "${CURRENT_PACKAGES_DIR}/share/${PORT}")
file(TOUCH "${CURRENT_PACKAGES_DIR}/share/${PORT}/usage-accurate")
vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/LICENSE")
