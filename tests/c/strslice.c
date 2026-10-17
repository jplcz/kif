/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: total over a C array of kif_str built with kif_str_try_create (2 + 3 = 5), first = 2. */
#include <stdio.h>
#include "strslice_proc.h"

int main(void)
{
	struct kif_str a[2];
	int rc;

	rc = kif_str_try_create(&a[0], (struct kif_strview){ "ab", 2 });
	rc |= kif_str_try_create(&a[1], (struct kif_strview){ "cde", 3 });
	printf("%d %u %u\n", rc, (unsigned)strslice_total((struct kif_cspan_str){ a, 2 }),
	    (unsigned)strslice_first((struct kif_cspan_str){ a, 2 }));
	kif_str_drop(&a[0]);
	kif_str_drop(&a[1]);
	return 0;
}
