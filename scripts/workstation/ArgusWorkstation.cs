// Ari's Workstation: a window that shows Ari's child session (a second Windows session of your own user).
//
// Built by `scripts\workstation-setup.ps1 install` with the .NET Framework's own compiler (nothing to install) into
// data\workstation\ArgusWorkstation.exe. `python -m argus.child_session host` runs it (the desk supervisor does, when
// ari.workstation is "child") and starts it again if it closes.
//
//   ArgusWorkstation.exe [--log <file>] [--size 1280x800]
//   ArgusWorkstation.exe --forget          forget the saved Windows password
//
// It connects the Remote Desktop control to this PC with ConnectToChildSession. Windows usually signs the child
// session in by itself. When it doesn't (a Microsoft account signed in with a PIN, for example), the window asks once
// for your Windows password and keeps it encrypted for your Windows user only (DPAPI) in
// %LOCALAPPDATA%\Argus\workstation.cred: never in the repo, never sent anywhere but this PC's own sign-in.
using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;

[ComImport, Guid("302D8188-0052-4807-806A-362B628F9AC5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMsRdpExtendedSettings
{
    void put_Property([MarshalAs(UnmanagedType.BStr)] string name, ref object value);
    void get_Property([MarshalAs(UnmanagedType.BStr)] string name, out object value);
}

// The Remote Desktop control (mstscax.dll), hosted the WinForms way so it is properly placed and activated.
class RdpControl : AxHost
{
    public RdpControl() : base("8B918B82-7985-4C24-89DF-C33AD2BBFBCD") { }
    public object Ocx { get { return GetOcx(); } }
}

static class Workstation
{
    const string Title = "Ari's Workstation";
    static string logPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "workstation.log");
    static readonly string credPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Argus", "workstation.cred");

    [DllImport("wtsapi32.dll")]
    static extern bool WTSIsChildSessionsEnabled(out bool enabled);

    static void Log(string msg)
    {
        try { File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg + Environment.NewLine); }
        catch { }
    }

    static string LoadPassword()
    {
        try
        {
            if (File.Exists(credPath))
                return Encoding.UTF8.GetString(ProtectedData.Unprotect(File.ReadAllBytes(credPath), null,
                                                                       DataProtectionScope.CurrentUser));
        }
        catch (Exception e) { Log("couldn't read the saved password: " + e.Message); }
        return null;
    }

    static void SavePassword(string pw)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(credPath));
            File.WriteAllBytes(credPath, ProtectedData.Protect(Encoding.UTF8.GetBytes(pw), null,
                                                                DataProtectionScope.CurrentUser));
        }
        catch (Exception e) { Log("couldn't save the password: " + e.Message); }
    }

    static string AskPassword(string account)
    {
        var d = new Form { Text = Title + ": sign in once", Width = 460, Height = 200, StartPosition = FormStartPosition.CenterScreen,
                           FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false, MinimizeBox = false, TopMost = true };
        var text = new Label { Left = 12, Top = 12, Width = 420, Height = 48,
                               Text = "Windows didn't sign Ari's session in by itself. Your Windows password for " + account +
                                      " (kept encrypted for your Windows user only, on this PC):" };
        var box = new TextBox { Left = 12, Top = 66, Width = 420, UseSystemPasswordChar = true };
        var ok = new Button { Text = "Sign in", Left = 252, Top = 104, Width = 86, DialogResult = DialogResult.OK };
        var no = new Button { Text = "Cancel", Left = 346, Top = 104, Width = 86, DialogResult = DialogResult.Cancel };
        d.Controls.AddRange(new Control[] { text, box, ok, no });
        d.AcceptButton = ok;
        d.CancelButton = no;
        return d.ShowDialog() == DialogResult.OK && box.Text.Length > 0 ? box.Text : null;
    }

    [STAThread]
    static int Main(string[] args)
    {
        int width = 1280, height = 800;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--log" && i + 1 < args.Length) logPath = args[++i];
            else if (args[i] == "--size" && i + 1 < args.Length)
            {
                var wh = args[++i].Split('x');
                int.TryParse(wh[0], out width);
                if (wh.Length > 1) int.TryParse(wh[1], out height);
            }
            else if (args[i] == "--forget")
            {
                try { File.Delete(credPath); } catch { }
                Console.WriteLine("Forgot the saved password.");
                return 0;
            }
        }
        bool on;
        if (!WTSIsChildSessionsEnabled(out on) || !on)
        {
            Log("child sessions are off: run scripts\\workstation-setup.ps1 install as administrator");
            return 2;
        }

        Application.EnableVisualStyles();
        string account = Environment.MachineName + "\\" + Environment.UserName;
        string pw = LoadPassword();
        bool asked = false;

        var form = new Form { Text = Title, ClientSize = new Size(width, height) };
        var rdp = new RdpControl { Dock = DockStyle.Fill };
        form.Controls.Add(rdp);
        form.Show();
        rdp.CreateControl();
        dynamic ocx = rdp.Ocx;

        Action connect = () =>
        {
            try
            {
                ocx.Server = "localhost";
                if (pw != null)
                {
                    ocx.UserName = account;
                    ocx.AdvancedSettings2.ClearTextPassword = pw;
                }
                ocx.DesktopWidth = width;
                ocx.DesktopHeight = height;
                try { ocx.AdvancedSettings2.SmartSizing = true; } catch { }
                try { ocx.ColorDepth = 32; } catch { }
                try { ocx.AdvancedSettings7.EnableCredSspSupport = true; } catch { }
                object yes = true;
                ((IMsRdpExtendedSettings)rdp.Ocx).put_Property("ConnectToChildSession", ref yes);
                Log("connecting" + (pw != null ? " as " + account : " (Windows signs in by itself)"));
                ocx.Connect();
            }
            catch (Exception e) { Log("couldn't connect: " + e.Message); }
        };

        // The control connects in the background: watch its state, connect again when it drops, and ask for the
        // password once if Windows can't sign the session in by itself.
        int last = -1, failures = 0;
        bool upThisTry = false;
        DateTime lastTry = DateTime.Now;
        var timer = new Timer { Interval = 2000 };
        timer.Tick += delegate
        {
            int state;
            try { state = (int)ocx.Connected; }  // 0 not connected, 1 connected, 2 connecting
            catch (Exception e) { Log("state: " + e.Message); return; }
            if (state != last)
            {
                Log("state " + (state == 1 ? "connected" : state == 2 ? "connecting" : "not connected"));
                form.Text = state == 1 ? Title : Title + (state == 2 ? " (connecting...)" : " (not connected)");
                if (state == 1) { upThisTry = true; failures = 0; }
                if (state == 0 && last != -1 && !upThisTry) failures++;
                last = state;
            }
            if (state != 0) return;
            double wait = failures >= 6 ? 60 : 5;  // keeps failing: once a minute
            if ((DateTime.Now - lastTry).TotalSeconds < wait) return;
            if (failures >= 2 && pw == null && !asked)
            {
                asked = true;
                pw = AskPassword(account);
                if (pw != null) { SavePassword(pw); failures = 0; }
            }
            else if (failures == 4 && pw != null)
            {
                Log("still can't sign in with the saved password: run ArgusWorkstation.exe --forget and try again");
            }
            lastTry = DateTime.Now;
            upThisTry = false;
            connect();
        };
        timer.Start();
        connect();
        Application.Run(form);
        return 0;
    }
}
