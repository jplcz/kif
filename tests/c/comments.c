/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: the output file shows the lowered comments (tests/c/out/comments/comments_proc.{h,c}); this checks the code still runs. */
#include <stdio.h>
#include "comments_proc.h"

int main(void)
{
	printf("%u\n", (unsigned)total(10, 3));
	printf("%u %u\n", (unsigned)comments_pick(7, 2), (unsigned)comments_pick(1, 9));
	return 0;
}
