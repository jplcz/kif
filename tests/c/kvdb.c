/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: the numbers come from the Python model in tests/c/kvdb_model.py (an independent dict-based model of the same workload). */
#include <stdio.h>
#include "kvdb_proc.h"

int main(void)
{
	static const unsigned sizes[] = {0, 1, 50, 2000, 20000};
	uint64_t u64;
	uint32_t u32;
	uintptr_t us;

	for (unsigned i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
		unsigned n = sizes[i];
		int rc = 0;
		rc |= kvdb_checksum_of(n, &u64);
		printf("n=%u checksum=%llu", n, (unsigned long long)u64);
		rc |= kvdb_errors_of(n, &u32);
		printf(" errors=%u", (unsigned)u32);
		rc |= kvdb_live_of(n, &us);
		printf(" live=%u", (unsigned)us);
		rc |= kvdb_slots_of(n, &us);
		printf(" slots=%u", (unsigned)us);
		rc |= kvdb_sum_of(n, &u64);
		printf(" sum=%llu rc=%d\n", (unsigned long long)u64, rc);
	}
	/* the starting size changes the table's shape, never the answers */
	for (unsigned s = 1; s <= 1024; s *= 4) {
		int rc = kvdb_checksum_from(2000, s, &u64);
		printf("start=%u checksum=%llu rc=%d\n", s, (unsigned long long)u64, rc);
	}
	return 0;
}
