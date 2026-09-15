using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal static class FakeApplication
{
    [DllImport("user32.dll")]
    private static extern bool RegisterHotKey(IntPtr window, int id, uint modifiers, uint key);

    [DllImport("user32.dll")]
    private static extern bool UnregisterHotKey(IntPtr window, int id);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr window, System.Text.StringBuilder text, int count);

    private const int HotkeyMessage = 0x0312;
    private const int HotkeyId = 0x4C53;
    private const uint Alt = 0x0001;
    private const uint Control = 0x0002;

    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        bool target = Array.Exists(args, value => value == "--target");
        Application.Run(target ? (Form)new TargetWindow() : new LosslessWindow());
    }

    private sealed class TargetWindow : Form
    {
        public TargetWindow()
        {
            Text = "LS Companion Simulation Game";
            ClientSize = new Size(960, 640);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(18, 24, 38);
            var title = new Label {
                Text = "Fake Game Target",
                ForeColor = Color.FromArgb(96, 165, 250),
                Font = new Font("Segoe UI", 28, FontStyle.Bold),
                AutoSize = true,
                Location = new Point(42, 42)
            };
            var instructions = new Label {
                Text = "Keep this window focused, then activate scaling from LS Companion.\r\n" +
                       "Ctrl+Alt+S toggles the simulated Lossless Scaling overlay.",
                ForeColor = Color.WhiteSmoke,
                Font = new Font("Segoe UI", 13),
                AutoSize = true,
                Location = new Point(46, 112)
            };
            Controls.Add(title);
            Controls.Add(instructions);
        }
    }

    private sealed class LosslessWindow : Form
    {
        private Form overlay;
        private readonly string logPath;
        private readonly string commandPath;
        private readonly Timer commandTimer;

        public LosslessWindow()
        {
            Text = "Lossless Scaling Simulator Controller";
            ShowInTaskbar = false;
            FormBorderStyle = FormBorderStyle.FixedToolWindow;
            StartPosition = FormStartPosition.Manual;
            Location = new Point(-32000, -32000);
            Size = new Size(1, 1);
            Opacity = 0;
            logPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "simulation.log");
            commandPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "simulation.command");
            commandTimer = new Timer { Interval = 100 };
            commandTimer.Tick += PollCommand;
            commandTimer.Start();
        }

        protected override void OnHandleCreated(EventArgs args)
        {
            base.OnHandleCreated(args);
            bool registered = RegisterHotKey(Handle, HotkeyId, Control | Alt, (uint)Keys.S);
            AppendLog(registered
                ? "Simulator ready; hotkey Ctrl+Alt+S registered"
                : "Simulator error; hotkey Ctrl+Alt+S registration failed");
        }

        protected override void OnHandleDestroyed(EventArgs args)
        {
            commandTimer.Stop();
            UnregisterHotKey(Handle, HotkeyId);
            base.OnHandleDestroyed(args);
        }

        private void PollCommand(object sender, EventArgs args)
        {
            if (!File.Exists(commandPath))
                return;
            try
            {
                File.Delete(commandPath);
                ToggleScaling();
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        protected override void WndProc(ref Message message)
        {
            if (message.Msg == HotkeyMessage && message.WParam.ToInt32() == HotkeyId)
                ToggleScaling();
            base.WndProc(ref message);
        }

        private void ToggleScaling()
        {
            if (overlay != null && !overlay.IsDisposed)
            {
                overlay.Close();
                overlay = null;
                AppendLog("Scaling stopped");
                return;
            }

            IntPtr foreground = GetForegroundWindow();
            uint pid;
            GetWindowThreadProcessId(foreground, out pid);
            string processName = "unknown.exe";
            try { processName = Process.GetProcessById((int)pid).ProcessName + ".exe"; }
            catch { }
            var title = new System.Text.StringBuilder(512);
            GetWindowText(foreground, title, title.Capacity);
            AppendLog(String.Format(
                "Scaling started for window: {0} (PID: {1}, Proc: {2})",
                title.Length == 0 ? "Simulation Target" : title.ToString(), pid, processName));
            AppendLog("Capture method: WGC, Scaling mode: Simulation");

            overlay = new Form {
                Text = "Simulation Presentation Overlay",
                FormBorderStyle = FormBorderStyle.None,
                StartPosition = FormStartPosition.Manual,
                Bounds = Screen.FromHandle(foreground).Bounds,
                TopMost = true,
                ShowInTaskbar = false,
                BackColor = Color.FromArgb(8, 12, 22),
                Opacity = 0.94
            };
            overlay.Controls.Add(new Label {
                Text = "LOSSLESS SCALING SIMULATION\r\n\r\nCtrl+Alt+S to stop",
                ForeColor = Color.FromArgb(96, 165, 250),
                BackColor = Color.Transparent,
                Font = new Font("Segoe UI", 28, FontStyle.Bold),
                AutoSize = true,
                Location = new Point(70, 70)
            });
            overlay.Show();
        }

        private void AppendLog(string message)
        {
            File.AppendAllText(logPath, DateTime.Now.ToString("O") + " " + message + Environment.NewLine);
        }
    }
}
