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
                cell_lines) -> list[str]:
    """Wrapped content lines for one day cell."""
    pdf.set_font("noto", size=SIZE_BODY)
    lines: list[str] = []
    for raw in cell_lines(cell):
        lines.extend(_wrap(pdf, raw, COL_W - 2 * CELL_PAD - 0.6))
    return lines


def _grid_pages(pdf: _HandoutPDF, view: dict, title: str,
                cell_lines) -> None:
    """Render one calendar (week rows × Mon-Sun) over as many pages as
    needed. ``cell_lines(day_cell) -> list[str]`` turns one day cell
    into compact text lines."""
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
        cells = [(c, _cell_lines(pdf, c, cell_lines)) for c in week]
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


def _student_cell_lines(cell: dict) -> list[str]:
    """Two rows per day: subjects on top, their periods (1限 style)
    below — comma-delimited, aligned by lesson order."""
    entries = [(s["period"], e)
               for s in cell["slots"] for e in s["entries"]]
    if not entries:
        return []
    entries.sort(key=lambda pe: pe[0])
    return ["、".join(e["subject_name"] for _p, e in entries),
            "、".join(f"{p}限" for p, _e in entries)]


# teacher handout: one MATRIX per week — rows are periods (1限 style),
# columns are the week's days, each cell lists the students the teacher
# takes in that slot. Weeks stack down the page.
PERIOD_COL_W = 22.0


def _teacher_matrix_pages(pdf: _HandoutPDF, view: dict,
                          title: str) -> None:
    pdf.page_title = title
    pdf.add_page()
    top = MARGIN + HEADER_H
    bottom = PAGE_H - FOOTER_H - 2.0
    periods = view["periods"]
    label_of: dict[int, str] = {}
    for w in view["weeks"]:
        for c in w:
            for s in c["slots"]:
                if s["label"] and s["period"] not in label_of:
                    label_of[s["period"]] = s["label"]
    day_w = (GRID_W - PERIOD_COL_W) / 7
    y = top
    first = True
    for week in view["weeks"]:
        # students per (period, day) and the wrapped cell lines
        pdf.set_font("noto", size=SIZE_BODY)
        grid: list[list[list[str]]] = []
        for p in periods:
            row = []
            for cell in week:
                names = "、".join(
                    e["student_name"]
                    for s in cell["slots"] if s["period"] == p
                    for e in s["entries"])
                row.append(_wrap(pdf, names, day_w - 1.6)
                           if names else [])
            grid.append(row)
        row_hs = [max(2 * CELL_PAD
                      + LINE_H * max([len(ls) for ls in row] + [1]), 6.0)
                  for row in grid]
        block_h = WDAY_H + sum(row_hs)
        if not first and y + block_h > bottom:
            pdf.add_page()
            y = top
        first = False
        # date header row (label column blank)
        pdf.set_draw_color(0)
        pdf.set_fill_color(235)
        pdf.set_xy(MARGIN, y)
        pdf.set_font("noto", size=SIZE_DATE)
        pdf.set_text_color(0)
        pdf.cell(PERIOD_COL_W, WDAY_H, "", border=1, fill=True)
        for i, cell in enumerate(week):
            d = dt.date.fromisoformat(cell["date"])
            wd = cell["weekday"]
            pdf.set_xy(MARGIN + PERIOD_COL_W + i * day_w, y)
            if wd == "Sun":
                pdf.set_text_color(190, 30, 30)
            elif wd == "Sat":
                pdf.set_text_color(30, 60, 190)
            else:
                pdf.set_text_color(0)
            if not cell["in_term"]:
                pdf.set_text_color(150)
            pdf.cell(day_w, WDAY_H,
                     f"{d.month}/{d.day}({WEEKDAY_JA[wd]})",
                     border=1, align="C", fill=True)
        ry = y + WDAY_H
        for pi, p in enumerate(periods):
            rh = row_hs[pi]
            # period label: 1限 (+ the time when known)
            pdf.set_draw_color(0)
            pdf.rect(MARGIN, ry, PERIOD_COL_W, rh)
            pdf.set_text_color(0)
            label = f"{p}限"
            if label_of.get(p):
                label += f" {label_of[p]}"
            size = _fit_text(pdf, label, PERIOD_COL_W - 1.6, SIZE_DATE)
            pdf.set_font("noto", size=size)
            pdf.set_xy(MARGIN + 0.8, ry)
            pdf.cell(PERIOD_COL_W - 1.6, rh,
                     _truncated(pdf, label, PERIOD_COL_W - 1.6))
            for i, cell in enumerate(week):
                x = MARGIN + PERIOD_COL_W + i * day_w
                has_slot = any(s["period"] == p for s in cell["slots"])
                if not cell["in_term"] or not has_slot:
                    pdf.set_fill_color(245)
                    pdf.rect(x, ry, day_w, rh, style="DF")
                    continue
                pdf.rect(x, ry, day_w, rh)
                pdf.set_font("noto", size=SIZE_BODY)
                pdf.set_text_color(0)
                cy = ry + CELL_PAD
                for line in grid[pi][i]:
                    pdf.set_xy(x + 0.8, cy)
                    pdf.cell(day_w - 1.6, LINE_H, line)
                    cy += LINE_H
            ry += rh
        y = ry + 2.5                      # small gap between week blocks


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


