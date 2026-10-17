/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: a result function returns 0 or -errno (this target: negative style) and stores its value through the last argument. */
#include <stdio.h>
#include "results_proc.h"

int main(void)
{
	uint32_t v = 0;
	int rc;

	printf("%u %u\n", (unsigned)results_f(2), (unsigned)results_f(9));
	printf("%u %u\n", (unsigned)results_h(2), (unsigned)results_h(9));
	rc = results_chain(1, &v);
	printf("%d %u\n", rc, (unsigned)v);
	rc = results_chain(3, &v);
	printf("%d\n", rc == -EBUSY);
	rc = results_pass(2, &v);
	printf("%d %u\n", rc, (unsigned)v);
	printf("%u %u\n", (unsigned)results_which(1), (unsigned)results_which(7));
	return 0;
}
