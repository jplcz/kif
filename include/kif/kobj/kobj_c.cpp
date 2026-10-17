// SPDX-FileCopyrightText: 2026 Jarosław Pelczar <jarek@jpelczar.com>
//
// SPDX-License-Identifier: BSD-2-Clause

// Implementation of the drop-in <sys/kobj.h> (compat/sys/kobj.h). Compile this file once into your program or
// library with your own build rules (add -DKIF_KOBJ_TRACE to enable call tracing); see docs/kobj-runtime.md. All memory comes from reloco allocators.

#include <errno.h>
#include <string.h>

#include <new>

#include <reloco/allocator.hpp>
#include <reloco/default_allocator.hpp>
#include <reloco/lifetime.hpp>
#include <reloco/mutex.hpp>

#include "compat/sys/kobj.h"

// This file implements the C ABI of compat/sys/kobj.h (raw pointers, casts, blocks carved from raw memory), so
// reloco-tidy's rules are off for all of it; the checked C++ API is in the header.
RELOCO_BEGIN_UNSAFE_CODE

namespace {

constexpr std::size_t block_align = 16;

// Bookkeeping stored in front of every block we allocate, so it can be freed through the same allocator.
struct holder {
    bool is_c;
    std::size_t total;
    reloco::allocator_ref ref;
    kif_allocator c;
};
constexpr std::size_t prefix = (sizeof(holder) + block_align - 1) / block_align * block_align;

kobjop_desc null_desc;
kobj_method_t null_method{&null_desc, nullptr};  // never equals a real descriptor, so a cache miss

reloco::mutex g_lock;  // guards class compilation and descriptor id assignment; never held across an allocator call
unsigned g_next_id = 1;
#ifdef KIF_KOBJ_TRACE
const kobj_trace_sink *g_trace = nullptr;  // accessed only with atomics
#endif

RELOCO_BEGIN_UNSAFE_BUFFER_USAGE

// Allocates `payload` zeroed bytes behind a holder; returns the payload or nullptr.
void *alloc_with(const holder &src, std::size_t payload) noexcept {
    const std::size_t total = prefix + payload;
    unsigned char *base = nullptr;
    if (src.is_c) {
        if (!src.c.alloc)
            return nullptr;
        base = static_cast<unsigned char *>(src.c.alloc(src.c.ctx, total, block_align));
    } else {
        auto r = src.ref.allocate(total, block_align);
        if (r)
            base = static_cast<unsigned char *>(r->ptr);
    }
    if (!base)
        return nullptr;
    memset(base, 0, total);
    holder *h = new (base) holder(src);
    h->total = total;
    return base + prefix;
}

void free_block(void *payload) noexcept {
    unsigned char *base = static_cast<unsigned char *>(payload) - prefix;
    const holder h = *reinterpret_cast<holder *>(base);
    if (h.is_c) {
        if (h.c.free)
            h.c.free(h.c.ctx, base, h.total);
    } else {
        h.ref.deallocate(base, h.total);
    }
}

RELOCO_END_UNSAFE_BUFFER_USAGE

holder from_ref(reloco::allocator_ref a) noexcept { return holder{false, 0, a, {}}; }
holder from_c(const kif_allocator *a) noexcept {
    if (!a)
        return from_ref(reloco::default_allocator());
    return holder{true, 0, {}, *a};
}

kobj_method_t *find_method(kobj_class_t cls, kobjop_desc_t desc) noexcept {
    for (kobj_method_t *m = cls->methods; m && m->desc; ++m)
        if (m->desc == desc)
            return m;
    if (cls->baseclasses)
        for (kobj_class_t *b = cls->baseclasses; *b; ++b)
            if (kobj_method_t *m = find_method(*b, desc))
                return m;
    return nullptr;
}

void register_ids(kobj_class_t cls) noexcept {  // g_lock held
    for (kobj_method_t *m = cls->methods; m && m->desc; ++m)
        if (m->desc->id == 0)
            m->desc->id = g_next_id++;
    if (cls->baseclasses)
        for (kobj_class_t *b = cls->baseclasses; *b; ++b)
            register_ids(*b);
}

void fill(kobj_class_t cls, kobj_ops_t ops) noexcept {
    for (auto &e : ops->cache)
        e = &null_method;
    ops->cls = cls;
    register_ids(cls);
    __atomic_store_n(&cls->ops, ops, __ATOMIC_RELEASE);  // pairs with the unlocked check in compile()
}

// The allocator is user code (it may take its own locks, or call back into kobj), so it never runs under g_lock:
// allocate first, then publish under the lock; if another thread compiled the class meanwhile, free our block.
int compile(kobj_class_t cls, const holder &a) noexcept {
    if (__atomic_load_n(&cls->ops, __ATOMIC_ACQUIRE))
        return 0;
    void *p = alloc_with(a, sizeof(kobj_ops));
    if (!p)
        return ENOMEM;
    g_lock.lock();
    const bool won = !cls->ops;
    if (won)
        fill(cls, static_cast<kobj_ops_t>(p));
    g_lock.unlock();
    if (!won)
        free_block(p);
    return 0;
}

kobj_t create_with(kobj_class_t cls, const holder &a) noexcept {
    if (!cls || cls->size < sizeof(struct kobj) || compile(cls, a) != 0)
        return nullptr;
    void *p = alloc_with(a, cls->size);
    if (!p)
        return nullptr;
    kobj_t obj = static_cast<kobj_t>(p);
    obj->ops = cls->ops;
    __atomic_fetch_add(&cls->refs, 1u, __ATOMIC_RELAXED);
    return obj;
}

void unbind(kobj_t obj) noexcept {
    if (obj->ops) {
        __atomic_fetch_sub(&obj->ops->cls->refs, 1u, __ATOMIC_RELAXED);
        obj->ops = nullptr;
    }
}

}  // namespace

