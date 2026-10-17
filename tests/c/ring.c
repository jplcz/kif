/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: methods are <Struct>_<name>; -ENOMEM is allocation_failed, -EINVAL is invalid_argument. */
#include <stdio.h>
#include "ring_proc.h"

int main(void)
{
	uint32_t v = 0;
	int rc;
	struct ring slot, z = RING_INIT;

	rc = ring_demo(3, &v);
	printf("%d %u\n", rc, (unsigned)v);
	printf("%d\n", ring_demo(2, &v) == -ENOMEM);
	printf("%d\n", ring_demo(9, &v) == -EINVAL);
	rc = ring_try_construct_at(&slot, 4);
	printf("%d %u %u\n", rc, (unsigned)slot.cap, (unsigned)slot.count);
	printf("%u\n", (unsigned)z.cap);
	printf("%u\n", (unsigned)ring_take(&z));
	return 0;
}
