"""A4-landscape PDF handouts (Japanese labels) from the calendar views.

Pure layout module: functions here consume the JSON-ready week-grid
structures built by ``app.views`` (overview / student / teacher shapes)
plus explicit metadata, and return PDF bytes. No DB, no FastAPI — the
endpoints in ``app.main`` do the loading and pass everything in.

Every page carries the same self-identifying footer: the term span, the
generation timestamp and page n/N — so after a re-generate, stale paper
copies are recognizable at a glance.
"""
from __future__ import annotations

import colorsys
import datetime as dt
from pathlib import Path

from fpdf import FPDF

FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" \
    / "NotoSansJP-Regular.ttf"

WEEKDAY_JA = {"Mon": "月", "Tue": "火", "Wed": "水", "Thu": "木",
              "Fri": "金", "Sat": "土", "Sun": "日"}

# page geometry (mm, A4 landscape)
MARGIN = 9.0
PAGE_W, PAGE_H = 297.0, 210.0
GRID_W = PAGE_W - 2 * MARGIN
COL_W = GRID_W / 7
HEADER_H = 12.0          # title band at the top of each page
WDAY_H = 5.0             # weekday header row of each grid chunk
FOOTER_H = 8.0
DATE_H = 3.6             # date line inside a cell
LINE_H = 2.9             # one content line inside a cell
CELL_PAD = 0.8
SIZE_TITLE = 12.5
SIZE_DATE = 7.0
SIZE_BODY = 6.2
SIZE_FOOTER = 7.0


def teacher_fill_rgb(teacher_id: str) -> tuple[int, int, int]:
    """Light tint of the teacher's UI color — SAME hash as app.js
    ``teacherColor`` (31-hash of the id, golden-angle hue, 62% sat), so
    the paper handouts match the on-screen timetable colors. Lightness
    is raised to 87% to keep black text readable on the fill."""
    h = 0
    for ch in str(teacher_id):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    hue = (h * 137.508) % 360
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, 0.87, 0.62)
    return round(r * 255), round(g * 255), round(b * 255)


def _circled(period: int) -> str:
    """①②… for periods 1-20 (falls back to plain text beyond that)."""
    if 1 <= period <= 20:
        return chr(0x2460 + period - 1)
    return f"P{period}"


def _fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.month}/{d.day}"


class _HandoutPDF(FPDF):
    """FPDF with the shared title header and footer on every page."""

    def __init__(self, term_label: str, generated_at: str):
        super().__init__(orientation="L", format="A4")
        self.term_label = term_label
        self.generated_at = generated_at
        self.page_title = ""
        self.add_font("noto", style="", fname=str(FONT_PATH))
        self.set_auto_page_break(False)
        self.set_margins(MARGIN, MARGIN)
        self.alias_nb_pages()

    def header(self):
        self.set_font("noto", size=SIZE_TITLE)
        self.set_text_color(0)
        self.set_xy(MARGIN, MARGIN)
        self.cell(GRID_W, 6.5, self.page_title)
        self.set_draw_color(120)
        self.line(MARGIN, MARGIN + 8.0, PAGE_W - MARGIN, MARGIN + 8.0)

    def footer(self):
        self.set_y(PAGE_H - FOOTER_H)
        self.set_font("noto", size=SIZE_FOOTER)
        self.set_text_color(90)
        left = f"期間 {self.term_label} ・ 作成 {self.generated_at}"
        self.cell(GRID_W / 2, 5, left)
        self.cell(GRID_W / 2, 5, f"{self.page_no()} / {{nb}} ページ",
                  align="R")


def _wrap(pdf: FPDF, text: str, width: float) -> list[str]:
    """Greedy character wrap (Japanese has no spaces to break on)."""
    out, cur = [], ""
    for ch in text:
        if pdf.get_string_width(cur + ch) > width and cur:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _cell_lines(pdf: FPDF, cell: dict,
                entry_lines) -> list[str]:
    """Wrapped content lines for one day cell."""
    pdf.set_font("noto", size=SIZE_BODY)
    lines: list[str] = []
    for slot in cell["slots"]:
        for raw in entry_lines(slot):
            lines.extend(_wrap(pdf, raw, COL_W - 2 * CELL_PAD - 0.6))
    return lines


