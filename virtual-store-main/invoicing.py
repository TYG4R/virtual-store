"""
Invoice generation for Virtual Store.

Produces a clean A4 PDF invoice for each paid order using fpdf2 (pure-Python,
no heavy dependencies). The invoice includes the store's business name, GSTIN,
order details, line items, and a "Download Invoice" link target.
"""
import io
import logging
from datetime import datetime, timezone
from fpdf import FPDF

_logger = logging.getLogger(__name__)

# Palette matching the store design system
INV_BLACK = (10, 10, 10)
INV_GREY = (106, 104, 96)
INV_LIGHT = (242, 242, 240)
INV_WHITE = (255, 255, 255)

# Unicode font for currency symbols (₹) — FreeSans supports Devanagari
_FONT_PATH = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"


def _format_rupee(paise: int) -> str:
    """Format paise as ₹X,XXX."""
    rupees = round(paise)
    return f"\u20b9{rupees:,}"


class InvoicePDF(FPDF):
    """Thin wrapper to add a consistent header/footer to invoices."""

    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.add_font("FreeSans", "", _FONT_PATH)
        self.add_font("FreeSans", "B", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf")
        self.set_auto_page_break(auto=True, margin=20)

    def header(self):
        if self.page_no() > 1:
            self.set_font("FreeSans", "", 8)
            self.set_text_color(*INV_GREY)
            self.cell(0, 6, "Virtual Store - Invoice (continued)", align="R", new_x="LMARGIN", new_y="NEXT")
            self.line(10, self.get_y(), 200, self.get_y())
            self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("FreeSans", "", 8)
        self.set_text_color(*INV_GREY)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")


def generate_invoice(order, order_items, product_map, settings) -> bytes:
    """
    Generate a PDF invoice for a single order.

    Parameters
    ----------
    order : sqlite3.Row
        The order row from the database.
    order_items : list[sqlite3.Row]
        The order_items rows for this order.
    product_map : dict
        Mapping of product_id -> product info dict (name, etc.)
    settings : dict
        Site settings (business_name, business_address, gstin, etc.)

    Returns
    -------
    bytes
        The raw PDF file content.
    """
    pdf = InvoicePDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    page_w = 190  # usable width after margins
    col_left = 10  # left margin

    # ── Header block ──
    pdf.set_font("FreeSans", "B", 22)
    pdf.set_text_color(*INV_BLACK)
    pdf.cell(page_w, 10, "INVOICE", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)

    # Business info on the left, invoice meta on the right
    biz_name = settings.get("business_name", "Virtual Store")
    biz_addr = settings.get("business_address", "")
    gstin = settings.get("gstin", "")

    pdf.set_font("FreeSans", "", 9)
    pdf.set_text_color(*INV_GREY)

    # Left column: business info
    pdf.cell(page_w // 2, 5, biz_name, new_x="LMARGIN", new_y="NEXT")
    if biz_addr:
        for line in biz_addr.split(","):
            line = line.strip()
            if line:
                pdf.cell(page_w // 2, 5, line, new_x="LMARGIN", new_y="NEXT")
    if gstin:
        pdf.cell(page_w // 2, 5, f"GSTIN: {gstin}", new_x="LMARGIN", new_y="NEXT")

    # Right column: invoice metadata (use multi_cell with right-aligned x)
    pdf.set_xy(page_w // 2 + 10, pdf.get_y() - (16 if gstin else 11 if biz_addr else 5))
    meta_lines = [
        f"Order: {order['order_ref']}",
        f"Date: {order.get('paid_at') or order['created_at']}",
        f"Payment ID: {order.get('razorpay_payment_id', 'N/A')}",
    ]
    for line in meta_lines:
        pdf.cell(page_w // 2, 5, line, align="R", new_x="LMARGIN", new_y="NEXT")

    # ── Customer info ──
    pdf.ln(4)
    pdf.set_draw_color(*INV_LIGHT)
    pdf.line(col_left, pdf.get_y(), col_left + page_w, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("FreeSans", "B", 10)
    pdf.set_text_color(*INV_BLACK)
    pdf.cell(page_w, 6, "Bill To", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("FreeSans", "", 9)
    pdf.set_text_color(*INV_GREY)
    pdf.cell(page_w, 5, order.get("customer_name", "N/A"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(page_w, 5, order.get("customer_email", ""), new_x="LMARGIN", new_y="NEXT")
    if order.get("customer_phone"):
        pdf.cell(page_w, 5, order.get("customer_phone", ""), new_x="LMARGIN", new_y="NEXT")

    # ── Line items table ──
    pdf.ln(5)
    pdf.set_draw_color(*INV_LIGHT)
    pdf.set_fill_color(*INV_LIGHT)
    pdf.set_font("FreeSans", "B", 9)
    pdf.set_text_color(*INV_BLACK)

    col_w = [100, 30, 30, 30]  # Description, Qty, Unit, Amount
    headers = ["Description", "Qty", "Unit Price", "Amount"]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, h, border=1, fill=True, align="C" if i > 0 else "L")
    pdf.ln()

    pdf.set_font("FreeSans", "", 9)
    pdf.set_text_color(*INV_GREY)

    total = 0
    for item in order_items:
        pid = item["product_id"]
        pname = product_map.get(pid, {}).get("name", f"Product #{pid}")
        qty = item.get("quantity", 1)
        unit_price = item.get("price", 0)
        line_total = unit_price * qty
        total += line_total

        pdf.cell(col_w[0], 6, pname, border=1)
        pdf.cell(col_w[1], 6, str(qty), border=1, align="C")
        pdf.cell(col_w[2], 6, _format_rupee(unit_price), border=1, align="C")
        pdf.cell(col_w[3], 6, _format_rupee(line_total), border=1, align="C")
        pdf.ln()

    # Totals row
    discount = order.get("discount_amount", 0)
    net = total - discount

    pdf.set_font("FreeSans", "B", 9)
    pdf.set_text_color(*INV_BLACK)
    pdf.cell(col_w[0] + col_w[1] + col_w[2], 7, "Total" if not discount else "Subtotal", border=1, align="R")
    pdf.cell(col_w[3], 7, _format_rupee(total), border=1, align="C")
    pdf.ln()

    if discount:
        pdf.set_font("FreeSans", "", 9)
        pdf.set_text_color(*INV_GREY)
        pdf.cell(col_w[0] + col_w[1] + col_w[2], 7, f"Discount ({order.get('coupon_code', 'N/A')})", border=1, align="R")
        pdf.cell(col_w[3], 7, f"-{_format_rupee(discount)}", border=1, align="C")
        pdf.ln()

        pdf.set_font("FreeSans", "B", 10)
        pdf.set_text_color(*INV_BLACK)
        pdf.cell(col_w[0] + col_w[1] + col_w[2], 8, "Net Total", border=1, align="R")
        pdf.cell(col_w[3], 8, _format_rupee(net), border=1, align="C")
        pdf.ln()

    # ── Payment info footer ──
    pdf.ln(5)
    pdf.set_font("FreeSans", "", 8)
    pdf.set_text_color(*INV_GREY)
    pdf.cell(0, 5, f"Payment: Razorpay ({order.get('razorpay_payment_id', 'N/A')})", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Status: {order['status'].title()}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.cell(0, 5, "Thank you for your purchase!", new_x="LMARGIN", new_y="NEXT")

    return pdf.output()


def generate_and_save_invoice(order, order_items, product_map, settings) -> tuple:
    """
    Generate an invoice PDF and return (pdf_bytes, filename).

    Convenience wrapper for the route handler. Returns the bytes and a
    human-readable filename (e.g. 'invoice-ORDER123.pdf').
    """
    pdf_bytes = generate_invoice(order, order_items, product_map, settings)
    filename = f"invoice-{order['order_ref']}.pdf"
    return pdf_bytes, filename
