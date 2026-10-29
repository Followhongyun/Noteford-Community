/*
 * wechat_key_hook.c — DYLD __DATA,__interpose hook for WeChat Mac key capture.
 *
 * MAIN ROUTE (wired into mac_capture via DYLD_INSERT_LIBRARIES). Build:
 *
 *   clang -arch arm64 -dynamiclib -O2 \
 *     native/macos/key_hook/wechat_key_hook.c -o libwechat_key_hook.dylib \
 *     -framework CoreFoundation -framework Security
 *
 *   WECHAT_KEY_HOOK_OUT=/path/candidates.jsonl \
 *   DYLD_INSERT_LIBRARIES=/path/libwechat_key_hook.dylib \
 *     <copy>/Contents/MacOS/WeChat
 *
 * Why interpose over LLDB attach: wechat-cli.com (4.1.13, 27/29 libs) shows the
 * hook must be IN PLACE before the first crypto call. Interpose via
 * __DATA,__interpose is live from image load.
 *
 * ANTI-TAMPER NEUTRALISATION (X1): static analysis of 4.1.15 (build 270135)
 * showed the self-check that SIGKILLs an ad-hoc-resigned copy (exit 255) is built
 * ENTIRELY on public Security.framework calls:
 *   - libwxld.dylib (loader, runs first): SecCodeCopySelf ->
 *     SecCodeCopySigningInformation -> CFDictionaryGetValue(kSecCodeInfoTeamIdentifier)
 *     compared against the embedded Tencent TeamID "5A4RE8SF68". Ad-hoc resign
 *     empties the TeamID -> mismatch -> __exit/abort. This is the fast killer.
 *   - wechat.dylib (deeper): SecStaticCodeCreateWithPath +
 *     SecStaticCodeCheckValidityWithErrors against a requirement string.
 * No csops / no self-SHA256 of own Mach-O were found, so these public functions
 * are the whole check and can be neutralised by interposing them — no binary
 * patch, no SIP off, and it tracks the Apple API across WeChat upgrades.
 *
 * We interpose exactly two functions:
 *   1) SecCodeCopySigningInformation: call the real one, then force the returned
 *      dict's kSecCodeInfoTeamIdentifier to "5A4RE8SF68" (one key only).
 *   2) SecStaticCodeCheckValidityWithErrors: always errSecSuccess, *errors=NULL.
 * We deliberately DO NOT touch SecTrustEvaluate / SecTrust* — those are TLS /
 * certificate validation; interposing them would break WeChat's network/login.
 *
 * Recursion note: dyld does NOT apply an __interpose to references made from the
 * image that declares it, so calling SecCodeCopySigningInformation /
 * CCKeyDerivationPBKDF below reaches the real implementation (no self-recursion),
 * same as the existing CommonCrypto hooks.
 *
 * SECURITY: the copy still uses our merge-式重签 (keep WeChat's original
 * entitlements, add get-task-allow + cs.allow-dyld-environment-variables +
 * cs.disable-library-validation). We never resign /Applications/WeChat.app.
 */

#include <CommonCrypto/CommonCrypto.h>
#include <CommonCrypto/CommonKeyDerivation.h>
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include <unistd.h>
#include <execinfo.h>
#include <dlfcn.h>
#include <sys/wait.h>
#include <sys/file.h>
#include <sys/types.h>
#include <spawn.h>

#define NF_DK_LEN 32
/* Only record PBKDF input when the call looks like a SQLCipher key derivation:
 * a large round count or a 32-byte derived key. Keeps noise out of the file. */
#define NF_MIN_ROUNDS 1000

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_fd = -1;

static void nf_open_once(void) {
    if (g_fd >= 0) return;
    const char *path = getenv("WECHAT_KEY_HOOK_OUT");
    if (!path || !*path) return;
    g_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
}

static void nf_write_line(const char *line, size_t len) {
    pthread_mutex_lock(&g_lock);
    nf_open_once();
    if (g_fd >= 0) (void)!write(g_fd, line, len);
    pthread_mutex_unlock(&g_lock);
}

static void nf_hex(const uint8_t *buf, size_t n, char *out) {
    static const char *H = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[i * 2] = H[(buf[i] >> 4) & 0xF];
        out[i * 2 + 1] = H[buf[i] & 0xF];
    }
    out[n * 2] = '\0';
}