def _grid_pages(pdf: _HandoutPDF, view: dict, title: str,
                entry_lines) -> None:
    """Render one calendar (week rows × Mon-Sun) over as many pages as
    needed. ``entry_lines(slot_cell) -> list[str]`` turns one timeslot
    cell into compact text lines."""
    pdf.page_title = title
    pdf.add_page()
    top = MARGIN + HEADER_H
    bottom = PAGE_H - FOOTER_H - 2.0
    y = top

    def weekday_header(y0: float) -> float:
        pdf.set_font("noto", size=SIZE_DATE)
        pdf.set_draw_color(0)
        for i, wd in enumerate("月火水木金土日"):
            pdf.set_xy(MARGIN + i * COL_W, y0)
            pdf.set_fill_color(235)
            pdf.set_text_color(0)
            pdf.cell(COL_W, WDAY_H, wd, border=1, align="C", fill=True)
        return y0 + WDAY_H

    y = weekday_header(y)
    for week in view["weeks"]:
        cells = [(c, _cell_lines(pdf, c, entry_lines)) for c in week]
        row_h = max(DATE_H + len(ls) * LINE_H + 2 * CELL_PAD
                    for (_c, ls) in cells)
        row_h = max(row_h, 8.0)
        if y + row_h > bottom:          # week doesn't fit: next page
            pdf.add_page()
            y = weekday_header(top)
        for i, (cell, lines) in enumerate(cells):
            x = MARGIN + i * COL_W
            pdf.set_draw_color(0)
            if not cell["in_term"]:
                pdf.set_fill_color(245)
                pdf.rect(x, y, COL_W, row_h, style="DF")
            else:
                pdf.rect(x, y, COL_W, row_h)
            # date line — Sunday red, Saturday blue (Japanese convention)
            pdf.set_font("noto", size=SIZE_DATE)
            wd = cell["weekday"]
            if wd == "Sun":
                pdf.set_text_color(190, 30, 30)
            elif wd == "Sat":
                pdf.set_text_color(30, 60, 190)
            else:
                pdf.set_text_color(0)
            if not cell["in_term"]:
                pdf.set_text_color(150)
            pdf.set_xy(x + CELL_PAD, y + CELL_PAD)
            pdf.cell(COL_W - 2 * CELL_PAD, DATE_H, _fmt_date(cell["date"]))
            # content lines
            pdf.set_font("noto", size=SIZE_BODY)
            pdf.set_text_color(0)
            cy = y + CELL_PAD + DATE_H
            for line in lines:
                pdf.set_xy(x + CELL_PAD, cy)
                pdf.cell(COL_W - 2 * CELL_PAD, LINE_H, line)
                cy += LINE_H
        y += row_h


def clip_view(view: dict, date_from: str | None,
              date_to: str | None) -> dict:
    """Restrict a week-grid view to [date_from, date_to] (ISO, either
    side optional): out-of-range days lose their slots and in_term flag;
    weeks entirely out of range are dropped."""
    if not date_from and not date_to:
        return view
    weeks = []
    for week in view["weeks"]:
        clipped = []
        any_in = False
        for cell in week:
            d = cell["date"]
            if (date_from and d < date_from) or (date_to and d > date_to):
                cell = {**cell, "in_term": False, "slots": []}
            elif cell["in_term"]:
                any_in = True
            clipped.append(cell)
        if any_in:
            weeks.append(clipped)
    return {**view, "weeks": weeks}


def _slot_prefix(slot: dict) -> str:
    label = (slot.get("label") or "").strip()
    p = _circled(slot["period"])
    return f"{p}{label} " if label else f"{p} "


def _student_lines(slot: dict) -> list[str]:
    return [f"{_slot_prefix(slot)}{e['subject_name']} "
            f"{e['teacher_name']} {e['room_name']}"
            for e in slot["entries"]]


def _teacher_lines(slot: dict) -> list[str]:
    return [f"{_slot_prefix(slot)}{e['subject_name']} "
            f"{e['student_name']}さん {e['room_name']}"
            for e in slot["entries"]]


