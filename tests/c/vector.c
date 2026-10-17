/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: a vector is `struct kif_vec_<T> { ptr, len, cap }`; its operations return 0 or -errno. */
#include <stdio.h>
#include <stdlib.h>
#include "vector_proc.h"

int main(void)
{
	uint32_t t = 0;
	size_t n = 0;
	int32_t r = 0;
	int rc;

	rc = vector_fill(4, &t);
	printf("%d %u\n", rc, (unsigned)t);
	rc = vector_grow(&n);
	printf("%d %u\n", rc, (unsigned)n);
	rc = vector_pts(&r);
	printf("%d %d\n", rc, (int)r);
	printf("%d\n", vector_bad() == -ENOENT);
	return 0;
}
