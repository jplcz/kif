/*
 * SPDX-FileCopyrightText: 2026 Jaroslaw Pelczar / Project KIF
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later WITH LicenseRef-Kif-Output-Exception
 */

/* Driver: hosted TLS cells. The main thread's block starts from the template image; a second thread gets its own copy. */
#include <pthread.h>
#include <stdio.h>
#include "cells_proc.h"

static void *worker(void *arg)
{
	(void)arg;
	unsigned s = cells_start();
	unsigned b = cells_bump();

	printf("worker %u %u\n", s, b);
	return NULL;
}

int main(void)
{
	pthread_t t;
	unsigned first = cells_bump();
	unsigned second = cells_bump();
	unsigned st = cells_start();

	printf("%u %u %u\n", first, second, st);
	int flipped = cells_flip();
	unsigned h1 = cells_hit();
	unsigned h2 = cells_hit();

	printf("%d %d\n", flipped, cells_flag());
	printf("%u %u\n", h1, h2);
	pthread_create(&t, NULL, worker, NULL);
	pthread_join(t, NULL);
	printf("%u\n", (unsigned)cells_bump());
	return 0;
}
