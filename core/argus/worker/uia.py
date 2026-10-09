"""An app's window as a surface for Ari's task loop, through Windows UI Automation (P7 part 3).

The "page" is the window's controls: buttons, fields, lists, checkboxes, tabs, each with a number. Ari presses and
fills them through UI Automation patterns (Invoke, Value, Toggle, SelectionItem, ExpandCollapse). That needs no mouse
and no keyboard focus, so it works on Ari's Workstation while you type elsewhere, and in your own window ("fill this
form") without moving your cursor.

A control that offers none of those patterns is clicked with window messages at its centre (`winmsg`); the result
check catches the apps that ignore them. A window that lists almost no controls is looked at instead
(`VisionSurface`): its picture with numbered boxes goes to the local vision model (V1), never to Claude, and is
never saved.

Needs the `uiautomation` package (pip install -e .[plugins]); Windows only.
"""

from __future__ import annotations

from typing import Any

KINDS = {"Button": "button", "Edit": "textbox", "ComboBox": "combobox", "CheckBox": "checkbox",
         "RadioButton": "radio", "ListItem": "option", "MenuItem": "menuitem", "TabItem": "tab",
         "Hyperlink": "link", "SplitButton": "button", "TreeItem": "treeitem", "DataItem": "row",
         "Document": "textbox", "Spinner": "spinner", "Slider": "slider"}
MAX_ITEMS = 120


