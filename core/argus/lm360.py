"""The DeepCool LM360 cooler's screen: 320x240, raw RGB565 frames over USB (VID 3633, PID 0026).

Each frame is a 13-byte header and 153,600 bytes of pixels (RGB565, little-endian, rows top to bottom). The
protocol bytes are from daedlock/deepcool-lm (MIT), as used by rifaniponk/LM360-custom-driver on Windows. Windows'
own driver works through pyusb + libusb-package (pip install -e .[eyes]); measured at about 40 frames a second.
Only one program can hold the screen: DeepCool's app must be closed.

`Screen` never raises: a missing cooler, a screen another program holds or a USB error just make `ok` False, and
the caller tries `open()` again later.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("argus.lm360")

VID, PID = 0x3633, 0x0026
EP_OUT = 0x01
W, H = 320, 240
FRAME_BYTES = W * H * 2
INIT = bytes.fromhex("aa0100092991")
HEADER = bytes.fromhex("aa08000001005802002c01bc11")
BRIGHTER = bytes.fromhex("aa040006036100d246")
DIMMER = bytes.fromhex("aa040006031d00e60b")
BLACK = bytes(FRAME_BYTES)


class Screen:
    def __init__(self, usb: Any = None):
        self._usb = usb  # tests pass a fake; None: pyusb, loaded on first open()
        self.dev: Any = None
        self.why = ""  # why the last open() failed, for one log line

    @property
    def ok(self) -> bool:
        return self.dev is not None

    def _modules(self) -> Any:
        if self._usb is None:
            import usb.core  # type: ignore[import-not-found]
            import usb.util  # type: ignore[import-not-found]

            self._usb = usb
        return self._usb

    def open(self) -> bool:
        if self.dev is not None:
            return True
        try:
            usb = self._modules()
        except ImportError:
            self.why = "pyusb is missing: pip install -e .[eyes]"
            return False
        backend = None
        try:
            import libusb_package  # type: ignore[import-not-found]

            backend = libusb_package.get_libusb1_backend()
        except Exception:  # noqa: BLE001 - not installed: pyusb looks for a system libusb
            pass
        try:
            dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
            if dev is None:
                self.why = "the cooler's screen isn't connected (USB 3633:0026)"
                return False
            try:
                dev.set_configuration()
            except usb.core.USBError:
                pass  # usually already configured
            usb.util.claim_interface(dev, 0)
            dev.write(EP_OUT, INIT, timeout=5000)
        except Exception as e:  # noqa: BLE001 - USBError, a busy screen (DeepCool's app), a driver problem
            self.why = f"couldn't take the screen ({str(e)[:120]}); is DeepCool's app running?"
            return False
        self.dev = dev
        self.why = ""
        return True

    def _write(self, data: bytes) -> bool:
        if self.dev is None:
            return False
        try:
            self.dev.write(EP_OUT, data, timeout=5000)
            return True
        except Exception as e:  # noqa: BLE001 - unplugged, or taken by another program
            log.info("lost the screen", extra={"error": str(e)[:120]})
            self.close()
            return False

    def frame(self, pixels: bytes) -> bool:
        """Show one frame (FRAME_BYTES of RGB565). False when the screen is gone.

        The header and the pixels must always go together: a header without its 153,600 bytes leaves the screen
        counting the next program's bytes as pixels, and it stays black until the PC loses power. So a caller
        must never be killed between the two (the player stops between frames; see eyes.py)."""
        if not self._write(HEADER):
            return False
        if self._write(pixels):
            return True
        log.warning("a frame was cut short: if the screen stays black, try `python -m argus.eyes unstick`")
        return False

    def unstick(self) -> bool:
        """Try to bring a screen back that's waiting for the rest of a cut-short frame: a frame's worth of zero
        bytes finishes whatever it is waiting for, then INIT again. Only on request (`eyes unstick`)."""
        return self._write(BLACK) and self._write(INIT)

    def brightness(self, up: bool, steps: int = 1) -> bool:
        return all(self._write(BRIGHTER if up else DIMMER) for _ in range(max(0, steps)))

    def close(self) -> None:
        dev, self.dev = self.dev, None
        if dev is None:
            return
        try:
            self._modules().util.dispose_resources(dev)
        except Exception:  # noqa: BLE001
            pass


def to_rgb565(img: Any) -> bytes:
    """A Pillow image as one screen frame (resized to 320x240). Needs numpy; only used when making clips."""
    import numpy as np

    a = np.asarray(img.convert("RGB").resize((W, H)), dtype=np.uint32)
    v = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
    return v.astype("<u2").tobytes()
