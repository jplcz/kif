/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: runs every function with success and with failing arguments (>9 fails); ASan reports a leak or a double free. */
#include <stdio.h>
#include "tempdrop_proc.h"

int main(void)
{
	size_t r;
	unsigned i, j;

	for (i = 2; i <= 11; i += 9)
		for (j = 2; j <= 11; j += 9) {
			struct pair p;
			int x1 = tempdrop_lit(i, j, &p);
			int x2, x3, x4, x5, x6, x7;

			if (x1 == 0)
				pair_drop(&p);
			x2 = tempdrop_lit2(i, j, &r);
			x3 = tempdrop_user(i, j, &r);
			x4 = tempdrop_cond(i, j, &r);
			x5 = tempdrop_args(i, j, &r);
			x6 = tempdrop_recv(i, j, &r);
			x7 = tempdrop_discard(i);
			printf("%d %d %d %d %d %d %d\n", x1, x2, x3, x4, x5, x6, x7);
		}
	printf("%d\n", tempdrop_wh(3, &r));
	return 0;
}
