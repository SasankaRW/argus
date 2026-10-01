# Argus for Android

Helios on your phone (the same pages as on the PC) plus the phone tools Ari can use: battery and network, ring it
loudly, Do Not Disturb, the torch, a timer, open an app or a link (plugin `phone`). No libraries; it only talks to
your own Argus (outgoing), over Tailscale.

## Build and install (once, about 15 minutes)

1. Install Android Studio on the PC (winget install Google.AndroidStudio) and open it once so it downloads the SDK.
2. File > Open > `G:\Projects\argus\android`. Let it sync (it downloads Gradle and the Android build tools; first
   time takes a few minutes). If it asks which Gradle to use, pick the wrapper (gradle-wrapper.properties).
3. On the phone: Settings > About phone > tap "Build number" 7 times; then Developer options > USB debugging on.
   Plug it into the PC, allow the PC.
4. In Android Studio pick the phone at the top and press Run (the green triangle). The app installs and opens.
5. In the app: Argus address (the PC now, e.g. `http://sas-pc:8600`, or the https address from `tailscale serve`)
   and the worker token (`ARGUS_WORKER_TOKEN` in G:\Projects\argus\.env). Connect.
6. Phone permissions (in the same page): Do Not Disturb control, Open apps and links, Microphone.
7. In argus.yaml add `phone` to `plugins.live` when you want it to act (until then it says what it would do).

To change the address later (the laptop move): long-press the Argus icon > Connection.

## What runs

- Helios full screen (WebView). Links to other sites open in your browser.
- A small always-on connection (a silent notification "Argus · connected"): the phone is a worker with the
  capability `phone` and runs only the `phone` plugin's jobs. It restarts after a reboot.
- Opening apps, links and timers from the background needs "display over other apps"; without it, Ari's request
  shows as a notification you tap.
