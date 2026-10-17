// SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
//
// SPDX-License-Identifier: BSD-2-Clause
#pragma once

/**
 * @file status.hpp
 * @brief Converts between `reloco::result` (C++) and the `int` status of a C kobj method (`cresult<>` in .kif).
 *
 * A `cresult<void>` / `cresult<i32>` method returns `reloco::result<void>` / `reloco::result<int>` in C++ and `int`
 * in C. The interface's `errno` clause picks the C encoding, which differs between kernels:
 *
 * | style            | failure   | success / value        | typical user          |
 * |------------------|-----------|------------------------|-----------------------|
 * | `errno_positive` | `+errno`  | any `<= 0` (probe prio)| FreeBSD               |
 * | `errno_negative` | `-errno`  | any `>= 0`             | kernels using -EINVAL |
 *
 * The errno *numbers* are kernel specific too. By default reloco's `to_errno` / `from_errno` are used; to use your
 * own table, define `KIF_KOBJ_ERRNO_MAP` before including any kobj header to a type that has
 * `static int to_errno(reloco::error) noexcept` and `static reloco::error from_errno(int) noexcept`, both working
 * with positive errno values (the style applies the sign):
 *
 * @code
 * struct my_errno {
 *     static int to_errno(reloco::error e) noexcept { return e == reloco::error::busy ? MY_EBUSY : MY_EIO; }
 *     static reloco::error from_errno(int v) noexcept { return v == MY_EBUSY ? reloco::error::busy : reloco::error::io_error; }
 * };
 * #define KIF_KOBJ_ERRNO_MAP ::my_errno
 * @endcode
 *
 * Both conversions are lossy: several errno values map to one `reloco::error`, and the reverse.
 */

#include <type_traits>

#include <reloco/error.hpp>
#include <reloco/expected.hpp>

#ifndef KIF_KOBJ_ERRNO_MAP
#include <reloco/error_errno.hpp>
#endif

namespace kif::kobj {

#ifndef KIF_KOBJ_ERRNO_MAP
/// The default errno numbering: reloco's best-effort translators.
struct reloco_errno_map {
    [[nodiscard]] static constexpr int to_errno(reloco::error e) noexcept { return reloco::to_errno(e); }
    [[nodiscard]] static constexpr reloco::error from_errno(int v) noexcept { return reloco::from_errno(v); }
};
using errno_map = reloco_errno_map;
#else
using errno_map = KIF_KOBJ_ERRNO_MAP;
#endif

namespace detail {
/// The positive errno a (possibly negated) status stands for; INT_MIN has no positive counterpart and becomes 0
/// (an unknown errno).
[[nodiscard]] constexpr int positive_errno(int rc) noexcept {
    if (rc >= 0) return rc;
    return rc == -2147483647 - 1 ? 0 : -rc;
}
}  // namespace detail

/// FreeBSD style: a positive value is `errno`; `0` and negative values are results (`DEVICE_PROBE` returns
/// `0` or a negative priority such as `BUS_PROBE_DEFAULT`).
struct errno_positive {
    [[nodiscard]] static int encode(const reloco::result<void> &r) noexcept {
        return r.has_value() ? 0 : errno_map::to_errno(r.error());
    }
    /// A value that is positive cannot travel as a result: it encodes as an out-of-range failure.
    [[nodiscard]] static int encode(const reloco::result<int> &r) noexcept {
        if (!r.has_value()) return errno_map::to_errno(r.error());
        return r.value() > 0 ? errno_map::to_errno(reloco::error::out_of_range) : r.value();
    }
    [[nodiscard]] static reloco::result<void> decode_void(int rc) noexcept {
        if (rc <= 0) return {};
        return reloco::unexpected(errno_map::from_errno(rc));
    }
    [[nodiscard]] static reloco::result<int> decode_int(int rc) noexcept {
        if (rc <= 0) return rc;
        return reloco::unexpected(errno_map::from_errno(rc));
    }
};

/// Kernels that return `-errno` on failure; `0` and positive values are results.
struct errno_negative {
    [[nodiscard]] static int encode(const reloco::result<void> &r) noexcept {
        return r.has_value() ? 0 : -errno_map::to_errno(r.error());
    }
    /// A value that is negative cannot travel as a result: it encodes as an out-of-range failure.
    [[nodiscard]] static int encode(const reloco::result<int> &r) noexcept {
        if (!r.has_value()) return -errno_map::to_errno(r.error());
        return r.value() < 0 ? -errno_map::to_errno(reloco::error::out_of_range) : r.value();
    }
    [[nodiscard]] static reloco::result<void> decode_void(int rc) noexcept {
        if (rc >= 0) return {};
        return reloco::unexpected(errno_map::from_errno(detail::positive_errno(rc)));
    }
    [[nodiscard]] static reloco::result<int> decode_int(int rc) noexcept {
        if (rc >= 0) return rc;
        return reloco::unexpected(errno_map::from_errno(detail::positive_errno(rc)));
    }
};

/// `encode<Style>(r)`: the C status for a result.
template <class Style, class T>
[[nodiscard]] inline int encode(const reloco::result<T> &r) noexcept {
    return Style::encode(r);
}

/// `decode<Style, T>(rc)`: the result a C status stands for; `T` is `void` or `int`.
template <class Style, class T>
[[nodiscard]] inline reloco::result<T> decode(int rc) noexcept {
    if constexpr (std::is_void_v<T>)
        return Style::decode_void(rc);
    else
        return Style::decode_int(rc);
}

}  // namespace kif::kobj
