// A real WPF dispatcher/window fixture; all test windows stay off-screen.
using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;

static class StartupGuardWpfTest
{
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr window, int attribute, out int value, int size);

    [STAThread]
    static int Main()
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var window = new Window {
            Title = "LS Companion WPF startup guard test", Width = 200, Height = 100,
            Left = -30000, Top = -30000, WindowStartupLocation = WindowStartupLocation.Manual
        };
        int result = 0;
        window.Show();
        var handle = new WindowInteropHelper(window).Handle;
        ShowWindow(handle, 1);
        ShowWindow(handle, 1);
        if (IsWindowVisible(handle)) result = 20;
        int cloaked;
        if (DwmGetWindowAttribute(handle, 14, out cloaked, 4) == 0 && cloaked == 0) result = 21;
        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(4500) };
        timer.Tick += delegate {
            timer.Stop();
            if (IsWindowVisible(handle)) result = 22;
            if (DwmGetWindowAttribute(handle, 14, out cloaked, 4) == 0 && cloaked != 0) result = 23;
            ShowWindow(handle, 4);
            if (!IsWindowVisible(handle)) result = 24;
            window.Close();
            app.Shutdown();
        };
        timer.Start();
        app.Run();
        return result;
    }
}
