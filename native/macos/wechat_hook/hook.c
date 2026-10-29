/*
 * Interpose CommonCrypto inside the resigned official WeChat.app.
 *
 * Build on macOS: make
 * The library is loaded only into that copy, via DYLD_INSERT_LIBRARIES.
 * It records PBKDF and AES key material to a Unix socket or a 0600 file.
 * It does not read process memory, does not change SIP, and does not
 * touch the official WeChat.app.
 */

#include <CommonCrypto/CommonCryptor.h>
#include <CommonCrypto/CommonKeyDerivation.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define DYLD_INTERPOSE(_replacement, _replacee)                                \
  __attribute__((used)) static struct {                                        \
    const void *replacement;                                                   \
    const void *replacee;                                                      \
  } _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = {  \
      (const void *)(unsigned long)&_replacement,                              \
      (const void *)(unsigned long)&_replacee};

#define MAX_TRACKED 4096
#define MAX_WRITES 8192

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static uint64_t g_seen[MAX_TRACKED];
static size_t g_seen_count;
static int g_writes;
static int g_fd = -1;
static int g_ready;

static uint64_t fnv1a(const char *text) {
  uint64_t hash = 14695981039346656037ULL;
  for (const unsigned char *cursor = (const unsigned char *)text; *cursor; cursor++) {
    hash ^= *cursor;
    hash *= 1099511628211ULL;
  }
  return hash;
}

static int already_seen(uint64_t hash) {
  for (size_t index = 0; index < g_seen_count; index++) {
    if (g_seen[index] == hash)
      return 1;
  }
  if (g_seen_count < MAX_TRACKED)
    g_seen[g_seen_count++] = hash;
  return 0;
}

static void hex_encode(const uint8_t *in, size_t len, char *out) {
  static const char digits[] = "0123456789abcdef";
  for (size_t index = 0; index < len; index++) {
    out[index * 2] = digits[in[index] >> 4];
    out[index * 2 + 1] = digits[in[index] & 0x0f];
  }
  out[len * 2] = '\0';
}

static int open_output(void) {
  if (g_ready)
    return g_fd;
  g_ready = 1;
  const char *socket_path = getenv("NOTEFORD_CAPTURE_SOCKET");
  if (socket_path && socket_path[0]) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd >= 0) {
      struct sockaddr_un address;
      memset(&address, 0, sizeof(address));
      address.sun_family = AF_UNIX;
      if (strlen(socket_path) < sizeof(address.sun_path)) {
        memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
        if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) {
          g_fd = fd;
          return g_fd;
        }
      }
      close(fd);
    }
  }
  const char *file_path = getenv("NOTEFORD_CAPTURE_PATH");
  if (file_path && file_path[0]) {
    int fd = open(file_path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0)
      g_fd = fd;
  }
  return g_fd;
}

static void emit_line(const char *line) {
  pthread_mutex_lock(&g_mu);
  if (g_writes >= MAX_WRITES || already_seen(fnv1a(line))) {
    pthread_mutex_unlock(&g_mu);
    return;
  }
  int fd = open_output();
  if (fd >= 0) {
    size_t length = strlen(line);
    if (write(fd, line, length) == (ssize_t)length && write(fd, "\n", 1) == 1)
      g_writes++;
  }
  pthread_mutex_unlock(&g_mu);
}

typedef int (*pbkdf_fn)(CCPBKDFAlgorithm, const char *, size_t, const uint8_t *,
                        size_t, CCPseudoRandomAlgorithm, unsigned int, uint8_t *,
                        size_t);
typedef CCCryptorStatus (*cryptor_fn)(CCOperation, CCAlgorithm, CCOptions,
                                      const void *, size_t, const void *,
                                      CCCryptorRef *);

static pbkdf_fn g_real_pbkdf;
static cryptor_fn g_real_cryptor;

static void resolve_pbkdf(void) {
  g_real_pbkdf = (pbkdf_fn)dlsym(RTLD_NEXT, "CCKeyDerivationPBKDF");
}

static void resolve_cryptor(void) {
  g_real_cryptor = (cryptor_fn)dlsym(RTLD_NEXT, "CCCryptorCreate");
}

static pbkdf_fn real_pbkdf(void) {
  static pthread_once_t once = PTHREAD_ONCE_INIT;
  pthread_once(&once, resolve_pbkdf);
  return g_real_pbkdf;
}

static cryptor_fn real_cryptor(void) {
  static pthread_once_t once = PTHREAD_ONCE_INIT;
  pthread_once(&once, resolve_cryptor);
  return g_real_cryptor;
}

static int hooked_pbkdf(CCPBKDFAlgorithm algorithm, const char *password,
                        size_t passwordLen, const uint8_t *salt, size_t saltLen,
                        CCPseudoRandomAlgorithm prf, unsigned int rounds,
                        uint8_t *derivedKey, size_t derivedKeyLen) {
  pbkdf_fn original = real_pbkdf();
  if (!original)
    return -1;
  int status = original(algorithm, password, passwordLen, salt, saltLen, prf,
                        rounds, derivedKey, derivedKeyLen);
  if (status == 0 && password && salt && derivedKey && passwordLen == 32 &&
      saltLen == 16 && derivedKeyLen == 32) {
    char password_hex[65], salt_hex[33], derived_hex[65], line[384];
    hex_encode((const uint8_t *)password, 32, password_hex);
    hex_encode(salt, 16, salt_hex);
    hex_encode(derivedKey, 32, derived_hex);
    snprintf(line, sizeof(line),
             "{\"v\":1,\"kind\":\"pbkdf\",\"algorithm\":%u,\"prf\":%u,"
             "\"rounds\":%u,\"password\":\"%s\",\"salt\":\"%s\",\"derived\":\"%s\"}",
             (unsigned)algorithm, (unsigned)prf, rounds, password_hex, salt_hex,
             derived_hex);
    emit_line(line);
  }
  return status;
}

static CCCryptorStatus hooked_cryptor(CCOperation op, CCAlgorithm alg,
                                      CCOptions options, const void *key,
                                      size_t keyLength, const void *iv,
                                      CCCryptorRef *cryptorRef) {
  cryptor_fn original = real_cryptor();
  if (!original)
    return kCCUnimplemented;
  CCCryptorStatus status =
      original(op, alg, options, key, keyLength, iv, cryptorRef);
  if (status == kCCSuccess && key && (keyLength == 16 || keyLength == 32) &&
      alg == kCCAlgorithmAES) {
    char key_hex[65], line[220];
    hex_encode(key, keyLength, key_hex);
    snprintf(line, sizeof(line),
             "{\"v\":1,\"kind\":\"aes\",\"op\":%u,\"algorithm\":%u,\"options\":%u,"
             "\"key_len\":%u,\"key\":\"%s\"}",
             (unsigned)op, (unsigned)alg, (unsigned)options, (unsigned)keyLength,
             key_hex);
    emit_line(line);
  }
  return status;
}

DYLD_INTERPOSE(hooked_pbkdf, CCKeyDerivationPBKDF)
DYLD_INTERPOSE(hooked_cryptor, CCCryptorCreate)
