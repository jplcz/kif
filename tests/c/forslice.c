/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: arrays({1,2,3,4}) = 2 + 6 + 8 + 8 = 24; vec(5) = 15 + 16 = 31; total = 3 + 4 = 7;
 * views({1,1,1,1}) = 8 + 8 = 16; vec_view = 9; at(1) = 4, at(9) fails. */
#include <stdio.h>
#include "forslice_proc.h"

int main(void)
{
	uint32_t a[4] = { 1, 2, 3, 4 };
	struct pt p[2] = { { 3 }, { 4 } };
	unsigned arrays_sum = forslice_arrays(a);
	unsigned vec_sum = 0;
	int rc = forslice_vec(5, &vec_sum);
	uint32_t b[4] = { 1, 1, 1, 1 };
	unsigned views = forslice_views(b);
	unsigned vv = 0;
	int rc2 = forslice_vec_view(&vv);
	unsigned at1 = 0, at9 = 0;
	int rc_at1 = forslice_at(1, &at1);
	int rc_at9 = forslice_at(9, &at9);
	unsigned total = forslice_total((struct kif_cspan_pt){ p, 2 });

	printf("%u %d %u %u\n", arrays_sum, rc, vec_sum, total);
	printf("%u %d %u\n", views, rc2, vv);
	printf("%d %u %d\n", rc_at1, at1, rc_at9 != 0);
	return 0;
}
