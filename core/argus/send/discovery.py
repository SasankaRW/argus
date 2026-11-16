"""LAN discovery: announce this agent as _argus-send._tcp and browse for the others (TXT id=<device id>)."""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable

log = logging.getLogger("argus.send.mdns")
SERVICE = "_argus-send._tcp.local."


def local_addresses() -> set[str]:
    """This machine's IPv4 addresses; a change means the network changed."""
    import ifaddr

    out: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                out.add(ip.ip)
    return out


class Discovery:
    def __init__(self, device_id: str, port: int, on_change: Callable[[], None], enabled: bool = True):
        self.device_id, self.port, self.on_change, self.enabled = device_id, port, on_change, enabled
        self._found: dict[str, dict[str, str]] = {}  # device id -> {service name: ip:port}
        self._lock = threading.Lock()
        self._zc = None
        self._info = None

    def addresses(self, device_id: str) -> list[str]:
        with self._lock:
            return list(self._found.get(device_id, {}).values())

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

            self._zc = Zeroconf()
            ips = sorted(local_addresses())
            if ips:
                self._info = ServiceInfo(SERVICE, f"{self.device_id}.{SERVICE}", port=self.port,
                                         addresses=[socket.inet_aton(i) for i in ips],
                                         properties={"id": self.device_id})
                self._zc.register_service(self._info)
            ServiceBrowser(self._zc, SERVICE, handlers=[self._handler])
        except Exception as e:  # noqa: BLE001  mDNS is a convenience; never stop the agent for it
            log.warning("mDNS is off: %s", e)
            self.stop()

    def _handler(self, zeroconf, service_type, name, state_change) -> None:
        from zeroconf import ServiceStateChange

        def resolve() -> None:
            if state_change is ServiceStateChange.Removed:
                with self._lock:
                    for found in self._found.values():
                        found.pop(name, None)
                return
            info = zeroconf.get_service_info(service_type, name, timeout=3000)
            if not info:
                return
            did = (info.properties.get(b"id") or b"").decode(errors="ignore")
            if not did or did == self.device_id:
                return
            addrs = [f"{a}:{info.port}" for a in info.parsed_addresses() if not a.startswith("169.254.")]
            if addrs:
                with self._lock:
                    self._found.setdefault(did, {})[name] = addrs[0]
                self.on_change()  # an announce of the target is a retry trigger

        threading.Thread(target=resolve, daemon=True).start()

    def stop(self) -> None:
        zc, self._zc = self._zc, None
        if zc:
            try:
                if self._info:
                    zc.unregister_service(self._info)
                zc.close()
            except Exception:  # noqa: BLE001
                pass
