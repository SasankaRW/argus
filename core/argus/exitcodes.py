"""Exit codes a child process uses to talk to its supervisor (argus.supervisor, or systemd's Restart=always)."""

EXIT_RESTART = 75  # "start me again now": not a crash, so no waiting and not counted (a worker told to restart in
# Helios, or giving the GPU back for game mode)
