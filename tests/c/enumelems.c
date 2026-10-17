/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: build() = 2 greens + 10 because element 1 is blue = 12; count over a plain C array. */
#include <stdio.h>
#include "enumelems_proc.h"

int main(void)
{
	enum color c[3] = { color_red, color_blue, color_blue };
	unsigned n = 0;
	int rc = enumelems_build(&n);

	printf("%d %u %u\n", rc, n, (unsigned)enumelems_count((struct kif_cspan_color){ c, 3 }, color_blue));
	return 0;
}
