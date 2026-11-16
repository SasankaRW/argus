// Argus "Send to ..." Explorer context menu entries (Windows 11 main menu, IExplorerCommand).
// One COM class per device; each launches `argus send <device> <paths...>` hidden. In-proc, so keep it small and quiet.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shobjidl.h>
#include <shlwapi.h>
#include <shlobj.h>
#include <string>
#include <vector>
#include <fstream>
#include <sstream>
#include <atomic>
#include <new>

static HMODULE g_module;
static std::atomic<long> g_locks{0};

// {18078fca-6f80-4237-bb30-b9e3004e444e} phone, {aa8ffa12-3cef-4798-80ed-b1eae89eb6d8} calypso
static const CLSID CLSID_SendPhone   = {0x18078fca, 0x6f80, 0x4237, {0xbb, 0x30, 0xb9, 0xe3, 0x00, 0x4e, 0x44, 0x4e}};
static const CLSID CLSID_SendCalypso = {0xaa8ffa12, 0x3cef, 0x4798, {0x80, 0xed, 0xb1, 0xea, 0xb6, 0xd8, 0x89, 0xeb}};

struct Device { const wchar_t* id; const wchar_t* title; const wchar_t* icon; };
static const Device kPhone   = {L"phone",   L"Send to my phone", L"send-phone.ico"};
static const Device kCalypso = {L"calypso", L"Send to Calypso",  L"send-calypso.ico"};

static std::wstring ModuleDir() {
    wchar_t p[MAX_PATH]; GetModuleFileNameW(g_module, p, MAX_PATH);
    PathRemoveFileSpecW(p);
    return p;
}

static std::wstring Env(const wchar_t* name) {
    wchar_t b[MAX_PATH * 2]; DWORD n = GetEnvironmentVariableW(name, b, MAX_PATH * 2);
    return (n && n < MAX_PATH * 2) ? std::wstring(b, n) : L"";
}

static std::string ReadFile(const std::wstring& path) {
    std::ifstream f(path, std::ios::binary); std::stringstream s; s << f.rdbuf(); return s.str();
}

static std::wstring SendDir() { return Env(L"LOCALAPPDATA") + L"\\Argus\\send"; }

// The installer writes the full path of argus.exe here; fall back to PATH.
static std::wstring ArgusExe() {
    std::string t = ReadFile(SendDir() + L"\\argus-exe.txt");
    while (!t.empty() && (t.back() == '\r' || t.back() == '\n' || t.back() == ' ')) t.pop_back();
    if (t.size() >= 3 && (unsigned char)t[0] == 0xEF) t.erase(0, 3);   // UTF-8 BOM
    if (t.empty()) return L"argus.exe";
    int n = MultiByteToWideChar(CP_UTF8, 0, t.c_str(), (int)t.size(), nullptr, 0);
    std::wstring w(n, 0); MultiByteToWideChar(CP_UTF8, 0, t.c_str(), (int)t.size(), &w[0], n);
    return w;
}

static void Quote(std::wstring& out, const std::wstring& s) {   // CommandLineToArgvW rules
    out += L'"';
    size_t slashes = 0;
    for (wchar_t c : s) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'"') { out.append(slashes * 2 + 1, L'\\'); out += L'"'; }
        else { out.append(slashes, L'\\'); out += c; }
        slashes = 0;
    }
    out.append(slashes * 2, L'\\');
    out += L'"';
}

