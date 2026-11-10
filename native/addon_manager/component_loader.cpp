// SPDX-License-Identifier: MIT
// Explicit, manifest-owned loading; never searches a directory for DLLs.
#include "component_loader.h"
#include "../log/logger.h"
#include <bcrypt.h>
#include <cwctype>
#include <tlhelp32.h>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <set>
#include <stdexcept>
#include <vector>

namespace {
using json = nlohmann::json;
std::recursive_mutex gate;
bool initialized = false;
DWORD result = ERROR_DLL_INIT_FAILED;
json status = json::array();

std::wstring Lower(std::wstring text) {
    for (auto& c : text) c = static_cast<wchar_t>(towlower(c));
    return text;
}
std::string Hash(const std::filesystem::path& path) {
    BCRYPT_ALG_HANDLE algorithm{}; BCRYPT_HASH_HANDLE hash{};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) throw std::runtime_error("Cannot open SHA-256 provider");
    struct Cleanup { BCRYPT_ALG_HANDLE& a; BCRYPT_HASH_HANDLE& h;
        ~Cleanup() { if(h) BCryptDestroyHash(h); if(a) BCryptCloseAlgorithmProvider(a,0); } } cleanup{algorithm,hash};
    DWORD size{}, count{};
    if (BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&size), sizeof(size), &count, 0)) throw std::runtime_error("Cannot allocate SHA-256 state");
    std::vector<UCHAR> object(size), buffer(65536);
    if (BCryptCreateHash(algorithm, &hash, object.data(), size, nullptr, 0, 0)) throw std::runtime_error("Cannot create SHA-256 hash");
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::runtime_error("Declared component is missing");
    while (file) {
        file.read(reinterpret_cast<char*>(buffer.data()), buffer.size());
        if (file.gcount() && BCryptHashData(hash, buffer.data(), static_cast<ULONG>(file.gcount()), 0)) throw std::runtime_error("Cannot hash component");
    }
    if (!file.eof()) throw std::runtime_error("Cannot read component");
    UCHAR digest[32]{};
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0)) throw std::runtime_error("Cannot finish component hash");
    const char* hex="0123456789abcdef"; std::string text;
    for (auto byte:digest) { text+=hex[byte>>4]; text+=hex[byte&15]; }
    return text;
}

void Load() {
    wchar_t executable[32768]{}; GetModuleFileNameW(nullptr, executable, ARRAYSIZE(executable));
    const auto root = std::filesystem::canonical(std::filesystem::path(executable).parent_path());
    const auto manifest = root/L".companion/components.json";
    if (!std::filesystem::exists(manifest)) return;
    if (std::filesystem::file_size(manifest)>65536) throw std::runtime_error("Component manifest exceeds size limit");
    std::ifstream input(manifest); const auto data=json::parse(input);
    if (data.value("schema",0)!=1 || !data.contains("components") || !data["components"].is_array() || data["components"].size()>32)
        throw std::runtime_error("Invalid component manifest");
    const auto allowed = Lower(std::filesystem::weakly_canonical(root/L".companion"/L"components").wstring()) + L"\\";
    std::set<std::string> identities;
    // Validate the complete plan before loading any selected module.
    std::vector<std::pair<std::string,std::filesystem::path>> plan;
    for (const auto& component:data["components"]) {
        const auto id=component.at("id").get<std::string>();
        const auto relative=std::filesystem::u8path(component.at("path").get<std::string>());
        if (relative.is_absolute() || relative.has_root_path() || !identities.insert(id).second)
            throw std::runtime_error("Duplicate component or absolute component path");
        const auto path=std::filesystem::canonical(root/relative);
        if (Lower(path.wstring()).rfind(allowed,0)!=0 || Lower(path.extension().wstring())!=L".dll")
            throw std::runtime_error("Component path escapes its managed directory");
        if (Hash(path)!=component.at("sha256").get<std::string>()) throw std::runtime_error("Component hash mismatch: "+id);
        plan.emplace_back(id,path);
    }
    for (const auto& [id,path]:plan) {
        HANDLE snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPMODULE,GetCurrentProcessId());
        if(snapshot==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot inspect loaded components");
        MODULEENTRY32W module{}; module.dwSize=sizeof(module);
        bool conflict=false;
        if(Module32FirstW(snapshot,&module)) do {
            const bool sameName=Lower(module.szModule)==Lower(path.filename().wstring());
            const bool anotherReShade=id=="reshade" && GetProcAddress(module.hModule,"ReShadeVersion");
            if((sameName||anotherReShade) && Lower(module.szExePath)!=Lower(path.wstring())) { conflict=true; break; }
        } while(Module32NextW(snapshot,&module));
        CloseHandle(snapshot);
        if(conflict) throw std::runtime_error("Another copy of selected component is already loaded: "+id);
        HMODULE loaded=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if(!loaded) throw std::runtime_error("Component load failed: "+id+" (Windows "+std::to_string(GetLastError())+")");
        HMODULE pinned=nullptr;
        if(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
                              reinterpret_cast<LPCWSTR>(loaded),&pinned)) throw std::runtime_error("Cannot retain component: "+id);
        status.push_back({{"id",id},{"path",path.u8string()},{"loaded",true},{"sha256",Hash(path)}});
        LOG_INFO("Companion","Explicit component loaded: %s from %s",id.c_str(),path.u8string().c_str());
    }
}
}

extern "C" __declspec(dllexport) DWORD WINAPI LSCInitializeComponents() {
    std::lock_guard<std::recursive_mutex> lock(gate);
    if(initialized) return result;
    initialized=true;
    try { Load(); result=ERROR_SUCCESS; }
    catch(const std::exception& error) {
        status.push_back({{"loaded",false},{"error",error.what()}});
        LOG_ERROR("Companion","Explicit component initialization failed: %s",error.what());
    }
    return result;
}
namespace lsc {
nlohmann::json ComponentStatus() { std::lock_guard<std::recursive_mutex> lock(gate); return status; }
}
