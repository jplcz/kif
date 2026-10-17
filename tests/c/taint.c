/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: the taint feature's generated C must compute the same values as the source says (taint_proc.h is the generated header). */
#include <stdint.h>
#include <stdio.h>
#include "taint_proc.h"

int main(void)
{
	static const uint32_t a[4] = { 10, 0, 30, 40 };
	struct kif_cspan_u32 t = { a, 4 };
	printf("%u %u\n", (unsigned)taint_pick(1, 7, 9), (unsigned)taint_pick(0, 7, 9));
	printf("%d %d\n", (int)taint_same(5, 5), (int)taint_same(5, 6));
	printf("%d %d %d\n", (int)taint_less(-3, 2), (int)taint_less(2, -3), (int)taint_less(4, 4));
	printf("%u %u %u\n", (unsigned)taint_lookup(t, 2), (unsigned)taint_lookup(t, 6), (unsigned)taint_lookup(t, 7));
	printf("%u %u\n", (unsigned)taint_classify(t, 1), (unsigned)taint_classify(t, 3));
	return 0;
}