class UiaSurface:  # pragma: no cover - Windows UI Automation
    """One window. `snapshot()` numbers its controls; `act()` uses them (the numbers of the last snapshot)."""

    def __init__(self, hwnd: int):
        try:
            import uiautomation as auto
        except ImportError:
            raise RuntimeError("working in apps needs uiautomation: pip install -e .[plugins]") from None
        self.auto = auto
        self.init = auto.UIAutomationInitializerInThread()  # COM for this thread
        self.root = auto.ControlFromHandle(hwnd)
        if self.root is None:
            raise RuntimeError("that window is gone")
        self.hwnd = hwnd
        self.controls: dict[int, Any] = {}
        self.boxes: dict[int, tuple[int, int, int, int]] = {}  # screen rectangles, for marks and message clicks
        self.around: Any = None  # the Workstation plugin: show the window for a real mouse click, then go back

    def _pattern(self, c: Any, name: str) -> Any:
        try:
            return c.GetPattern(getattr(self.auto.PatternId, name))
        except Exception:  # noqa: BLE001
            return None

    def snapshot(self) -> dict:
        self.controls, self.boxes = {}, {}
        items, text = [], []
        n = 0
        for c, _depth in self.auto.WalkControl(self.root, maxDepth=25):
            try:
                kind = c.ControlTypeName.removesuffix("Control")
                if c.IsOffscreen or not c.IsEnabled:
                    continue
                name = (c.Name or "").strip()
            except Exception:  # noqa: BLE001 - a control that went away meanwhile
                continue
            if kind == "Text" and name:
                text.append(name)
                continue
            role = KINDS.get(kind)
            if role is None:
                continue
            password = bool(getattr(c, "IsPassword", False))
            value = ""
            vp = self._pattern(c, "ValuePattern")
            if vp is not None and not password:
                try:
                    value = str(vp.Value or "")[:40]
                except Exception:  # noqa: BLE001
                    pass
            n += 1
            self.controls[n] = c
            try:
                r = c.BoundingRectangle
                self.boxes[n] = (r.left, r.top, r.right, r.bottom)
            except Exception:  # noqa: BLE001
                pass
            items.append({"n": n, "role": role, "name": name[:80] or (c.AutomationId or "")[:40],
                          "type": "password" if password else "", "value": value,
                          "field": (c.AutomationId or "")[:40], "auto": ""})
            if n >= MAX_ITEMS:
                break
        title = self.root.Name or ""
        return {"url": f"window: {title}", "title": title, "text": " · ".join(text)[:1500], "items": items}

    def goto(self, url: str, new_tab: bool = False) -> dict:
        raise RuntimeError("an app has no web address")

    def idle_seconds(self) -> float:
        from . import winmsg

        return winmsg.idle_seconds()

    def picture(self) -> bytes | None:
        """A small JPEG of the window, for when Ari is stuck and asks you."""
        from . import winmsg

        try:
            png, _ = winmsg.capture(self.hwnd)
        except Exception:  # noqa: BLE001
            return None
        return small_jpeg(png)

    def act(self, step: dict) -> dict:
        c = self.controls.get(step.get("n"))
        kind = step.get("do")
        try:
            if kind in ("click", "type", "select") and c is None:
                return {"ok": False, "error": "that control is gone: look again"}
            if kind == "click":
                for pat, call in (("InvokePattern", "Invoke"), ("TogglePattern", "Toggle"),
                                  ("SelectionItemPattern", "Select"), ("ExpandCollapsePattern", "Expand")):
                    p = self._pattern(c, pat)
                    if p is not None:
                        getattr(p, call)()
                        break
                else:  # no pattern: a click sent to the window as messages, at the control's centre
                    box = self.boxes.get(step["n"])
                    if box is None:
                        return {"ok": False, "error": "it can't be pressed without the mouse; try another control"}
                    from . import winmsg

                    winmsg.click(self.hwnd, (box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            elif kind == "type":
                p = self._pattern(c, "ValuePattern")
                if p is None or p.IsReadOnly:
                    return {"ok": False, "error": "that field can't be typed into this way"}
                p.SetValue(step.get("text") or "")
            elif kind == "select":
                want = (step.get("text") or "").strip().lower()
                ec = self._pattern(c, "ExpandCollapsePattern")
                if ec is not None:
                    ec.Expand()
                hit = None
                for item, _d in self.auto.WalkControl(c, maxDepth=4):
                    if (item.Name or "").strip().lower() == want:
                        hit = item
                        break
                if hit is not None and self._pattern(hit, "SelectionItemPattern") is not None:
                    self._pattern(hit, "SelectionItemPattern").Select()
                elif self._pattern(c, "ValuePattern") is not None:
                    self._pattern(c, "ValuePattern").SetValue(step.get("text") or "")
                else:
                    return {"ok": False, "error": f"no option {step.get('text')!r}"}
                if ec is not None:
                    try:
                        ec.Collapse()
                    except Exception:  # noqa: BLE001
                        pass
            elif kind == "mouse":
                box = self.boxes.get(step.get("n"))
                if box is None or self.around is None:
                    return {"ok": False, "error": "no place to click with the mouse"}
                from . import winmsg

                self.around(lambda: winmsg.real_click((box[0] + box[2]) // 2, (box[1] + box[3]) // 2))
            elif kind == "scroll":
                p = self._pattern(self.root, "ScrollPattern")
                if p is None:
                    return {"ok": False, "error": "this window doesn't scroll that way"}
                p.Scroll(0, 4)  # ScrollAmount.LargeIncrement, down
            else:
                return {"ok": False, "error": f"{kind} doesn't work in an app; use click, type or select"}
        except Exception as e:  # noqa: BLE001 - told to the model, which tries another way
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        return {"ok": True, "url": f"window: {self.root.Name or ''}", "title": self.root.Name or ""}


def small_jpeg(png: bytes, side: int = 720) -> bytes:
    """Under ~250 KB for an approval: shrunk, JPEG."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70)
    return out.getvalue()


class VisionSurface:  # pragma: no cover - Windows: the picture and the clicks
    """A window Ari looks at: its picture with numbered boxes (from UI Automation, when it lists any) for the vision
    model; clicks and typing go to the window as messages. `sees = True` makes the task loop send the picture."""

    sees = True

    def __init__(self, hwnd: int, uia: UiaSurface | None = None, max_side: int = 1280):
        self.hwnd, self.uia, self.max_side = hwnd, uia, max_side
        self._pic: bytes | None = None
        self.rect = (0, 0, 0, 0)
        self.scale = 1.0
        self.around: Any = None  # the Workstation plugin: show the window for a real mouse click, then go back

    def snapshot(self) -> dict:
        from . import winmsg
        from .marks import draw_marks, shrink, to_picture

        snap = self.uia.snapshot() if self.uia is not None else {"title": "", "text": "", "items": []}
        png, self.rect = winmsg.capture(self.hwnd)
        png, self.scale = shrink(png, self.max_side)
        boxes = []
        for it in snap["items"]:
            box = self.uia.boxes.get(it["n"]) if self.uia is not None else None
            if box:
                boxes.append({"n": it["n"], **to_picture(box, self.rect, self.scale)})
        self._pic = draw_marks(png, boxes)  # in memory for this step only
        import io

        from PIL import Image

        w, h = Image.open(io.BytesIO(self._pic)).size
        return {"url": f"window: {snap.get('title') or ''}", "title": snap.get("title") or "", "text": snap["text"],
                "items": snap["items"], "picture": {"width": w, "height": h}}

    def image(self) -> bytes:
        if self._pic is None:  # a job that came back after waiting: look again
            self.snapshot()
        return self._pic  # type: ignore[return-value]

    def goto(self, url: str, new_tab: bool = False) -> dict:
        raise RuntimeError("an app has no web address")

    def idle_seconds(self) -> float:
        from . import winmsg

        return winmsg.idle_seconds()

    def picture(self) -> bytes | None:
        return small_jpeg(self.image())

    def act(self, step: dict) -> dict:
        from . import winmsg
        from .marks import to_screen

        self._pic = None
        kind = step.get("do")
        try:
            if kind == "mouse":
                if self.around is None:
                    return {"ok": False, "error": "the mouse isn't available here"}
                if step.get("n") is not None and self.uia is not None and step["n"] in self.uia.boxes:
                    b = self.uia.boxes[step["n"]]
                    sx, sy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
                else:
                    sx, sy = to_screen(float(step.get("x") or 0), float(step.get("y") or 0), self.rect, self.scale)
                self.around(lambda: winmsg.real_click(sx, sy))
                return {"ok": True}
            if step.get("n") is not None and self.uia is not None and kind in ("click", "type", "select"):
                return self.uia.act(step)
            if kind == "click" and step.get("x") is not None and step.get("y") is not None:
                winmsg.click(self.hwnd, *to_screen(float(step["x"]), float(step["y"]), self.rect, self.scale))
            elif kind == "type":
                winmsg.type_text(self.hwnd, step.get("text") or "")
                if step.get("enter"):
                    winmsg.key(self.hwnd, winmsg.VK["enter"])
            elif kind == "press":
                vk = winmsg.VK.get((step.get("key") or "").replace(" ", "").lower())
                if vk is None:
                    return {"ok": False, "error": f"I can't press {step.get('key')!r} here"}
                winmsg.key(self.hwnd, vk)
            else:
                return {"ok": False, "error": f"{kind} doesn't work here: click a box (or x, y), type or press"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        return {"ok": True}
