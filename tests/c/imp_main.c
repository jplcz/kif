/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: imp_main_proc.h includes imp_lib_proc.h; the library's names keep its stem (imp_lib_twice, IMP_LIB_LIMIT). */
#include <stdio.h>
#include "imp_main_proc.h"

int main(void)
{
	struct point p = { 3, 4 };
	uintptr_t n = 0;
	int rc;

	printf("%d\n", (int)imp_main_area(p));
	rc = imp_main_fill(5, &n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = imp_main_heap_sum(7, &n);
	printf("%d %u\n", rc, (unsigned)n);
	printf("%u\n", (unsigned)imp_main_dyn_total());
	return 0;
}
