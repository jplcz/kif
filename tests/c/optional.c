/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: optional<T> lowering. `bad()` panics and is deliberately not called. */
#include <stdio.h>
#include "optional_proc.h"

int main(void)
{
	size_t n = 0;
	int rc;
	bool bn = false;
	struct kif_opt_u32 a = optional_pick(0), b = optional_pick(5);

	printf("%d %d\n", a.has, b.has);
	printf("%u\n", (unsigned)b.v);
	printf("%u\n", (unsigned)optional_total(2, 0));
	printf("%u\n", (unsigned)optional_total(0, 0));
	printf("%u\n", (unsigned)optional_total(5, 5));
	printf("%d\n", (int)optional_sumpt(3));
	printf("%d\n", (int)optional_sumpt(0));
	rc = optional_named(true, &n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = optional_named(false, &n);
	printf("%d %u\n", rc, (unsigned)n);
	printf("%d %d\n", (int)optional_describe(5), (int)optional_describe(0));
	printf("%d %d\n", (int)optional_present(true), (int)optional_present(false));
	rc = optional_nonempty(true, &bn);
	printf("%d %d\n", rc, bn);
	rc = optional_nonempty(false, &bn);
	printf("%d %d\n", rc, bn);
	{
		uintptr_t al = 0;

		rc = optional_alloc_len(&al);
		printf("%d %u\n", rc, (unsigned)al);
	}
	{
		int32_t t = 0;

		rc = optional_frame_total(5, true, &t);
		printf("%d %d\n", rc, (int)t);
		rc = optional_frame_total(5, false, &t);
		printf("%d %d\n", rc, (int)t);
	}
	{
		uint32_t v = 0;

		rc = optional_tv(4, &v);
		printf("%d %u\n", rc, (unsigned)v);
		rc = optional_tv(0, &v);
		printf("%d\n", rc != 0);
	}
	return 0;
}
