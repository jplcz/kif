/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: contracts_proc.h is the generated header; names are <stem>_<fn>. */
#include <stdio.h>
#include "contracts_proc.h"

int main(void)
{
	uint32_t a[3] = { 7, 8, 9 };
	struct kif_span_u32 s = { a, 3 };
	printf("%u %u\n", (unsigned)contracts_use_pick(9), (unsigned)contracts_use_pick(1));
	printf("%u\n", (unsigned)contracts_use_liar(5));
	printf("%u\n", (unsigned)contracts_use_head((struct kif_cspan_u32){ a, 3 }));
	unsigned r = contracts_bump(s);
	printf("%u %u\n", r, (unsigned)a[0]);
	return 0;
}
