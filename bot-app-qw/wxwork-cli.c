#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdarg.h>
#include <ctype.h>
#include <signal.h>
#include <curl/curl.h>

#define MAX_URL 1024
#define DEFAULT_HOOK_URL "http://localhost:6255/message"
#define MAX_POLL_RESPONSE 65536

static char HOOK_URL[MAX_URL] = DEFAULT_HOOK_URL;
static volatile int g_exit = 0;
static uint32_t g_client_id = 0;

typedef struct {
    char *data;
    size_t len;
    size_t cap;
    int truncated;
} PollResponse;

static int has_visible_content(const char *s) {
    if (!s) return 0;
    while (*s) {
        if (!isspace((unsigned char)*s)) return 1;
        s++;
    }
    return 0;
}

static void log_info(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vprintf(fmt, args);
    printf("\n");
    fflush(stdout);
    va_end(args);
}

static void trim(char *s) {
    unsigned char *start = (unsigned char *)s;
    while (isspace(*start)) start++;
    if ((char *)start != s) {
        memmove(s, start, strlen((char *)start) + 1);
    }
    if (*s == 0) return;

    char *end = s + strlen(s) - 1;
    while (end >= s && isspace((unsigned char)*end)) *end-- = 0;
}

static void read_ini_config(const char *path) {
    FILE *fp = fopen(path, "r");
    if (!fp) {
        log_info("[INFO] Config not found, using default hook: %s", DEFAULT_HOOK_URL);
        return;
    }

    char line[1024];
    int found = 0;
    while (fgets(line, sizeof(line), fp)) {
        trim(line);
        if (line[0] == 0 || line[0] == ';' || line[0] == '#') continue;
        if (!found && strcmp(line, "[config]") == 0) { found = 1; continue; }
        if (!found) continue;
        if (line[0] == '[') break;

        char *eq = strchr(line, '=');
        if (!eq) continue;

        *eq = 0;
        char *key = line;
        char *val = eq + 1;
        trim(key);
        trim(val);

        if (strcmp(key, "hook") == 0) {
            strncpy(HOOK_URL, val, MAX_URL - 1);
        }
    }
    fclose(fp);
}

static void report_to_server(uint32_t cid, const char *json) {
    if (HOOK_URL[0] == 0) return;

    CURL *curl = curl_easy_init();
    if (!curl) return;

    struct curl_slist *hdr = curl_slist_append(NULL, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_URL, HOOK_URL);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, hdr);
    CURLcode r = curl_easy_perform(curl);
    if (r != CURLE_OK) {
        log_info("[CURL ERROR] %s", curl_easy_strerror(r));
    }
    curl_slist_free_all(hdr);
    curl_easy_cleanup(curl);
}

// DLL declarations
typedef void (__stdcall *ConnectCB)(uint32_t);
typedef void (__stdcall *RecvCB)(uint32_t, const char *, uint32_t);
typedef void (__stdcall *CloseCB)(uint32_t);

static HMODULE hDll = NULL;
static int (__stdcall *InjectWxWork)(const char *, const char *) = NULL;
static int (__stdcall *InitWxWorkSocket)(ConnectCB, RecvCB, CloseCB) = NULL;
static void (__stdcall *UseUtf8)(void) = NULL;
static int (__stdcall *SendWxWorkData)(uint32_t, const char *) = NULL;
static int (__stdcall *DestroyWxWork)(void) = NULL;

static void send_wxwork_data(const char *buf) {
    if (!has_visible_content(buf)) {
        return;
    }

    if (g_client_id && SendWxWorkData) {
        int r = SendWxWorkData(g_client_id, buf);
        if (r != 1) {
            log_info("[ERROR] SendWxWorkData returned: %d", r);
        }
    } else {
        log_info("[ERROR] g_client_id is 0 or SendWxWorkData is NULL");
    }
}

static int load_dll_and_resolve(const char *dll_path) {
    hDll = LoadLibraryA(dll_path);
    if (!hDll) return 0;

    uintptr_t base = (uintptr_t)hDll;
    UseUtf8 = (void (__stdcall *)(void))(base + 0x4A60);
    InitWxWorkSocket = (int (__stdcall *)(ConnectCB,RecvCB,CloseCB))(base + 0x4B10);
    InjectWxWork = (int (__stdcall *)(const char *, const char *))(base + 0x4BF0);
    DestroyWxWork = (int (__stdcall *)(void))(base + 0x5310);
    SendWxWorkData = (int (__stdcall *)(uint32_t, const char *))(base + 0x5800);

    return 1;
}

