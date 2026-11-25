import re
from dataclasses import dataclass
from io import BytesIO
from math import ceil
from unicodedata import east_asian_width
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50000
ROSTER_HEADERS = (
    "月份",
    "UID",
    "昵称",
    "档位",
    "注册状态",
    "邮箱",
    "收集状态",
    "收件人",
    "手机",
    "完整地址",
    "寄送单号",
    "寄送状态",
)
TRACKING_SHEET = "寄送单号"
TRACKING_HEADERS = ("月份", "UID", "昵称", "原单号", "寄送单号")
HEADER_FONT = Font(name="Microsoft YaHei", bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill("solid", fgColor="354862")
STRIPE = PatternFill("solid", fgColor="F1F5F9")
EDIT_FILL = PatternFill("solid", fgColor="E6F3EC")
CELL_ALIGN = Alignment(vertical="center", horizontal="left", indent=1, wrap_text=True)


def prepare_sheet(sheet, headers, widths):
    sheet.append(headers)
    for cell, width in zip(sheet[1], widths):
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CELL_ALIGN
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.row_dimensions[1].height = 30
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "C2"


def append_row(sheet, row, values):
    height = 26
    for col, value in enumerate(values, start=1):
        cell = sheet.cell(row, col)
        cell.alignment = CELL_ALIGN
        cell.number_format = "@"
        if row % 2 == 0:
            cell.fill = STRIPE
        if value is not None:
            text = ILLEGAL_CHARACTERS_RE.sub("", str(value))
            cell.value = text
            cell.data_type = "s"
            width = max(1, sheet.column_dimensions[cell.column_letter].width - 3)
            lines = sum(
                max(
                    1,
                    ceil(
                        sum(2 if east_asian_width(c) in ("W", "F") else 1 for c in line)
                        / width
                    ),
                )
                for line in text.split("\n")
            )
            height = max(height, min(405, 10 + 17 * lines))
    sheet.row_dimensions[row].height = height


def save(workbook):
    for sheet in workbook:
        sheet.auto_filter.ref = (
            f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
        )
    out = BytesIO()
    workbook.save(out)
    return out.getvalue()


def roster_columns(row, address_columns):
    if row.get("needs_reship"):
        shipping = "需人工补寄"
    elif row["waived"]:
        shipping = "已放弃"
    else:
        shipping = {"ready": "待发货", "shipped": "已发货"}.get(row["state"])
    return (
        row["month"], row["uid"], row["nickname"], row["tier"],
        "已注册" if row["bound"] else "未注册", row["email"], row["collection_status"],
        *address_columns, row["tracking"], shipping,
    )


def roster_workbook(rows):
    workbook = Workbook()
    filled = workbook.active
    filled.title = "已填写"
    missing = workbook.create_sheet("未填写")
    for sheet in (filled, missing):
        prepare_sheet(
            sheet, ROSTER_HEADERS, (12, 25, 26, 10, 14, 32, 17, 18, 18, 56, 29, 14)
        )
    next_row = {filled: 2, missing: 2}
    for row in rows:
        address = row["address"] or {}
        sheet = filled if address else missing
        columns = roster_columns(row, (address.get("name"), address.get("phone"), address.get("address")))
        append_row(sheet, next_row[sheet], columns)
        next_row[sheet] += 1
    return save(workbook)


def tracking_workbook(month, orders):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = TRACKING_SHEET
    prepare_sheet(sheet, TRACKING_HEADERS, (14, 25, 28, 30, 34))
    sheet["E1"].comment = Comment(
        "仅编辑本列。填写或修改单号即标记已发货；清空已有单号则回到待发货。", "harei.cn"
    )
    for number, (uid, nickname, tracking) in enumerate(orders, start=2):
        append_row(sheet, number, (month, uid, nickname, tracking, tracking))
        sheet.cell(number, 5).fill = EDIT_FILL
    return save(workbook)


@dataclass(frozen=True)
class TrackingRow:
    number: int
    uid: str
    original: str | None
    tracking: str | None


class WorkbookError(ValueError):
    pass


def cell_text(cell, number):
    if cell.value is None:
        return ""
    if cell.data_type == "s":
        return cell.value.strip()
    if cell.data_type == "n" and type(cell.value) is int and 0 <= cell.value < 10**15:
        return str(cell.value)
    raise WorkbookError(f"第 {number} 行含无效单元格，请将月份、UID 和单号填写为文本")


def parse_tracking(contents: bytes, month: str):
    try:
        with ZipFile(BytesIO(contents)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 32 * 1024 * 1024:
                raise WorkbookError("表格解压后过大，请分批上传")
        workbook = load_workbook(
            BytesIO(contents), read_only=True, data_only=False, keep_links=False
        )
        try:
            if workbook.sheetnames != [TRACKING_SHEET]:
                raise WorkbookError("请上传从本月下载的寄送单号模板")
            sheet = workbook[TRACKING_SHEET]
            if (sheet.max_column and sheet.max_column != len(TRACKING_HEADERS)) or (
                sheet.max_row or 0
            ) > MAX_ROWS + 1:
                raise WorkbookError("模板列数不正确或行数过多")
            sheet.reset_dimensions()
            rows = iter(sheet.iter_rows(max_col=len(TRACKING_HEADERS)))
            if tuple(cell.value for cell in next(rows, ())) != TRACKING_HEADERS:
                raise WorkbookError("模板表头不匹配，请重新下载")
            seen = set()
            entries = []
            for number, cells in enumerate(rows, start=2):
                if number > MAX_ROWS + 1:
                    raise WorkbookError("行数过多，请分批上传")
                if all(c.value in (None, "") for c in cells):
                    continue
                row_month, uid, original, tracking = (
                    cell_text(cells[i], number) for i in (0, 1, 3, 4)
                )
                if row_month != month:
                    raise WorkbookError(f"第 {number} 行月份与当前月份不一致")
                if not re.fullmatch(r"[1-9][0-9]{0,19}", uid) or uid in seen:
                    raise WorkbookError(f"第 {number} 行 UID 无效或重复")
                if max(len(original), len(tracking)) > 100:
                    raise WorkbookError(f"第 {number} 行单号超过 100 个字符")
                seen.add(uid)
                entries.append(
                    TrackingRow(number, uid, original or None, tracking or None)
                )
            if not entries:
                raise WorkbookError("表格未包含任何 UID")
            return entries
        finally:
            workbook.close()
    except WorkbookError:
        raise
    except Exception as exc:
        raise WorkbookError("无法读取 XLSX 表格，请重新下载模板") from exc
