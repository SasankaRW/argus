# Argus for Android

Helios on your phone (the same pages as on the PC), your **notifications** (approvals, failures, reminders, summaries:
they arrive with the app closed), and the phone tools Ari can use: battery and network, ring it loudly, volume,
media, Do Not Disturb, the torch, a timer, open an app or a link, and, with phone control on, press Back / Home, lock,
read the screen, tap and type (plugin `phone`). No libraries; it only talks to your own Argus (outgoing), over
Tailscale. Nothing goes through ntfy, Google or any other outside service.

## Build and install (once, about 15 minutes)

1. Install Android Studio on the PC (winget install Google.AndroidStudio) and open it once so it downloads the SDK.
2. File > Open > `G:\Projects\argus\android`. Let it sync (it downloads Gradle and the Android build tools; first
   time takes a few minutes). If it asks which Gradle to use, pick the wrapper (gradle-wrapper.properties).
3. On the phone: Settings > About phone > tap "Build number" 7 times; then Developer options > USB debugging on.
   Plug it into the PC, allow the PC.
4. In Android Studio pick the phone at the top and press Run (the green triangle). The app installs and opens.
5. In the app: Argus address (the PC now, e.g. `http://sas-pc:8600`, or the https address from `tailscale serve`)
   and the worker token (`ARGUS_WORKER_TOKEN` in G:\Projects\argus\.env). Connect.
6. Phone permissions (in the same page, tap each, they open Android's own setting):
   - **Show notifications** and **Run in the background (no battery limit)**: both, so notifications keep coming
     with the app closed and in deep sleep.
   - **Phone control (Accessibility > Argus)**: so Ari can press buttons, read the screen, tap and type. Android 13+
     may grey it out for an app installed outside the Play Store: App info > ⋮ > Allow restricted settings, then
     try again. It only acts when you ask Ari; tapping and typing always ask you first. Password fields are never read.
   - Do Not Disturb control, Open apps and links, Microphone.
   - **Send me a test notification** checks the whole path.
7. In argus.yaml add `phone` to `plugins.live` when you want it to act (until then it says what it would do).

To change the address later (the laptop move): long-press the Argus icon > Connection.

## What runs

- Helios full screen (WebView). Links to other sites open in your browser.
- A small always-on connection (a silent notification "Argus · connected") with two loops, both outgoing only:
  the **inbox** (asks Argus for notifications, waiting up to 25 s, shows them, acknowledges them) and the **worker**
  (capability `phone`; runs only the `phone` plugin's jobs). It restarts after a reboot, after an app update, and
  every 15 minutes if Android stopped it. When the phone is off or off Tailscale, messages wait on the PC
  (`notify.keep_hours`, 48 h) and arrive when it is back.
- Notification buttons (Approve / Reject) call your Argus with the one-time link, then clear the notification.
- Opening apps, links and timers from the background needs "display over other apps"; without it, Ari's request
  shows as a notification you tap.
