"""Custom-drawn pieces of the window: level meter, record button and the
scrolling mic list."""

import time
import tkinter as tk
from tkinter import ttk

BG = "#1c1c1c"
FG = "#fafafa"
DIM = "#9a9a9a"
FAINT = "#5a5a5a"
RED = "#e5484d"
RED_HOT = "#ff6369"
AMBER = "#ffb224"
GREEN = "#3ddc84"
YELLOW = "#ffd23f"
WARN = "#ff6b6b"
ACCENT = "#57c8ff"


def blend(color, into, amount):
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(into[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}"
                         for x, y in zip(a, b))


class Meter(tk.Canvas):
    """Segmented LED level meter with a peak-hold marker."""

    SEG, GAP, FLOOR = 5, 2, -60.0

    def __init__(self, parent, height=16):
        super().__init__(parent, height=height, bg=BG,
                         highlightthickness=0, width=200)
        self.lit_ids, self.colors = [], []
        self.lit = -1
        self.peak_db = self.FLOOR
        self.peak_time = 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def _color(self, i, n):
        db = self.FLOOR * (1 - (i + 0.5) / n)
        return RED if db > -3 else YELLOW if db > -12 else GREEN

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        n = max(10, (w + self.GAP) // (self.SEG + self.GAP))
        self.lit_ids, self.colors = [], []
        for i in range(n):
            x = i * (self.SEG + self.GAP)
            c = self._color(i, n)
            self.create_rectangle(x, 0, x + self.SEG, h, width=0,
                                  fill=blend(c, BG, 0.82))
            self.lit_ids.append(self.create_rectangle(
                x, 0, x + self.SEG, h, width=0, fill=c, state="hidden"))
            self.colors.append(c)
        self.peak_id = self.create_rectangle(0, 0, 0, h, width=0,
                                             fill=FG, state="hidden")
        self.lit = 0

    def set(self, db, active=True):
        n = len(self.lit_ids)
        if not n:
            return
        frac = min(1.0, max(0.0, (db - self.FLOOR) / -self.FLOOR))
        lit = int(round(frac * n)) if active else 0
        if lit != self.lit:
            lo, hi = sorted((lit, self.lit))
            for i in range(lo, hi):
                self.itemconfig(self.lit_ids[i],
                                state="normal" if i < lit else "hidden")
            self.lit = lit
        now = time.monotonic()
        if db >= self.peak_db or now - self.peak_time > 1.5:
            self.peak_db = db
            self.peak_time = now
        pfrac = (self.peak_db - self.FLOOR) / -self.FLOOR
        i = min(n - 1, int(pfrac * n))
        if active and pfrac > 0.02:
            x = i * (self.SEG + self.GAP)
            self.coords(self.peak_id, x, 0, x + self.SEG,
                        self.winfo_height())
            self.itemconfig(self.peak_id, state="normal",
                            fill=blend(self.colors[i], "#ffffff", 0.5))
        else:
            self.itemconfig(self.peak_id, state="hidden")


class RecordButton(tk.Canvas):
    """Big round record / stop button."""

    def __init__(self, parent, command, size=64):
        super().__init__(parent, width=size, height=size, bg=BG,
                         highlightthickness=0, cursor="hand2")
        self.size, self.command = size, command
        self.recording = self.hover = False
        self.pulse = 0
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, on):
        self.hover = on
        self._draw()

    def set_recording(self, on):
        self.recording = on
        self._draw()

    def tick(self, paused):
        if self.recording:
            self.pulse = (self.pulse + 1) % 40
            self._draw(paused)

    def _draw(self, paused=False):
        self.delete("all")
        s, pad = self.size, 3
        ring = "#3a3a3a"
        if self.recording:
            glow = abs(20 - self.pulse) / 20
            ring = AMBER if paused else blend(RED, "#3a3a3a", glow * 0.7)
        self.create_oval(pad, pad, s - pad, s - pad, outline=ring,
                         width=3)
        fill = RED_HOT if self.hover else RED
        if self.recording:
            q = s * 0.33
            self._round_rect(q, q, s - q, s - q, 5, fill)
        else:
            q = s * 0.24
            self.create_oval(q, q, s - q, s - q, fill=fill, width=0)

    def _round_rect(self, x1, y1, x2, y2, r, fill):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
               x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
               x1, y1 + r, x1, y1]
        self.create_polygon(pts, smooth=True, fill=fill, width=0)


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container for the mic cards."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.bar = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.win = self.canvas.create_window((0, 0), window=self.inner,
                                             anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self._refresh())
        self.canvas.bind("<Configure>", lambda e: (
            self.canvas.itemconfig(self.win, width=e.width),
            self._refresh()))
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", self._wheel)
        self.canvas.bind_all("<Button-5>", self._wheel)

    def _refresh(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        if needed and not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y", before=self.canvas)
        elif not needed and self.bar.winfo_ismapped():
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, event):
        w = event.widget
        while w is not None and w is not self:
            w = getattr(w, "master", None)
        if w is None or self.inner.winfo_reqheight() <= \
                self.canvas.winfo_height():
            return
        if getattr(event, "num", 0) == 4:
            step = -1
        elif getattr(event, "num", 0) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step * 2, "units")
