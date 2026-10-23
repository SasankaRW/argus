// Ari's Workstation: a window that shows Ari's child session (a second Windows session of your own user).
//
// Built by `scripts\workstation-setup.ps1 install` with the .NET Framework's own compiler (nothing to install) into
// data\workstation\ArgusWorkstation.exe. `python -m argus.child_session host` runs it (the desk supervisor does, when
// ari.workstation is "child") and starts it again if it closes.
//
//   ArgusWorkstation.exe [--log <file>] [--size 1280x800]
//
// It connects the Remote Desktop control to this PC with ConnectToChildSession, and Windows signs the child session
// in with your own sign-in. It never asks for, keeps or types a password or PIN.
//
// Your account must never be locked out by it: every sign-in that fails counts as a wrong password for Windows. So it
// never tries while your session is locked (Windows can't sign Ari in then), waits 2 minutes after a failed try, and
// makes at most 3 failed tries an hour, counted across restarts in %LOCALAPPDATA%\Argus\workstation-tries.txt
// (Windows locks an account after 10 by default). After that it waits, saying so in the title, until you unlock the
// PC or click Connect now (each allows one more try).
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

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
    const int MaxFailedPerHour = 3;
    const double AfterFailS = 120, AfterDropS = 30;
    static string logPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "workstation.log");
    static readonly string triesPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Argus", "workstation-tries.txt");
    static bool lockedByEvent;

    [DllImport("wtsapi32.dll")]
    static extern bool WTSIsChildSessionsEnabled(out bool enabled);
    [DllImport("wtsapi32.dll")]
    static extern bool WTSQuerySessionInformationW(IntPtr server, int sessionId, int infoClass, out IntPtr buffer,
                                                   out int bytes);
    [DllImport("wtsapi32.dll")]
    static extern void WTSFreeMemory(IntPtr memory);

    static void Log(string msg)
    {
        try { File.AppendAllText(logPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg + Environment.NewLine); }
        catch { }
    }

    // Is your session locked? Windows' session flags (WTSSessionInfoEx: 0 locked, 1 unlocked), and the lock and
    // unlock events seen since the window opened. Either one saying locked is enough.
    static bool Locked()
    {
        if (lockedByEvent) return true;
        IntPtr buf;
        int bytes;
        const int WTSSessionInfoEx = 25, WTS_CURRENT_SESSION = -1;
        if (!WTSQuerySessionInformationW(IntPtr.Zero, WTS_CURRENT_SESSION, WTSSessionInfoEx, out buf, out bytes))
            return false;
        try
        {
            // WTSINFOEXW: Level (4 bytes), padding to 8, then WTSINFOEX_LEVEL1_W: SessionId, SessionState, SessionFlags
            return bytes >= 20 && Marshal.ReadInt32(buf, 0) == 1 && Marshal.ReadInt32(buf, 16) == 0;
        }
        finally { WTSFreeMemory(buf); }
    }

    // Failed tries in the last hour, kept across restarts of this window.
    static List<DateTime> FailedTries()
    {
        var out_ = new List<DateTime>();
        try
        {
            if (File.Exists(triesPath))
                foreach (var line in File.ReadAllLines(triesPath))
                {
                    DateTime t;
                    if (DateTime.TryParse(line, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out t) &&
                        DateTime.UtcNow - t < TimeSpan.FromHours(1))
                        out_.Add(t);
                }
        }
        catch (Exception e) { Log("couldn't read the failed tries: " + e.Message); }
        return out_;
    }

    static void NoteFailedTry()
    {
        var tries = FailedTries();
        tries.Add(DateTime.UtcNow);
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(triesPath));
            var lines = new List<string>();
            foreach (var t in tries) lines.Add(t.ToString("o", CultureInfo.InvariantCulture));
            File.WriteAllLines(triesPath, lines.ToArray());
        }
        catch (Exception e) { Log("couldn't keep the failed try: " + e.Message); }
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
        }
        bool on;
        if (!WTSIsChildSessionsEnabled(out on) || !on)
        {
            Log("child sessions are off: run scripts\\workstation-setup.ps1 install as administrator");
            return 2;
        }

        Application.EnableVisualStyles();
        var form = new Form { Text = Title, ClientSize = new Size(width, height) };
        var rdp = new RdpControl { Dock = DockStyle.Fill };
        var menu = new MenuStrip { Dock = DockStyle.Top };
        var again = new ToolStripMenuItem("Connect now");
        menu.Items.Add(again);
        form.Controls.Add(rdp);
        form.Controls.Add(menu);
        form.MainMenuStrip = menu;
        rdp.BringToFront();  // the menu takes the top, the session the rest
        form.Show();
        rdp.CreateControl();
        dynamic ocx = rdp.Ocx;

        int last = -1;
        bool trying = false, upThisTry = false, waitingForYou = false, oneMore = false, seenBusy = false;
        DateTime nextTry = DateTime.Now, tryStarted = DateTime.Now, connectedAt = DateTime.Now;
        string why = "";

        SystemEvents.SessionSwitch += (s, e) =>
        {
            if (e.Reason == SessionSwitchReason.SessionLock) { lockedByEvent = true; Log("your session locked: no tries until it's unlocked"); }
            if (e.Reason == SessionSwitchReason.SessionUnlock)
            {
                lockedByEvent = false;
                waitingForYou = false;  // you're back: one more try is fine
                oneMore = true;
                nextTry = DateTime.Now.AddSeconds(5);
                Log("your session unlocked");
            }
        };
        again.Click += (s, e) => { waitingForYou = false; oneMore = true; nextTry = DateTime.Now; };

        Action connect = () =>
        {
            try
            {
                ocx.Server = "localhost";
                ocx.DesktopWidth = width;
                ocx.DesktopHeight = height;
                try { ocx.AdvancedSettings2.SmartSizing = true; } catch { }
                try { ocx.ColorDepth = 32; } catch { }
                try { ocx.AdvancedSettings7.EnableCredSspSupport = true; } catch { }
                object yes = true;
                ((IMsRdpExtendedSettings)rdp.Ocx).put_Property("ConnectToChildSession", ref yes);
                Log("connecting (Windows signs Ari in with your own sign-in)");
                trying = true;
                upThisTry = false;
                seenBusy = false;
                tryStarted = DateTime.Now;
                ocx.Connect();
            }
            catch (Exception e)
            {
                trying = false;
                nextTry = DateTime.Now.AddSeconds(AfterFailS);
                Log("couldn't connect: " + e.Message);
            }
        };

        var timer = new Timer { Interval = 2000 };
        timer.Tick += delegate
        {
            int state;
            try { state = (int)ocx.Connected; }  // 0 not connected, 1 connected, 2 connecting
            catch (Exception e) { Log("state: " + e.Message); return; }
            if (state != last)
            {
                Log("state " + (state == 1 ? "connected" : state == 2 ? "connecting" : "not connected"));
                if (state == 1) { upThisTry = true; connectedAt = DateTime.Now; }
                last = state;
            }
            // A try has ended when the control is back to "not connected" after showing "connecting" or "connected", or
            // 15 s after it started without showing either (a quick failure between two looks still counts as failed).
            if (trying && state == 0 && (seenBusy || (DateTime.Now - tryStarted).TotalSeconds > 15))
            {
                trying = false;
                // "Connected" is only the connection: a sign-in that then fails drops it within about 30 s. Up for
                // 2 minutes or more means Ari was in and it dropped later: try again soon, it doesn't count.
                if (upThisTry && (DateTime.Now - connectedAt).TotalSeconds >= 120) nextTry = DateTime.Now.AddSeconds(AfterDropS);
                else
                {
                    NoteFailedTry();  // the sign-in failed: Windows counts it
                    nextTry = DateTime.Now.AddSeconds(AfterFailS);
                    Log("the sign-in failed (" + FailedTries().Count + " in the last hour, at most " + MaxFailedPerHour + ")");
                }
            }
            if (trying && state != 0) seenBusy = true;
            if (trying) { form.Text = state == 1 ? Title : Title + " (connecting...)"; return; }
            if (state != 0) { form.Text = Title; return; }  // connected (a try that already counted as up)

            if (Locked()) why = "your PC is locked";
            else if (waitingForYou) why = "Windows didn't sign Ari in; Connect now tries again";
            else if (!oneMore && FailedTries().Count >= MaxFailedPerHour)
            {
                why = "Windows didn't sign Ari in; Connect now tries again";
                if (!waitingForYou) Log("stopped: " + MaxFailedPerHour + " failed sign-ins in the last hour");
                waitingForYou = true;
            }
            else if (DateTime.Now < nextTry) why = "trying again soon";
            else { why = ""; oneMore = false; connect(); }
            form.Text = Title + " (not connected" + (why != "" ? ": " + why : "") + ")";
        };
        timer.Start();
        Application.Run(form);
        return 0;
    }
}