/* Append one JSON line. No printf to stdout — avoid leaking into any log. */
static void nf_emit_hex(const char *kind, const uint8_t *buf, size_t n,
                        unsigned long rounds, const uint8_t *salt, size_t saltLen) {
    if (!buf || n == 0 || n > 64) return;
    char hex[129];
    nf_hex(buf, n, hex);
    char salt_hex[65];
    salt_hex[0] = '\0';
    if (salt && saltLen > 0 && saltLen <= 32) nf_hex(salt, saltLen, salt_hex);
    char line[480];
    int len = snprintf(line, sizeof(line),
                       "{\"v\":1,\"kind\":\"%s\",\"len\":%zu,\"rounds\":%lu,"
                       "\"salt\":\"%s\",\"hex\":\"%s\"}\n",
                       kind, n, rounds, salt_hex, hex);
    if (len > 0) nf_write_line(line, (size_t)len);
}

/* Diagnostic event line (anti-tamper interpose hits). Timestamped, no secrets. */
static void nf_emit_event(const char *kind, const char *detail) {
    char line[256];
    int len = snprintf(line, sizeof(line),
                       "{\"v\":1,\"kind\":\"%s\",\"detail\":\"%s\",\"ts\":%ld,\"pid\":%d}\n",
                       kind, detail ? detail : "", (long)time(NULL), (int)getpid());
    if (len > 0) {
        nf_write_line(line, (size_t)len);
        /* X1b diagnostic: mirror NON-SECRET events to stderr so we can confirm
         * hook load / interpose hits even if the sandbox blocks the log file.
         * Secret material (nf_emit_hex) is NEVER mirrored. */
        (void)!write(2, line, (size_t)len);
    }
}

/* forward decl: defined in the X1c re-injection block below */
static void nf_capture_self_paths(void);

/* ---- load marker: proves the hook image was actually inserted ---- */
__attribute__((constructor)) static void nf_on_load(void) {
    nf_capture_self_paths();
    char line[128];
    int len = snprintf(line, sizeof(line),
                       "{\"v\":1,\"kind\":\"hook_loaded\",\"pid\":%d}\n",
                       (int)getpid());
    if (len > 0) {
        nf_write_line(line, (size_t)len);
        (void)!write(2, line, (size_t)len);
    }
}

/* ---- CCKeyDerivationPBKDF: capture the INPUT password (= shared passphrase) ---- */
static int nf_CCKeyDerivationPBKDF(CCPBKDFAlgorithm algorithm, const char *password,
                                   size_t passwordLen, const uint8_t *salt,
                                   size_t saltLen, CCPseudoRandomAlgorithm prf,
                                   unsigned rounds, uint8_t *derivedKey,
                                   size_t derivedKeyLen) {
    int interesting = (rounds >= NF_MIN_ROUNDS) || (derivedKeyLen == NF_DK_LEN);
    if (interesting && password && passwordLen > 0 && passwordLen <= 64) {
        nf_emit_hex("pbkdf_in", (const uint8_t *)password, passwordLen, rounds,
                    salt, saltLen);
    }
    int rc = CCKeyDerivationPBKDF(algorithm, password, passwordLen, salt, saltLen,
                                  prf, rounds, derivedKey, derivedKeyLen);
    if (rc == kCCSuccess && interesting && derivedKey && derivedKeyLen == NF_DK_LEN) {
        nf_emit_hex("pbkdf_out", derivedKey, derivedKeyLen, rounds, salt, saltLen);
    }
    return rc;
}

/* ---- CCCryptorCreate: capture the 32B AES key (per-db enc_key) — double insurance ---- */
static CCCryptorStatus nf_CCCryptorCreate(CCOperation op, CCAlgorithm alg,
                                          CCOptions options, const void *key,
                                          size_t keyLength, const void *iv,
                                          CCCryptorRef *cryptorRef) {
    if (key && keyLength == NF_DK_LEN && alg == kCCAlgorithmAES) {
        nf_emit_hex("aes_key", (const uint8_t *)key, keyLength, 0, NULL, 0);
    }
    return CCCryptorCreate(op, alg, options, key, keyLength, iv, cryptorRef);
}

/* ---- Anti-tamper neutralisation #1: force self TeamIdentifier = Tencent's ----
 * libwxld's self-check reads kSecCodeInfoTeamIdentifier from this dict and
 * compares it to "5A4RE8SF68". After an ad-hoc resign the real dict has no (or a
 * different) TeamID, so we overwrite just that one key and hand back the rest
 * unchanged. Everything else in the signing-info dict is preserved. */
static int g_sig_logged = 0;
static OSStatus nf_SecCodeCopySigningInformation(SecStaticCodeRef code,
                                                  SecCSFlags flags,
                                                  CFDictionaryRef *information) {
    OSStatus rc = SecCodeCopySigningInformation(code, flags, information);
    if (rc == errSecSuccess && information && *information) {
        CFMutableDictionaryRef m =
            CFDictionaryCreateMutableCopy(NULL, 0, *information);
        if (m) {
            CFDictionarySetValue(m, kSecCodeInfoTeamIdentifier, CFSTR("5A4RE8SF68"));
            CFRelease(*information);
            *information = m; /* ownership passes to caller, which CFReleases it */
            if (g_sig_logged < 8) {
                nf_emit_event("anti_tamper", "SecCodeCopySigningInformation:teamid_forced");
                g_sig_logged++;
            }
        }
    } else if (g_sig_logged < 8) {
        nf_emit_event("anti_tamper", "SecCodeCopySigningInformation:passthrough");
        g_sig_logged++;
    }
    return rc;
}

