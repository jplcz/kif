/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: only calls the functions; every release happens inside the generated code. ASan fails the test on a leak or a double free. */
#include <stdio.h>
#include "autodrop_proc.h"

int main(void)
{
	size_t n = 0;
	int rc;

	rc = autodrop_early(0, &n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = autodrop_early(1, &n);
	printf("%d\n", rc);
	rc = autodrop_handoff(&n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = autodrop_reassign(&n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = autodrop_looped(&n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = autodrop_heap(&n);
	printf("%d %u\n", rc, (unsigned)n);
	return 0;
}