static void Launch(const std::wstring& cmd) {
    std::wstring line = cmd;
    STARTUPINFOW si{}; si.cb = sizeof si; si.dwFlags = STARTF_USESHOWWINDOW; si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi{};
    if (CreateProcessW(nullptr, &line[0], nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    }
}

class SendCommand : public IExplorerCommand {
    std::atomic<long> refs_{1};
    const Device* dev_;
public:
    explicit SendCommand(const Device* d) : dev_(d) { ++g_locks; }
    ~SendCommand() { --g_locks; }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (riid == IID_IUnknown || riid == IID_IExplorerCommand) { *ppv = static_cast<IExplorerCommand*>(this); AddRef(); return S_OK; }
        *ppv = nullptr; return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return ++refs_; }
    IFACEMETHODIMP_(ULONG) Release() override { long r = --refs_; if (!r) delete this; return r; }

    IFACEMETHODIMP GetTitle(IShellItemArray*, PWSTR* name) override {
        return SHStrDupW(dev_->title, name);
    }
    IFACEMETHODIMP GetIcon(IShellItemArray*, PWSTR* icon) override {
        return SHStrDupW((ModuleDir() + L"\\" + dev_->icon).c_str(), icon);
    }
    IFACEMETHODIMP GetToolTip(IShellItemArray*, PWSTR* tip) override { *tip = nullptr; return E_NOTIMPL; }
    IFACEMETHODIMP GetCanonicalName(GUID* g) override { *g = (dev_ == &kPhone) ? CLSID_SendPhone : CLSID_SendCalypso; return S_OK; }
    IFACEMETHODIMP GetState(IShellItemArray*, BOOL, EXPCMDSTATE* s) override { *s = ECS_ENABLED; return S_OK; }
    IFACEMETHODIMP GetFlags(EXPCMDFLAGS* f) override { *f = ECF_DEFAULT; return S_OK; }
    IFACEMETHODIMP EnumSubCommands(IEnumExplorerCommand** e) override { *e = nullptr; return E_NOTIMPL; }

    IFACEMETHODIMP Invoke(IShellItemArray* items, IBindCtx*) override {
        if (!items) return S_OK;
        DWORD count = 0; items->GetCount(&count);
        std::wstring head; Quote(head, ArgusExe()); head += L" send "; head += dev_->id;
        std::wstring line = head;
        bool any = false;
        for (DWORD i = 0; i < count; ++i) {
            IShellItem* it = nullptr;
            if (FAILED(items->GetItemAt(i, &it))) continue;
            PWSTR path = nullptr;
            if (SUCCEEDED(it->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
                std::wstring q; Quote(q, path);
                if (line.size() + q.size() + 1 > 30000 && any) { Launch(line); line = head; any = false; }
                line += L' '; line += q; any = true;
                CoTaskMemFree(path);
            }
            it->Release();
        }
        if (any) Launch(line);
        return S_OK;
    }
};

class Factory : public IClassFactory {
    std::atomic<long> refs_{1};
    const Device* dev_;
public:
    explicit Factory(const Device* d) : dev_(d) {}
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (riid == IID_IUnknown || riid == IID_IClassFactory) { *ppv = static_cast<IClassFactory*>(this); AddRef(); return S_OK; }
        *ppv = nullptr; return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return ++refs_; }
    IFACEMETHODIMP_(ULONG) Release() override { long r = --refs_; if (!r) delete this; return r; }
    IFACEMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        SendCommand* c = new (std::nothrow) SendCommand(dev_);
        if (!c) return E_OUTOFMEMORY;
        HRESULT hr = c->QueryInterface(riid, ppv); c->Release(); return hr;
    }
    IFACEMETHODIMP LockServer(BOOL l) override { if (l) ++g_locks; else --g_locks; return S_OK; }
};

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void** ppv) {
    const Device* d = IsEqualCLSID(clsid, CLSID_SendPhone) ? &kPhone : IsEqualCLSID(clsid, CLSID_SendCalypso) ? &kCalypso : nullptr;
    if (!d) { *ppv = nullptr; return CLASS_E_CLASSNOTAVAILABLE; }
    Factory* f = new (std::nothrow) Factory(d);
    if (!f) return E_OUTOFMEMORY;
    HRESULT hr = f->QueryInterface(riid, ppv); f->Release(); return hr;
}
STDAPI DllCanUnloadNow() { return g_locks.load() == 0 ? S_OK : S_FALSE; }
BOOL APIENTRY DllMain(HMODULE m, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) { g_module = m; DisableThreadLibraryCalls(m); }
    return TRUE;
}