# transposed master table: one ROW per day, one COLUMN per period,
# ONE WEEK PER PAGE (sub-rows shrink to make the week fit). A day row
# is divided into teacher LANES — every teacher working that day owns
# ONE two-sub-row band across ALL the day's periods (student on the
# left, the teacher's name on the right of each sub-row; one student
# per row, second row empty for a single student; the band stays blank
# in periods the teacher is off). Bands are tinted with the teacher's
# UI color. Subjects/rooms not shown.
DATE_COL_W = 21.0
SUB_H = 3.0              # max height of one student sub-row (mm)
NAME_FRAC = 0.34         # teacher-name part of a period cell (right)


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

    weeks = [[c for c in w if c["in_term"]] for w in view["weeks"]]
    weeks = [w for w in weeks if w]
    for wdays in weeks:                    # ---- one page per week
        pdf.add_page()
        y = table_header(top)
        # shrink the sub-row height until the whole week fits the page
        units = sum(max(1, len(day_teachers(d))) for d in wdays) \
            * rows_per
        sub_h = min(SUB_H, (bottom - y) / units)
        block_h = rows_per * sub_h
        body_size = min(SIZE_BODY, sub_h * 2.1)
        for day in wdays:
            lanes = day_teachers(day)
            row_h = max(1, len(lanes)) * block_h
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
                if slot is None:                # no such period this day
                    pdf.set_fill_color(240)
                    pdf.rect(x, y, per_w, row_h, style="DF")
                    continue
                pdf.rect(x, y, per_w, row_h)
                # this slot's entries by teacher id — placed into the
                # day-wide lane of that teacher, blank where they're off
                here = {t.get("teacher_id", t["teacher_name"]): t
                        for t in slot["entries"]}
                for k, (tid, _tname) in enumerate(lanes):
                    if tid in here:
                        by = y + k * block_h
                        pdf.set_fill_color(*teacher_fill_rgb(tid))
                        pdf.rect(x + 0.1, by + 0.1,
                                 per_w - 0.2, block_h - 0.2, style="F")
                # light separators: one per student sub-row, plus the
                # student|name split (teacher name on the RIGHT)
                split_x = x + per_w - name_w
                pdf.set_draw_color(200)
                for k in range(1, max(1, len(lanes)) * rows_per):
                    pdf.line(x, y + k * sub_h, x + per_w, y + k * sub_h)
                pdf.line(split_x, y, split_x, y + row_h)
                pdf.set_draw_color(0)
                for k, (tid, tname) in enumerate(lanes):
                    t = here.get(tid)
                    if t is None:
                        continue           # teacher off this period
                    pupils = [l["student_name"] for l in t["lessons"]]
                    for j in range(rows_per):
                        sy = y + k * block_h + j * sub_h
                        # student on the left — one per row
                        if j < len(pupils):
                            pupil = pupils[j]
                            size = _fit_text(pdf, pupil,
                                             per_w - name_w - 1.2,
                                             body_size)
                            pdf.set_text_color(0)
                            pdf.set_font("noto", size=size)
                            pdf.set_xy(x + 0.6, sy)
                            pdf.cell(per_w - name_w - 1.2, sub_h,
                                     _truncated(pdf, pupil,
                                                per_w - name_w - 1.2))
                        # the teacher's name on the RIGHT of every row
                        pdf.set_text_color(0)
                        size = _fit_text(pdf, tname, name_w - 1.2,
                                         body_size)
                        pdf.set_font("noto", size=size)
                        pdf.set_xy(split_x + 0.6, sy)
                        pdf.cell(name_w - 1.2, sub_h,
                                 _truncated(pdf, tname, name_w - 1.2))
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
                    _student_cell_lines)
    return bytes(pdf.output())


def teachers_pdf(views: list[dict], generated_at: str) -> bytes:
    """One document, one week-matrix timetable per teacher (each
    starts a new page): period rows × day columns, students in the
    cells, one block per week."""
    if not views:
        raise ValueError("no teachers to print")
    pdf = _HandoutPDF(term_label(views[0]), generated_at)
    for v in views:
        pdf.term_label = term_label(v)
        _teacher_matrix_pages(pdf, v,
                              f"時間割(講師用) {v['teacher_name']}")
    return bytes(pdf.output())