// Callbacks
static void __stdcall on_connect(uint32_t cid) {
    g_client_id = cid;
    log_info("[CONNECTED] ID=%u", cid);
}

static void __stdcall on_recv(uint32_t cid, const char *msg, uint32_t len) {
    (void)len;
    log_info("[RECEIVED] ID=%u", cid);
    log_info("[RECEIVED MSG] %s", msg);
    report_to_server(cid, msg);
}

static void __stdcall on_close(uint32_t cid) {
    log_info("[DISCONNECTED] ID=%u", cid);
}

static size_t handle_poll_response(char *ptr, size_t size, size_t nmemb, void *userdata) {
    size_t total = size * nmemb;
    PollResponse *response = (PollResponse *)userdata;

    if (!response || !response->data || response->cap == 0) {
        return total;
    }

    size_t available = response->cap - response->len - 1;
    size_t copy_len = total;
    if (copy_len > available) {
        copy_len = available;
        response->truncated = 1;
    }

    if (copy_len > 0) {
        memcpy(response->data + response->len, ptr, copy_len);
        response->len += copy_len;
        response->data[response->len] = 0;
    }

    return total;
}

static void polling_loop() {
    log_info("[POLLING] Interval: 1 seconds to %s", HOOK_URL);

    while (!g_exit) {
        CURL *curl = curl_easy_init();
        if (curl) {
            char response_buf[MAX_POLL_RESPONSE] = {0};
            PollResponse response = { response_buf, 0, sizeof(response_buf), 0 };
            long status = 0;

            curl_easy_setopt(curl, CURLOPT_URL, HOOK_URL);
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, handle_poll_response);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);

            CURLcode r = curl_easy_perform(curl);
            curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);

            if (r != CURLE_OK) {
                log_info("[POLL ERROR] %s", curl_easy_strerror(r));
            } else if (status == 204) {
                /* No queued message. */
            } else if (status < 200 || status >= 300) {
                log_info("[POLL ERROR] HTTP %ld", status);
            } else if (has_visible_content(response.data)) {
                if (response.truncated) {
                    log_info("[POLL ERROR] Response truncated at %d bytes", MAX_POLL_RESPONSE - 1);
                }
                send_wxwork_data(response.data);
                log_info("[POLL RESPONSE] %s", response.data);
            }

            curl_easy_cleanup(curl);
        }
        Sleep(1000);
    }
}

static BOOL WINAPI console_ctrl_handler(DWORD type) {
    if (type == CTRL_C_EVENT || type == CTRL_CLOSE_EVENT) {
        g_exit = 1;
        return TRUE;
    }
    return FALSE;
}

int main(int argc, char *argv[]) {
    const char *dll_path = "loader.dll";
    const char *helper_path = "helper.dll";
    const char *wxwork_path = "C:\\Program Files (x86)\\WXWork\\WXWork.exe";

    if (argc > 1) dll_path = argv[1];
    if (argc > 2) helper_path = argv[2];
    if (argc > 3) wxwork_path = argv[3];

    SetConsoleCtrlHandler(console_ctrl_handler, TRUE);

    if (!load_dll_and_resolve(dll_path)) {
        log_info("[FATAL] Failed to load DLL: %s", dll_path);
        return 1;
    }

    UseUtf8();
    InitWxWorkSocket(on_connect, on_recv, on_close);
    read_ini_config("bot.ini");

    log_info("[CONFIG] dll=%s", dll_path);
    log_info("[CONFIG] helper=%s", helper_path);
    log_info("[CONFIG] wxwork=%s", wxwork_path);
    log_info("[CONFIG] hook=%s", HOOK_URL);

    int inj = InjectWxWork(helper_path, wxwork_path);
    log_info("[INJECT RESULT] %d", inj);

    polling_loop();

    if (DestroyWxWork) DestroyWxWork();
    if (hDll) FreeLibrary(hDll);

    return 0;
}
