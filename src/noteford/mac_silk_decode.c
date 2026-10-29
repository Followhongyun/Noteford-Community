/* Noteford's bounded command-line adapter for the Skype SILK SDK. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "SKP_Silk_SDK_API.h"

/* The SDK's assertion hook must terminate only this isolated decoder. */
void SKP_assert(int condition) {
    if (!condition) exit(10);
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    FILE *input = fopen(argv[1], "rb");
    if (!input) return 3;
    int first = fgetc(input);
    if (first != 2 && first != EOF) ungetc(first, input);
    char magic[9];
    if (fread(magic, 1, 9, input) != 9 || memcmp(magic, "#!SILK_V3", 9)) {
        fclose(input); return 4;
    }
    SKP_int32 state_size = 0;
    if (SKP_Silk_SDK_Get_Decoder_Size(&state_size) || state_size <= 0) {
        fclose(input); return 5;
    }
    void *state = calloc(1, (size_t)state_size);
    if (!state || SKP_Silk_SDK_InitDecoder(state)) {
        free(state); fclose(input); return 5;
    }
    FILE *output = fopen(argv[2], "wb");
    if (!output) { free(state); fclose(input); return 3; }
    SKP_SILK_SDK_DecControlStruct control;
    memset(&control, 0, sizeof(control));
    control.API_sampleRate = 24000;
    unsigned char packet[8192], length[2];
    SKP_int16 samples[4800];
    size_t written = 0;
    int result = 0;
    for (;;) {
        size_t got = fread(length, 1, 2, input);
        if (!got && feof(input)) break;
        if (got != 2) { result = 6; break; }
        unsigned size = length[0] | ((unsigned)length[1] << 8);
        if (size == 65535) break;
        if (!size || size > sizeof(packet) || fread(packet, 1, size, input) != size) {
            result = 6; break;
        }
        int frames = 0;
        do {
            SKP_int16 count = 4800;
            if (++frames > 5 || SKP_Silk_SDK_Decode(state, &control, 0, packet,
                                                  (SKP_int)size, samples, &count)
                || count <= 0 || count > 4800) { result = 7; break; }
            written += (size_t)count * 2;
            if (written > 64 * 1024 * 1024 ||
                fwrite(samples, 2, (size_t)count, output) != (size_t)count) {
                result = 8; break;
            }
        } while (control.moreInternalDecoderFrames);
        if (result) break;
    }
    if (!written && !result) result = 9;
    if (ferror(input)) result = 8;
    if (fclose(output)) result = 8;
    fclose(input); free(state);
    return result;
}
