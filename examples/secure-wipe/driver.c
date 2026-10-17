/* Driver for demo.kif: calls the exported functions and prints, so the generated C is compiled and run like tests/c cases. */
#include <stdio.h>
#include "demo_proc.h"

int main(void)
{
	static const uint8_t data[3] = { 1, 2, 3 };
	struct kif_cspan_u8 span = { data, 3 };

	printf("checksum=%u\n", (unsigned)checksum(span));
	printf("derive=%llu\n", (unsigned long long)derive(5));
	printf("mix=%llu\n", (unsigned long long)mix(0xF0, 0x0F));
	printf("scratch=%u\n", (unsigned)scratch());
	return 0;
}
