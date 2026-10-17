/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: demo = 9*100 + 5*10 + 9 + 1000; points = 12; from_vec = 30; out_of_range(3) would trap, so only n = 2 runs. */
#include <stdio.h>
#include "span_proc.h"

int main(void)
{
	unsigned n = 0;
	int rc = span_from_vec(&n);

	printf("%u %u\n", (unsigned)span_demo(), (unsigned)span_points());
	printf("%d %u %u\n", rc, n, (unsigned)span_out_of_range(2));
	return 0;
}
