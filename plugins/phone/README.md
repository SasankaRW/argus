# Phone

No plugin.py: these workflows run inside the Argus app on your phone (`android/`), which connects to Argus as a
worker with the capability `phone`. Like every plugin it starts in dry-run (the app says what it would do); add
`phone` to `plugins.live` to let it act.

Phone control (press buttons, read the screen, tap, type) needs the Argus app's accessibility service: turn it on in
the app's setup page (Android Settings > Accessibility > Argus). Tapping and typing are `risky`: Ari asks you first.
Volume, media and lock need no extra permission.