def term_label(view: dict) -> str:
    """First–last in-term date across the grid, e.g. '7/21〜8/31'."""
    dates = [c["date"] for w in view["weeks"] for c in w if c["in_term"]]
    if not dates:
        return "—"
    return f"{_fmt_date(min(dates))}〜{_fmt_date(max(dates))}"


def _fit_text(pdf: FPDF, text: str, width: float, size: float,
              min_size: float = 4.2) -> float:
    """Largest font size ≤ ``size`` at which ``text`` fits ``width``;
    the caller truncates if even ``min_size`` is too big."""
    s = size
    while s > min_size:
        pdf.set_font("noto", size=s)
        if pdf.get_string_width(text) <= width:
            return s
        s -= 0.4
    pdf.set_font("noto", size=min_size)
    return min_size


def _truncated(pdf: FPDF, text: str, width: float) -> str:
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "…") > width:
        text = text[:-1]
    return text + "…"


# transposed master table: one ROW per day, one COLUMN per period. A
# day row is divided into teacher LANES — every teacher working that
# day owns ONE two-sub-row band across ALL the day's periods (name on
# both rows, ONE student per sub-row, second row empty for a single
# student; the band stays blank in periods the teacher is off). Bands
# are tinted with the teacher's UI color. Subjects/rooms not shown.
DATE_COL_W = 21.0
SUB_H = 3.0              # height of one student sub-row (mm)
NAME_FRAC = 0.34         # teacher-name part of a period cell


