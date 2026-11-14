using System;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;

class Program
{
    [DllImport("shell32.dll")] static extern int SHGetKnownFolderPath(ref Guid id, uint flags, IntPtr token, out IntPtr path);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetModuleHandleW(string name);
    static int Main(string[] args)
    {
        if (args.Length == 0) return 0;
        if (args.Length > 1 && args[1] == "--relaunch")
        {
            var start = new ProcessStartInfo(Environment.ProcessPath) { UseShellExecute = false };
            start.ArgumentList.Add(args[0]);
            using var child = Process.Start(start);
            Console.WriteLine("child=" + child.Id);
            if (!child.WaitForExit(20000)) { child.Kill(); return 25; }
            return child.ExitCode;
        }
        if (args.Length > 1 && GetModuleHandleW(args[1]) == IntPtr.Zero) return 24;
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (!String.Equals(Path.GetFullPath(local), Path.GetFullPath(args[0]), StringComparison.OrdinalIgnoreCase)) return 21;
        var id = new Guid("F1B32785-6FBA-4FCF-9D55-7B8E7F157091");
        if (SHGetKnownFolderPath(ref id, 0, IntPtr.Zero, out IntPtr value) != 0) return 22;
        try { if (!String.Equals(Marshal.PtrToStringUni(value), local, StringComparison.OrdinalIgnoreCase)) return 23; }
        finally { Marshal.FreeCoTaskMem(value); }
        string settings = Path.Combine(local, "Lossless Scaling", "Settings.xml");
        File.WriteAllText(settings, "<Settings><Fixture>private</Fixture></Settings>");
        Console.WriteLine(settings);
        return 0;
    }
}
