/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: the target allocator is replaced by one that remembers each block's size, so every free can check that the block was wiped first. */
#include <stdio.h>
#include <stdlib.h>

static size_t frees, unwiped;

static void *tracked_malloc(size_t n)
{
	size_t *p = malloc(n + 16);

	p[0] = n;
	return p + 2;
}

static void tracked_free(void *q)
{
	size_t *p = (size_t *)q - 2;
	const unsigned char *b = q;

	for (size_t i = 0; i < p[0]; i++)
		if (b[i] != 0) {
			unwiped++;
			break;
		}
	frees++;
	free(p);
}

#define malloc(n) tracked_malloc(n)
#define free(p) tracked_free(p)
#include "securevec_proc.h"
#undef malloc
#undef free

int main(void)
{
	uint32_t t = 0;
	size_t n = 0;
	int rc;
	struct kif_svec_u32 v;
	uint32_t *old;

	rc = securevec_grow(&t);
	printf("%d %u\n", rc, (unsigned)t);
	rc = securevec_shrink(&n);
	printf("%d %u\n", rc, (unsigned)n);

	/* the helpers are in the header, so this file's allocator is the one they use: growth wipes the old block, pop and clear wipe the elements */
	rc = kif_svec_u32_try_create(&v, 2);
	for (uint32_t i = 0; i < 5; i++)
		rc |= kif_svec_u32_try_push(&v, 0xAAAAAAAAu);
	printf("grown %d, %u wiped blocks freed, %u unwiped\n", rc, (unsigned)frees, (unsigned)unwiped);
	old = v.ptr;
	rc = kif_svec_u32_try_pop(&v);
	printf("pop %d, vacated %u\n", rc, (unsigned)old[4]);
	rc = kif_svec_u32_try_erase_at(&v, 0);
	printf("erase %d, vacated %u\n", rc, (unsigned)old[3]);
	rc = kif_svec_u32_try_resize(&v, 1);
	printf("shrink %d, vacated %u %u\n", rc, (unsigned)old[1], (unsigned)old[2]);
	kif_svec_u32_clear(&v);
	printf("clear, vacated %u\n", (unsigned)old[0]);
	kif_svec_u32_drop(&v);
	printf("dropped, freed %u blocks, %u unwiped\n", (unsigned)frees, (unsigned)unwiped);
	return 0;
}
