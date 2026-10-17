/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: owned_try_construct_at builds the object in caller memory, log_drop releases both vectors; plain has no user drop. */
#include <stdio.h>
#include "owned_proc.h"

int main(void)
{
	struct log l;
	struct plain p = PLAIN_INIT;
	int rc = log_try_construct_at(&l, 4);

	printf("%d\n", rc);
	printf("%d %d\n", log_add(&l, 3), log_add(&l, 4));
	printf("%u\n", (unsigned)log_total(&l));
	log_drop(&l);
	printf("%d %d %u\n", l.a.ptr == NULL, l.b.ptr == NULL, (unsigned)plain_count(&p));
	plain_drop(&p);
	{
		struct named nm;
		struct kif_strview t = { "abcd", 4 };

		printf("%d ", named_try_construct_at(&nm, t));
		printf("%u\n", (unsigned)named_size(&nm));
		named_drop(&nm);
		printf("%d\n", nm.label.ptr == NULL);
	}
	return 0;
}
