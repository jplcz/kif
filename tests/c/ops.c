/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: `#[ops]` handles (`&mut dyn counter`) over two implementers, a default body and a typed local handle. */
#include <stdio.h>
#include "ops_proc.h"

int main(void)
{
	printf("%u\n", (unsigned)ops_simple_total());
	printf("%u\n", (unsigned)ops_doubler_total());
	printf("%u\n", (unsigned)ops_twice_total());
	return 0;
}
