// Process-local known-folder redirection, installed before the managed LS entry point.
#include <windows.h>
#include <shlobj.h>
#include <knownfolders.h>
#include <MinHook.h>
#include <string>
#include <mutex>

namespace {
std::wstring localPath, roamingPath;
using KnownFolder = HRESULT(WINAPI*)(REFKNOWNFOLDERID, DWORD, HANDLE, PWSTR*);
using Folder = HRESULT(WINAPI*)(HWND, int, HANDLE, DWORD, LPWSTR);
KnownFolder originalKnown = nullptr;
Folder originalFolder = nullptr;
std::mutex initialization;
bool installed = false;

HRESULT WINAPI PrivateKnownFolder(REFKNOWNFOLDERID id, DWORD flags, HANDLE token, PWSTR* output) {
    const auto* path = IsEqualGUID(id, FOLDERID_LocalAppData) ? &localPath :
        IsEqualGUID(id, FOLDERID_RoamingAppData) ? &roamingPath : nullptr;
    if (!path) return originalKnown(id, flags, token, output);
    if (!output) return E_INVALIDARG;
    const auto size = (path->size() + 1) * sizeof(wchar_t);
    *output = static_cast<PWSTR>(CoTaskMemAlloc(size));
    if (!*output) return E_OUTOFMEMORY;
    memcpy(*output, path->c_str(), size);
    return S_OK;
}

HRESULT WINAPI PrivateFolder(HWND window, int folder, HANDLE token, DWORD flags, LPWSTR output) {
    const int id = folder & 0xff;
    const auto* path = id == CSIDL_LOCAL_APPDATA ? &localPath : id == CSIDL_APPDATA ? &roamingPath : nullptr;
    if (!path) return originalFolder(window, folder, token, flags, output);
    if (!output || path->size() >= MAX_PATH) return E_INVALIDARG;
    wcscpy_s(output, MAX_PATH, path->c_str());
    return S_OK;
}
}

extern "C" __declspec(dllexport) DWORD WINAPI LSCInitializeIsolation(const wchar_t* local, const wchar_t* roaming) {
    std::lock_guard<std::mutex> lock(initialization);
    if (installed) return localPath == local && roamingPath == roaming ? ERROR_SUCCESS : ERROR_INVALID_PARAMETER;
    if (!local || !roaming || !*local || !*roaming) return ERROR_INVALID_PARAMETER;
    localPath = local; roamingPath = roaming;
    HMODULE shell = LoadLibraryW(L"shell32.dll");
    if (!shell) return GetLastError();
    const auto initialized = MH_Initialize();
    if (initialized != MH_OK && initialized != MH_ERROR_ALREADY_INITIALIZED) return ERROR_DLL_INIT_FAILED;
    void* known = reinterpret_cast<void*>(GetProcAddress(shell, "SHGetKnownFolderPath"));
    void* folder = reinterpret_cast<void*>(GetProcAddress(shell, "SHGetFolderPathW"));
    if (!known || !folder || MH_CreateHook(known, &PrivateKnownFolder, reinterpret_cast<void**>(&originalKnown)) != MH_OK ||
        MH_CreateHook(folder, &PrivateFolder, reinterpret_cast<void**>(&originalFolder)) != MH_OK ||
        MH_EnableHook(known) != MH_OK || MH_EnableHook(folder) != MH_OK) return ERROR_DLL_INIT_FAILED;
    HMODULE pinned = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                           reinterpret_cast<LPCWSTR>(&LSCInitializeIsolation), &pinned)) return GetLastError();
    installed = true;
    return ERROR_SUCCESS;
}
