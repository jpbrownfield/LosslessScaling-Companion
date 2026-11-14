using System;
using System.IO;
using System.Runtime.InteropServices;

internal class StartupHook
{
    static readonly object Initialization = new object();
    static bool initialized;
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr LoadLibraryExW(string path, IntPtr file, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll")] static extern uint GetCurrentProcessId();
    [UnmanagedFunctionPointer(CallingConvention.Winapi, CharSet = CharSet.Unicode)]
    delegate uint InitializeIsolation(string local, string roaming);
    [UnmanagedFunctionPointer(CallingConvention.Winapi)] delegate uint InitializeComponents();

    static string Required(string key)
    {
        string value = Environment.GetEnvironmentVariable(key);
        if (String.IsNullOrEmpty(value)) throw new InvalidOperationException("Missing isolation contract: " + key);
        return value;
    }
    static bool Same(string a, string b) => String.Equals(Path.GetFullPath(a).TrimEnd('\\'), Path.GetFullPath(b).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
    static string Escape(string text) => text.Replace("\\", "\\\\").Replace("\"", "\\\"");

    public static void Initialize()
    {
        lock (Initialization)
        {
            if (initialized) return;
            InitializeCore();
            initialized = true;
        }
    }

    static void InitializeCore()
    {
        string root = AppContext.BaseDirectory;
        if (!File.Exists(Path.Combine(root, ".companion-runtime.json")))
            throw new InvalidOperationException("Isolation startup is restricted to a managed LS clone");
        string local = Required("LSC_PRIVATE_LOCALAPPDATA"), roaming = Required("LSC_PRIVATE_APPDATA");
        string token = Required("LSC_ISOLATION_TOKEN"), receipt = Required("LSC_ISOLATION_RECEIPT");
        if (token.Length != 64 || token.Trim("0123456789abcdef".ToCharArray()).Length != 0)
            throw new InvalidOperationException("Invalid isolation launch identity");
        string helper = Path.Combine(root, ".companion", "bootstrap", "LSC_RuntimeIsolation.dll");
        IntPtr module = LoadLibraryExW(helper, IntPtr.Zero, 0x00000100 | 0x00001000);
        if (module == IntPtr.Zero) throw new InvalidOperationException("Isolation DLL could not load: " + Marshal.GetLastWin32Error());
        IntPtr entry = GetProcAddress(module, "LSCInitializeIsolation");
        if (entry == IntPtr.Zero) throw new InvalidOperationException("Isolation DLL has no initializer");
        var initialize = Marshal.GetDelegateForFunctionPointer<InitializeIsolation>(entry);
        uint error = initialize(local, roaming);
        if (error != 0) throw new InvalidOperationException("Private known-folder initialization failed: " + error);
        // Exercise the same managed API LS uses before it can read settings.
        string actualLocal = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string actualRoaming = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        if (!Same(actualLocal, local) || !Same(actualRoaming, roaming))
            throw new InvalidOperationException("LS settings isolation could not be verified");
        string settings = Path.Combine(actualLocal, "Lossless Scaling", "Settings.xml");
        if (!File.Exists(settings)) throw new InvalidOperationException("Private LS settings are missing");
        if (File.Exists(Path.Combine(root, ".companion", "components.json")))
        {
            // The host owns all component loading. Run it before LS can create
            // graphics devices, even when Windows already loaded system DXGI.
            IntPtr host = LoadLibraryExW(Path.Combine(root, "Lossless.dll"), IntPtr.Zero, 0x00000100 | 0x00001000);
            IntPtr load = host == IntPtr.Zero ? IntPtr.Zero : GetProcAddress(host, "LSCInitializeComponents");
            if (load == IntPtr.Zero || Marshal.GetDelegateForFunctionPointer<InitializeComponents>(load)() != 0)
                throw new InvalidOperationException("Companion host could not initialize the selected components");
        }
        string payload = "{\"schema\":1,\"pid\":" + GetCurrentProcessId() + ",\"token\":\"" + token +
            "\",\"verified\":true,\"settings\":\"" + Escape(settings) + "\"}";
        // LS can restart itself after applying its GPU preference. Its child
        // inherits this launch contract, but must not overwrite the parent's
        // identity or collide with its completed receipt/temporary file.
        string processReceipt = receipt + "." + GetCurrentProcessId();
        string temporary = processReceipt + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, payload);
            File.Move(temporary, processReceipt);
            // Retain the original first-writer receipt for older launchers.
            // New launchers prefer the PID-specific receipt above.
            try { File.Copy(processReceipt, receipt, false); }
            catch (IOException) when (File.Exists(receipt)) { }
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
