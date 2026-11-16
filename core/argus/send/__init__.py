"""Argus Send: send files between my devices (contract: docs/send-protocol.md).

An agent on each device receives on TCP 8610, sends from a disk queue, picks LAN or Tailscale per request and
keeps working when Argus is down. Argus only helps with pairing, fresh addresses and shared history.
"""

PORT = 8610
CHUNK = 8 * 1024 * 1024
SMALL = 1024 * 1024  # files under this are one chunk each and may go in parallel
SESSION_SECONDS = 600
