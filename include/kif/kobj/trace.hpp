// SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
//
// SPDX-License-Identifier: BSD-2-Clause

#pragma once

/// @file trace.hpp
/// Optional call tracing for kobj: every traced method call is written to a microfmt logger.
///
/// Tracing exists only when `KIF_KOBJ_TRACE` is defined on the compiler command line (`-DKIF_KOBJ_TRACE`) for
/// `kobj_c.cpp` and for each file whose calls should be reported; without it the trace API is not compiled at all and
/// untraced code pays nothing. There is no runtime switch to turn tracing on in a build that lacks it. Reporting is
/// global: install a `logger_tracer` and every
/// traced call in the process becomes one record at `microfmt::log::level::trace`, e.g.
///
///     kobj: sim_uart.mmio_read -> 0x55d0c2a1b3c0
///     kobj: sim_rom.mmio_read -> 0x55d0c2a1b4e0 (interface default)
///
/// See docs/kobj-runtime.md ("Call tracing") for the full guide.

#if __cplusplus < 202002L && !(defined(_MSVC_LANG) && _MSVC_LANG >= 202002L)
#error "kif/kobj/trace.hpp needs C++20 (microfmt's logger does); the rest of the kobj runtime needs only C++17"
#endif

#ifndef KIF_KOBJ_TRACE
#error "call tracing is compile-time opt-in: define KIF_KOBJ_TRACE (-DKIF_KOBJ_TRACE) for kobj_c.cpp and this file's users"
#endif

#include <cstdint>

#include <microfmt/log/logger.hpp>
#include <reloco/lifetime.hpp>

#include "compat/sys/kobj.h"

namespace kif::kobj {

/// Writes traced calls to a microfmt logger while enabled. Not copyable or movable: the runtime keeps a pointer to it.
///
/// `Logger` is any microfmt logger type (`microfmt::log::basic_logger<...>`, `extended_logger<...>`); it must outlive
/// the tracer. At most one tracer (or other `kobj_trace_sink`) is active at a time.
template <class Logger>
class RELOCO_POINTER logger_tracer {
public:
    explicit logger_tracer(Logger &logger RELOCO_LIFETIMEBOUND RELOCO_LIFETIME_CAPTURE_BY_THIS) noexcept
        : logger_(logger) {
        sink_.fn = &logger_tracer::emit;
        sink_.ctx = this;
    }
    logger_tracer(Logger &&) = delete;
    logger_tracer(const logger_tracer &) = delete;
    logger_tracer &operator=(const logger_tracer &) = delete;
    ~logger_tracer() { disable(); }

    /// Starts reporting calls (replacing any other sink). Idempotent.
    void enable() noexcept {
        kobj_trace_set(&sink_);
        enabled_ = true;
    }

    /// Stops reporting. Only the sink installed by this tracer is removed. Callers must make sure no traced call is in
    /// flight on another thread before destroying the tracer or the logger.
    void disable() noexcept {
        if (enabled_) {
            kobj_trace_set(nullptr);
            enabled_ = false;
        }
    }

    [[nodiscard]] bool enabled() const noexcept { return enabled_; }

private:
    RELOCO_BEGIN_UNSAFE_CODE  // bridges the C callback (void *ctx, C strings, function pointers)
    static microfmt::string_view view(const char *s) noexcept {
        return s ? microfmt::string_view(s) : microfmt::string_view();
    }

    static void emit(void *ctx, const kobj_trace_event *ev) noexcept {
        Logger &logger = reloco::unsafe::ptr_cast<logger_tracer>(ctx)->logger_;
        if (!logger.should_log(microfmt::log::level::trace))
            return;
        // The record is cheap to build only when the level is enabled, so the check above comes first.
        const auto impl = reinterpret_cast<std::uintptr_t>(ev->func);
        const microfmt::string_view cls = view(ev->cls ? ev->cls->name : nullptr);
        const microfmt::string_view note = ev->deflt ? microfmt::string_view(" (interface default)") : microfmt::string_view();
        logger.log(microfmt::log::level::trace, "kobj: {}.{} -> {:#x}{}", cls, view(ev->op), impl, note);
    }
    RELOCO_END_UNSAFE_CODE

    Logger &logger_;
    kobj_trace_sink sink_{};
    bool enabled_ = false;
};

}  // namespace kif::kobj