/* ---- Anti-tamper neutralisation #2: deeper static code validity check ----
 * wechat.dylib uses SecStaticCodeCreateWithPath + SecStaticCodeCheckValidityWithErrors
 * against a requirement to re-validate code on disk. Force success. This is code
 * signing validation only; TLS/cert checks go through SecTrust* which we leave
 * alone so WeChat's network/login keep working. */
static int g_chk_logged = 0;
static OSStatus nf_SecStaticCodeCheckValidityWithErrors(SecStaticCodeRef staticCode,
                                                        SecCSFlags flags,
                                                        SecRequirementRef requirement,
                                                        CFErrorRef *errors) {
    if (errors) *errors = NULL;
    if (g_chk_logged < 8) {
        nf_emit_event("anti_tamper", "SecStaticCodeCheckValidityWithErrors:forced_success");
        g_chk_logged++;
    }
    return errSecSuccess;
}

/* ---- DIAGNOSTIC (X1 only): trace voluntary exit/abort to find the killer ----
 * exit 255 is a voluntary exit (not a signal), so whatever decides to kill the
 * resigned copy calls exit/_exit/abort. We log a backtrace (module+offset) right
 * before handing off to the real function, which reveals whether the kill comes
 * from libwxld, wechat.dylib, or elsewhere. No recursion: dyld does not interpose
 * references made from this image. */
static void nf_log_backtrace(const char *which, int code) {
    void *frames[64];
    int n = backtrace(frames, 64);
    char line[4000];
    size_t off = 0;
    off += (size_t)snprintf(line + off, sizeof(line) - off,
        "{\"v\":1,\"kind\":\"exit_trace\",\"via\":\"%s\",\"code\":%d,\"pid\":%d,\"frames\":[",
        which, code, (int)getpid());
    for (int i = 0; i < n && off < sizeof(line) - 160; i++) {
        Dl_info info;
        const char *name = "?";
        long delta = 0;
        if (dladdr(frames[i], &info) && info.dli_fname) {
            name = info.dli_fname;
            const char *b = strrchr(name, '/');
            if (b) name = b + 1;
            if (info.dli_saddr) delta = (long)((char *)frames[i] - (char *)info.dli_saddr);
        }
        off += (size_t)snprintf(line + off, sizeof(line) - off,
            "%s\"%s+%ld\"", i ? "," : "", name, delta);
    }
    off += (size_t)snprintf(line + off, sizeof(line) - off, "]}\n");
    nf_write_line(line, off);
}

static void nf_exit(int code) { nf_log_backtrace("exit", code); exit(code); }
static void nf__exit(int code) { nf_log_backtrace("_exit", code); _exit(code); }
static void nf_abort(void) { nf_log_backtrace("abort", -1); abort(); }

/* ---- DIAGNOSTIC (X1 only): launcher/single-instance probes (pass-through) ----
 * libwxld forks/spawns a worker and uses flock + bootstrap_look_up for a
 * single-instance guard. With the official WeChat running, these can make the
 * launcher's main() return -1 for reasons unrelated to anti-tamper. We log only;
 * we never change their behaviour. */
extern int bootstrap_look_up(unsigned int bp, const char *name, unsigned int *sp);

static int nf_bootstrap_look_up(unsigned int bp, const char *name, unsigned int *sp) {
    int rc = bootstrap_look_up(bp, name, sp);
    char detail[220];
    snprintf(detail, sizeof(detail), "bootstrap_look_up name=%s rc=%d", name ? name : "", rc);
    nf_emit_event("instance_probe", detail);
    return rc;
}

static pid_t nf_waitpid(pid_t pid, int *status, int options) {
    pid_t r = waitpid(pid, status, options);
    if (r > 0 && status) {
        char detail[200];
        int exited = WIFEXITED(*status);
        int code = exited ? WEXITSTATUS(*status) : -1;
        int sig = WIFSIGNALED(*status) ? WTERMSIG(*status) : 0;
        snprintf(detail, sizeof(detail),
                 "waitpid child=%d exited=%d code=%d signal=%d", (int)r, exited, code, sig);
        nf_emit_event("instance_probe", detail);
    }
    return r;
}

static int nf_flock(int fd, int operation) {
    int rc = flock(fd, operation);
    char detail[120];
    snprintf(detail, sizeof(detail), "flock op=%d rc=%d errno=%d", operation, rc, rc ? errno : 0);
    nf_emit_event("instance_probe", detail);
    return rc;
}

