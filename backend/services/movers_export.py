"""Spreadsheet formatting for the seller-scoped Top Movers report."""
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


def build_movers_workbook(rows, days):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Top Movers'
    ws.append([f'Top Movers - last {days} days'])
    ws.merge_cells('A1:D1')
    ws['A1'].font = Font(bold=True, size=15, color='FFFFFF')
    ws['A1'].fill = PatternFill('solid', fgColor='075E4B')
    ws.row_dimensions[1].height = 28
    ws.append(['Rank', 'Master SKU', 'Product name', 'Ordered pieces'])
    for cell in ws[2]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='087D60')
    for rank, item in enumerate(rows, 1):
        ws.append([rank, str(item['sku']), str(item['name']), int(item['units_moved'])])
        # Never interpret seller-entered SKU/name as spreadsheet formulas.
        for col in (2, 3):
            ws.cell(ws.max_row, col).data_type = 's'
        for cell in ws[ws.max_row]:
            cell.alignment = Alignment(vertical='center', wrap_text=True)
            if rank % 2 == 0:
                cell.fill = PatternFill('solid', fgColor='E9F5EF')
        ws.row_dimensions[ws.max_row].height = 25
    for col, width in zip('ABCD', (10, 36, 48, 20)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A3'
    ws.auto_filter.ref = f'A2:D{ws.max_row}'
    ws.sheet_view.showGridLines = False
    output = BytesIO()
    wb.save(output)
    return output.getvalue()