extern "C" {

#ifdef KIF_KOBJ_TRACE
void kobj_trace_set(const kobj_trace_sink *sink) { __atomic_store_n(&g_trace, sink, __ATOMIC_RELEASE); }

void kobj_trace_call(kobj_class_t cls, kobjop_desc_t desc, const char *op, kobjop_t func) {
    const kobj_trace_sink *sink = __atomic_load_n(&g_trace, __ATOMIC_ACQUIRE);
    if (sink && sink->fn) {
        const kobj_trace_event ev{cls, desc, op, func, func == desc->deflt.func};
        sink->fn(sink->ctx, &ev);
    }
}

#endif  // KIF_KOBJ_TRACE

int kobj_error_method(void) { return ENXIO; }

kobjop_t kobj_lookup_method(kobj_class_t cls, kobj_method_t **cep, kobjop_desc_t desc) {
    kobj_method_t *m = find_method(cls, desc);
    if (!m)
        m = &desc->deflt;  // the interface default (kobj_error_method when the .kif/.m has none)
    if (cep)
        __atomic_store_n(cep, m, __ATOMIC_RELAXED);
    return m->func;
}

int kobj_class_compile(kobj_class_t cls) { return compile(cls, from_ref(reloco::default_allocator())); }

int kif_kobj_class_compile(kobj_class_t cls, const kif_allocator *a) { return compile(cls, from_c(a)); }

void kobj_class_compile_static(kobj_class_t cls, kobj_ops_t ops) {
    g_lock.lock();
    if (!cls->ops)
        fill(cls, ops);
    g_lock.unlock();
}

void kobj_class_free(kobj_class_t cls) {
    void *dead = nullptr;
    g_lock.lock();
    if (cls->ops && __atomic_load_n(&cls->refs, __ATOMIC_RELAXED) == 0) {
        dead = cls->ops;  // only valid for caches made by kobj_class_compile (static ones are never freed here)
        cls->ops = nullptr;
    }
    g_lock.unlock();
    if (dead)
        free_block(dead);  // outside the lock: the allocator is user code
}

void kobj_init(kobj_t obj, kobj_class_t cls) {
    if (kobj_class_compile(cls) != 0)
        return;
    obj->ops = cls->ops;
    __atomic_fetch_add(&cls->refs, 1u, __ATOMIC_RELAXED);
}

void kobj_init_static(kobj_t obj, kobj_class_t cls) { kobj_init(obj, cls); }

kobj_t kobj_create(kobj_class_t cls, struct malloc_type *, int) {
    return create_with(cls, from_ref(reloco::default_allocator()));
}

kobj_t kif_kobj_create(kobj_class_t cls, const kif_allocator *a) { return create_with(cls, from_c(a)); }

void kobj_delete(kobj_t obj, struct malloc_type *) { kif_kobj_destroy(obj); }

void kobj_uninit(kobj_t obj) { unbind(obj); }

void kif_kobj_destroy(kobj_t obj) {
    if (!obj)
        return;
    unbind(obj);
    free_block(obj);
}

}  // extern "C"

namespace kif::kobj {

::kobj_t unsafe_create(::kobj_class_t cls, reloco::allocator_ref alloc) noexcept {
    return create_with(cls, from_ref(alloc));
}

int unsafe_class_compile(::kobj_class_t cls, reloco::allocator_ref alloc) noexcept {
    return compile(cls, from_ref(alloc));
}

void unsafe_destroy(::kobj_t obj) noexcept { kif_kobj_destroy(obj); }

}  // namespace kif::kobj

RELOCO_END_UNSAFE_CODE