/* ---- X1c: re-inject DYLD_INSERT across the launcher's self-relaunch ----
 * Finding (X1b): libwxld honours our DYLD_INSERT on the initial exec, but when
 * the copy becomes the primary instance it re-execs / posix_spawns the real GUI
 * process with DYLD_* scrubbed from the environment, so the process that performs
 * the login CCKeyDerivationPBKDF has no hook (observed: surviving GUI keeps
 * WECHAT_KEY_HOOK_OUT but loses DYLD_INSERT_LIBRARIES; dylib unmapped). We counter
 * by interposing execve/posix_spawn(p) and putting DYLD_INSERT_LIBRARIES (and the
 * out path) back into envp, so the hook rides across the relaunch into the GUI.
 * Pure interpose, no binary patch, version-agnostic. */
static char g_hook_path[1024];
static char g_out_path[1024];

static void nf_capture_self_paths(void) {
    const char *h = getenv("DYLD_INSERT_LIBRARIES");
    const char *o = getenv("WECHAT_KEY_HOOK_OUT");
    if (h && *h) { strncpy(g_hook_path, h, sizeof(g_hook_path) - 1); }
    if (o && *o) { strncpy(g_out_path, o, sizeof(g_out_path) - 1); }
}

/* Build a new envp that guarantees our two vars are present (replacing any
 * existing/scrubbed copies). Caller leaks the small allocation (process is about
 * to exec). Returns NULL if nothing to inject. */
static char **nf_build_envp(char *const envp[]) {
    if (!g_hook_path[0]) return NULL;
    size_t n = 0;
    if (envp) while (envp[n]) n++;
    char **ne = (char **)malloc((n + 3) * sizeof(char *));
    if (!ne) return NULL;
    size_t j = 0;
    for (size_t i = 0; i < n; i++) {
        if (strncmp(envp[i], "DYLD_INSERT_LIBRARIES=", 22) == 0) continue;
        if (strncmp(envp[i], "WECHAT_KEY_HOOK_OUT=", 20) == 0) continue;
        ne[j++] = envp[i];
    }
    static char hbuf[1100];
    snprintf(hbuf, sizeof(hbuf), "DYLD_INSERT_LIBRARIES=%s", g_hook_path);
    ne[j++] = hbuf;
    if (g_out_path[0]) {
        static char obuf[1100];
        snprintf(obuf, sizeof(obuf), "WECHAT_KEY_HOOK_OUT=%s", g_out_path);
        ne[j++] = obuf;
    }
    ne[j] = NULL;
    nf_emit_event("reinject", "dyld_insert_reinjected");
    return ne;
}

static int nf_execve(const char *path, char *const argv[], char *const envp[]) {
    char **ne = nf_build_envp(envp);
    return execve(path, argv, ne ? ne : envp);
}

static int nf_posix_spawn(pid_t *pid, const char *path,
                          const posix_spawn_file_actions_t *fa,
                          const posix_spawnattr_t *attr,
                          char *const argv[], char *const envp[]) {
    char **ne = nf_build_envp(envp);
    return posix_spawn(pid, path, fa, attr, argv, ne ? ne : envp);
}

static int nf_posix_spawnp(pid_t *pid, const char *file,
                           const posix_spawn_file_actions_t *fa,
                           const posix_spawnattr_t *attr,
                           char *const argv[], char *const envp[]) {
    char **ne = nf_build_envp(envp);
    return posix_spawnp(pid, file, fa, attr, argv, ne ? ne : envp);
}

/* ---- __DATA,__interpose table: live from image load, before first crypto call ---- */
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} nf_interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)nf_CCKeyDerivationPBKDF, (const void *)CCKeyDerivationPBKDF},
    {(const void *)nf_CCCryptorCreate, (const void *)CCCryptorCreate},
    {(const void *)nf_SecCodeCopySigningInformation,
     (const void *)SecCodeCopySigningInformation},
    {(const void *)nf_SecStaticCodeCheckValidityWithErrors,
     (const void *)SecStaticCodeCheckValidityWithErrors},
    {(const void *)nf_exit, (const void *)exit},
    {(const void *)nf__exit, (const void *)_exit},
    {(const void *)nf_abort, (const void *)abort},
    {(const void *)nf_bootstrap_look_up, (const void *)bootstrap_look_up},
    {(const void *)nf_waitpid, (const void *)waitpid},
    {(const void *)nf_flock, (const void *)flock},
    {(const void *)nf_execve, (const void *)execve},
    {(const void *)nf_posix_spawn, (const void *)posix_spawn},
    {(const void *)nf_posix_spawnp, (const void *)posix_spawnp},
};
