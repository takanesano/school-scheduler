"""Excel (.xlsx) export of the transposed master table.

Same layout as the PDF overview handout (`print_pdf.overview_pdf`):
one block of rows per in-term day, one column pair per period
(teacher | students), a fixed number of teacher sub-rows everywhere.
Pure module: consumes the views.py week-grid shape plus metadata and
returns the workbook bytes — no DB, no FastAPI.
"""
from __future__ import annotations

import datetime as dt
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .print_pdf import WEEKDAY_JA, _circled, term_label

THIN = Side(style="thin", color="BBBBBB")
MEDIUM = Side(style="medium", color="000000")
GREY = PatternFill("solid", fgColor="EEEEEE")
HEAD_FILL = PatternFill("solid", fgColor="E3E3E3")
SUN = Font(color="BE1E1E")
SAT = Font(color="1E3CBE")
BOLD = Font(bold=True)


def overview_xlsx(view: dict, generated_at: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "時間割 全体表"

    days = [c for w in view["weeks"] for c in w if c["in_term"]]
    periods = view["periods"]
    n_sub = max((len(s["entries"]) for d in days for s in d["slots"]),
                default=1) or 1
    label_of: dict[int, str] = {}
    for d in days:
        for s in d["slots"]:
            if s["label"] and s["period"] not in label_of:
                label_of[s["period"]] = s["label"]

    # ---- header row: 日付 | ① 09:00-10:10 (merged over 2 cols) | …
    ws.cell(row=1, column=1, value="日付").font = BOLD
    ws.cell(row=1, column=1).fill = HEAD_FILL
    ws.cell(row=1, column=1).alignment = Alignment(
        horizontal="center", vertical="center")
    for i, p in enumerate(periods):
        c0 = 2 + i * 2
        head = _circled(p)
        if label_of.get(p):
            head += f" {label_of[p]}"
        ws.merge_cells(start_row=1, start_column=c0,
                       end_row=1, end_column=c0 + 1)
        cell = ws.cell(row=1, column=c0, value=head)
        cell.font = BOLD
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # ---- one block of n_sub rows per day
    r = 2
    for day in days:
        d = dt.date.fromisoformat(day["date"])
        wd = day["weekday"]
        ws.merge_cells(start_row=r, start_column=1,
                       end_row=r + n_sub - 1, end_column=1)
        date_cell = ws.cell(row=r, column=1,
                            value=f"{d.month}/{d.day}({WEEKDAY_JA[wd]})")
        date_cell.alignment = Alignment(horizontal="center",
                                        vertical="center")
        if wd == "Sun":
            date_cell.font = SUN
        elif wd == "Sat":
            date_cell.font = SAT
        slots = {s["period"]: s for s in day["slots"]}
        for i, p in enumerate(periods):
            c0 = 2 + i * 2
            slot = slots.get(p)
            for k in range(n_sub):
                tcell = ws.cell(row=r + k, column=c0)
                scell = ws.cell(row=r + k, column=c0 + 1)
                if slot is None:
                    tcell.fill = GREY
                    scell.fill = GREY
                elif k < len(slot["entries"]):
                    t = slot["entries"][k]
                    tcell.value = t["teacher_name"]
                    scell.value = "、".join(
                        l["student_name"] for l in t["lessons"])
        r += n_sub

    # ---- borders: thin inside a day×period cell, medium around it
    last_row = r - 1
    for row in range(2, last_row + 1):
        block_top = (row - 2) % n_sub == 0
        block_bottom = (row - 2) % n_sub == n_sub - 1
        ws.cell(row=row, column=1).border = Border(
            left=MEDIUM, right=MEDIUM,
            top=MEDIUM if block_top else THIN,
            bottom=MEDIUM if block_bottom else THIN)
        for i in range(len(periods)):
            c0 = 2 + i * 2
            ws.cell(row=row, column=c0).border = Border(
                left=MEDIUM, right=THIN,
                top=MEDIUM if block_top else THIN,
                bottom=MEDIUM if block_bottom else THIN)
            ws.cell(row=row, column=c0 + 1).border = Border(
                left=THIN, right=MEDIUM,
                top=MEDIUM if block_top else THIN,
                bottom=MEDIUM if block_bottom else THIN)
    for col in range(1, 2 + 2 * len(periods)):
        ws.cell(row=1, column=col).border = Border(
            left=MEDIUM, right=MEDIUM, top=MEDIUM, bottom=MEDIUM)

    # ---- column widths, freeze panes, print setup
    ws.column_dimensions["A"].width = 10
    for i in range(len(periods)):
        ws.column_dimensions[get_column_letter(2 + i * 2)].width = 13
        ws.column_dimensions[get_column_letter(3 + i * 2)].width = 22
    ws.freeze_panes = "B2"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    ws.oddFooter.left.text = f"期間 {term_label(view)} ・ 作成 {generated_at}"
    ws.oddFooter.right.text = "&P / &N ページ"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
