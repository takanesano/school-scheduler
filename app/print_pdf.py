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
            f"{e['teacher_name']}先生 {e['room_name']}"
            for e in slot["entries"]]


def _teacher_lines(slot: dict) -> list[str]:
    return [f"{_slot_prefix(slot)}{e['subject_name']} "
            f"{e['student_name']}さん {e['room_name']}"
            for e in slot["entries"]]


def _overview_lines(slot: dict) -> list[str]:
    out = []
    for t in slot["entries"]:
        parts = [f"{l['student_name']}({l['subject_name']})"
                 for l in t["lessons"]]
        pupils = "、".join(parts)
        out.append(f"{_slot_prefix(slot)}{t['teacher_name']}: {pupils}")
    return out


def term_label(view: dict) -> str:
    """First–last in-term date across the grid, e.g. '7/21〜8/31'."""
    dates = [c["date"] for w in view["weeks"] for c in w if c["in_term"]]
    if not dates:
        return "—"
    return f"{_fmt_date(min(dates))}〜{_fmt_date(max(dates))}"


def overview_pdf(view: dict, generated_at: str) -> bytes:
    pdf = _HandoutPDF(term_label(view), generated_at)
    _grid_pages(pdf, view, "時間割 全体表", _overview_lines)
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
        _grid_pages(pdf, v, f"時間割(講師用) {v['teacher_name']} 先生",
                    _teacher_lines)
    return bytes(pdf.output())