def overview_pdf(view: dict, generated_at: str) -> bytes:
    pdf = _HandoutPDF(term_label(view), generated_at)
    pdf.page_title = "時間割 全体表"
    days = [c for w in view["weeks"] for c in w if c["in_term"]]
    periods = view["periods"]
    if not days or not periods:
        pdf.add_page()
        pdf.set_font("noto", size=SIZE_BODY)
        pdf.set_xy(MARGIN, MARGIN + HEADER_H)
        pdf.cell(GRID_W, 5, "時間割はまだありません")
        return bytes(pdf.output())

    # each teacher lane is `rows_per` sub-rows — 2 by design (one
    # student per row, capacity 2), stretched only if some teacher ever
    # has more; a day's height = its number of working teachers
    rows_per = max([2] + [len(t["lessons"])
                          for d in days for s in d["slots"]
                          for t in s["entries"]])

    def day_teachers(day: dict) -> list[tuple[str, str]]:
        """(id, name) of every teacher working that day, sorted by id —
        each owns one lane across all of the day's periods."""
        seen: dict[str, str] = {}
        for s in day["slots"]:
            for t in s["entries"]:
                seen.setdefault(t.get("teacher_id", t["teacher_name"]),
                                t["teacher_name"])
        return sorted(seen.items())
    # a representative time label per period (first seen)
    label_of: dict[int, str] = {}
    for d in days:
        for s in d["slots"]:
            if s["label"] and s["period"] not in label_of:
                label_of[s["period"]] = s["label"]

    per_w = (GRID_W - DATE_COL_W) / len(periods)
    name_w = per_w * NAME_FRAC
    block_h = rows_per * SUB_H
    top = MARGIN + HEADER_H
    bottom = PAGE_H - FOOTER_H - 2.0

    def table_header(y0: float) -> float:
        pdf.set_font("noto", size=SIZE_DATE)
        pdf.set_text_color(0)
        pdf.set_draw_color(0)
        pdf.set_fill_color(235)
        pdf.set_xy(MARGIN, y0)
        pdf.cell(DATE_COL_W, WDAY_H, "日付", border=1, align="C", fill=True)
        for i, p in enumerate(periods):
            head = _circled(p)
            if label_of.get(p):
                head += f" {label_of[p]}"
            pdf.set_xy(MARGIN + DATE_COL_W + i * per_w, y0)
            pdf.cell(per_w, WDAY_H, head, border=1, align="C", fill=True)
        return y0 + WDAY_H

    pdf.add_page()
    y = table_header(top)
    for day in days:
        lanes = day_teachers(day)
        row_h = max(1, len(lanes)) * block_h
        if y + row_h > bottom:
            pdf.add_page()
            y = table_header(top)
        # date cell (weekday colored the Japanese way)
        d = dt.date.fromisoformat(day["date"])
        pdf.set_draw_color(0)
        pdf.rect(MARGIN, y, DATE_COL_W, row_h)
        wd = day["weekday"]
        if wd == "Sun":
            pdf.set_text_color(190, 30, 30)
        elif wd == "Sat":
            pdf.set_text_color(30, 60, 190)
        else:
            pdf.set_text_color(0)
        pdf.set_font("noto", size=SIZE_DATE)
        pdf.set_xy(MARGIN + 1, y + row_h / 2 - 2)
        pdf.cell(DATE_COL_W - 2, 4,
                 f"{d.month}/{d.day}({WEEKDAY_JA[wd]})")
        slots = {s["period"]: s for s in day["slots"]}
        for i, p in enumerate(periods):
            x = MARGIN + DATE_COL_W + i * per_w
            slot = slots.get(p)
            if slot is None:                    # no such period this day
                pdf.set_fill_color(240)
                pdf.rect(x, y, per_w, row_h, style="DF")
                continue
            pdf.rect(x, y, per_w, row_h)
            # this slot's entries by teacher id — placed into the
            # day-wide lane of that teacher, blank where they are off
            here = {t.get("teacher_id", t["teacher_name"]): t
                    for t in slot["entries"]}
            for k, (tid, _tname) in enumerate(lanes):
                if tid in here:
                    by = y + k * block_h
                    pdf.set_fill_color(*teacher_fill_rgb(tid))
                    pdf.rect(x + 0.1, by + 0.1,
                             per_w - 0.2, block_h - 0.2, style="F")
            # light separators: one per student sub-row, plus the
            # name|student split
            pdf.set_draw_color(200)
            for k in range(1, max(1, len(lanes)) * rows_per):
                pdf.line(x, y + k * SUB_H, x + per_w, y + k * SUB_H)
            pdf.line(x + name_w, y, x + name_w, y + row_h)
            pdf.set_draw_color(0)
            for k, (tid, tname) in enumerate(lanes):
                t = here.get(tid)
                if t is None:
                    continue               # teacher off this period
                pupils = [l["student_name"] for l in t["lessons"]]
                for j in range(rows_per):
                    sy = y + k * block_h + j * SUB_H
                    # the teacher's name on EVERY row of their block
                    pdf.set_text_color(0)
                    size = _fit_text(pdf, tname, name_w - 1.2, SIZE_BODY)
                    pdf.set_font("noto", size=size)
                    pdf.set_xy(x + 0.6, sy)
                    pdf.cell(name_w - 1.2, SUB_H,
                             _truncated(pdf, tname, name_w - 1.2))
                    if j < len(pupils):        # one student per row
                        pupil = pupils[j]
                        size = _fit_text(pdf, pupil,
                                         per_w - name_w - 1.2, SIZE_BODY)
                        pdf.set_font("noto", size=size)
                        pdf.set_xy(x + name_w + 0.6, sy)
                        pdf.cell(per_w - name_w - 1.2, SUB_H,
                                 _truncated(pdf, pupil,
                                            per_w - name_w - 1.2))
        y += row_h
    return bytes(pdf.output())


def students_pdf(views: list[dict], generated_at: str) -> bytes:
    """One document, one calendar per student (each starts a new page)."""
    if not views:
        raise ValueError("no students to print")
    pdf = _HandoutPDF(term_label(views[0]), generated_at)
    for v in views:
        pdf.term_label = term_label(v)
        _grid_pages(pdf, v, f"時間割(生徒用) {v['student_name']} さん",
                    _student_lines)
    return bytes(pdf.output())


def teachers_pdf(views: list[dict], generated_at: str) -> bytes:
    """One document, one calendar per teacher (each starts a new page)."""
    if not views:
        raise ValueError("no teachers to print")
    pdf = _HandoutPDF(term_label(views[0]), generated_at)
    for v in views:
        pdf.term_label = term_label(v)
        _grid_pages(pdf, v, f"時間割(講師用) {v['teacher_name']}",
                    _teacher_lines)
    return bytes(pdf.output())
