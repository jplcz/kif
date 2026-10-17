/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: greet("world") builds ">> hello world", checks it, erases the prefix and returns 11 + 11. */
#include <stdio.h>
#include "str_proc.h"

int main(void)
{
	struct kif_strview v = { "world", 5 };
	size_t n = 0;
	int rc = str_greet(v, &n);

	printf("%d %u\n", rc, (unsigned)n);
	return 0;
}
