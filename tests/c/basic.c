/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: calls the lowered functions (basic_proc.h is the generated header; names are <stem>_<fn>). */
#include <stdio.h>
#include "basic_proc.h"

int main(void)
{
	printf("%u %u\n", (unsigned)basic_sum(5), (unsigned)basic_sum(0));
	printf("%llu\n", (unsigned long long)basic_fact(10));
	printf("%d %d %d\n", (int)basic_pick(mode_a, 9), (int)basic_pick(mode_b, 9), (int)basic_pick(mode_c, 9));
	printf("%u\n", (unsigned)basic_arr(2));
	return 0;
}
