/* aplite.exe: the Windows dist's entry point (aplite/build_dist.sh builds it with zig). Does what aplite_fallback.cmd
 * does, for people and tools alike: `aplite <component|patch|url> [args]`.
 * - Double-clicked (alone in a console of its own, output to that console, not in Windows Terminal yet): reopens
 *   itself in Windows Terminal when there is one (APLITE_NO_WT=1 never does).
 * - Otherwise (cmd, PowerShell, a tool capturing the output) runs in place: python\python.exe -m aplite <args>, exit
 *   code passed through, started again on RELAUNCH (back to the launcher).
 * - In a console of its own, an error exit waits for Enter so the window doesn't close on it. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

#define RELAUNCH 75 /* aplite exits with this to be started again, fresh (background.RELAUNCH) */

static wchar_t here[MAX_PATH]; /* this exe's folder, with a trailing backslash */

/* The command line after this program's name, as given (no re-quoting). argv[0] rules: quoted up to the next quote,
 * else up to whitespace. */
static const wchar_t *args_given(void) {
    const wchar_t *p = GetCommandLineW();
    if (*p == L'"') {
        for (p++; *p && *p != L'"'; p++) {}
        if (*p) p++;
    } else {
        for (; *p && *p != L' ' && *p != L'\t'; p++) {}
    }
    while (*p == L' ' || *p == L'\t') p++;
    return p;
}

static BOOL is_console(DWORD which) {
    return GetFileType(GetStdHandle(which)) == FILE_TYPE_CHAR;
}

/* The console was made for us (double-click, Start menu, a URL handler): it closes when we exit. */
static BOOL own_console(void) {
    DWORD pids[2];
    return GetConsoleProcessList(pids, 2) == 1 && is_console(STD_INPUT_HANDLE) && is_console(STD_OUTPUT_HANDLE);
}

static BOOL WINAPI ignore_ctrl(DWORD type) {
    return type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT; /* Python handles them; we wait for its exit code */
}

/* Run a command line in this console and return its exit code; -1 when it can't start. */
static long run(const wchar_t *format, const wchar_t *a, const wchar_t *b) {
    static wchar_t line[32768];
    STARTUPINFOW si = {sizeof si};
    PROCESS_INFORMATION pi;
    DWORD code = 1;
    _snwprintf(line, 32767, format, a, b);
    line[32767] = 0;
    if (!CreateProcessW(NULL, line, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) return -1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (long)code;
}

static void set_env(void) {
    static wchar_t value[32768];
    wchar_t old[32000] = L"";
    SetEnvironmentVariableW(L"SKIP_REQUIREMENTS_UPDATE", L"1");
    SetEnvironmentVariableW(L"APLITE_DATA_DIR", here); /* user data in this folder (aplite/datadir.py) */
    SetEnvironmentVariableW(L"APLITE_RELAUNCH", L"75");
    if (GetEnvironmentVariableW(L"PYTHONPATH", old, 32000) >= 32000) old[0] = 0;
    _snwprintf(value, 32767, old[0] ? L"%lsapp;%ls" : L"%lsapp%ls", here, old);
    value[32767] = 0;
    SetEnvironmentVariableW(L"PYTHONPATH", value);
}

static int finish(long code, BOOL owned) {
    if (owned && code != 0) {
        if (code < 0) wprintf(L"\nCouldn't start %lspython\\python.exe.", here);
        else wprintf(L"\nArchipelago Lite exited with an error (status %ld).", code);
        wprintf(L" Press Enter to close.");
        getwchar();
    }
    return code < 0 ? 1 : (int)code;
}

int wmain(void) {
    wchar_t self[MAX_PATH], wt[MAX_PATH], python[MAX_PATH + 32];
    const wchar_t *args = args_given();
    BOOL owned = own_console();
    DWORD n = GetModuleFileNameW(NULL, self, MAX_PATH);
    wchar_t *slash;
    long code;
    if (n == 0 || n >= MAX_PATH) return 1;
    wcscpy(here, self);
    slash = wcsrchr(here, L'\\');
    if (slash) slash[1] = 0;

    if (owned && !GetEnvironmentVariableW(L"WT_SESSION", NULL, 0) && !GetEnvironmentVariableW(L"APLITE_NO_WT", NULL, 0)
        && SearchPathW(NULL, L"wt.exe", NULL, MAX_PATH, wt, NULL)) {
        static wchar_t line[32768];
        STARTUPINFOW si = {sizeof si};
        PROCESS_INFORMATION pi;
        _snwprintf(line, 32767, L"\"%ls\" -d . \"%ls\" %ls", wt, self, args);
        line[32767] = 0;
        if (CreateProcessW(NULL, line, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi)) {
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            return 0;
        }
    }

    set_env();
    SetConsoleCtrlHandler(ignore_ctrl, TRUE);
    _snwprintf(python, MAX_PATH + 31, L"%lspython\\python.exe", here);
    python[MAX_PATH + 31] = 0;
    code = run(L"\"%ls\" -m aplite %ls", python, args);
    while (code == RELAUNCH) code = run(L"\"%ls\" -m aplite%ls", python, L"");
    return finish(code, owned);
}
