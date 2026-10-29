/* Tiny target for mac_lldb_worker --self-test. Calls CCKeyDerivationPBKDF once. */
#include <CommonCrypto/CommonKeyDerivation.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    uint8_t out[32];
    uint8_t salt[16];
    memset(salt, 0x3a, sizeof(salt));
    memset(out, 0, sizeof(out));
    int rc = CCKeyDerivationPBKDF(
        kCCPBKDF2,
        "noteford-probe",
        14,
        salt,
        sizeof(salt),
        kCCPRFHmacAlgSHA512,
        2,
        out,
        sizeof(out));
    /* Touch result so it is not optimized away; do not print key bytes. */
    volatile uint8_t sink = 0;
    for (size_t i = 0; i < sizeof(out); i++) {
        sink ^= out[i];
    }
    printf("probe_target_done rc=%d sink=%u\n", rc, (unsigned)sink);
    return rc == 0 ? 0 : 1;
}
