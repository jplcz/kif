/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: chain = (5 + 1) + 7 + len("n") = 14. */
#include <stdio.h>
#include "heap_proc.h"

int main(void)
{
	unsigned n = 0;
	int rc = heap_chain(&n);

	printf("%d %u\n", rc, n);
	return 0;
}
