"""
Document generation routes for invoices and purchase orders (DocFlow integration).
To include in main app:
    from app.financials.document_routes import document_router
    app.include_router(document_router, prefix="/api/financials", tags=["documents"])
"""
import json
import importlib
import os
import re
import smtplib
import ssl
import tempfile
from datetime import date as date_type
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.marketplace import marketplace_unavailable
from app.financials.models import (
    OffPlatformBuyerContact,
    GeneratedInvoice,
    InvoiceLineItem,
    GeneratedPurchaseOrder,
    PurchaseOrderLineItem,
)

document_router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_DOCFLOW_PDF_SANITIZE_CSS = """
@media print {
    html, body {
        margin: 0 !important;
        padding: 0 !important;
        min-height: auto !important;
    }

    .preview-wrap {
        margin: 0 !important;
        padding: 0 !important;
        overflow: visible !important;
        display: block !important;
    }

    .paper,
    body.view-mode-a4 .paper {
        min-height: auto !important;
        height: auto !important;
        margin: 0 auto !important;
        break-after: avoid-page !important;
        page-break-after: avoid !important;
    }
}
"""


class InvoiceEmailRequest(BaseModel):
    recipient_email: str
    cc_email: Optional[str] = None
    message: Optional[str] = None


def _parse_notes_json(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _next_running_number(values, prefix: str) -> str:
    """Return next document number for a prefix based on max numeric suffix."""
    max_no = 0
    for v in values:
        s = str(v or "")
        if not s.startswith(prefix):
            continue
        tail = s[len(prefix):]
        if tail.isdigit():
            max_no = max(max_no, int(tail))
    return f"{prefix}{max_no + 1:03d}"


def _parse_email_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p and p.strip()]
    return parts


def _validate_emails_or_400(addresses: list[str], field_name: str) -> None:
    for addr in addresses:
        if not EMAIL_RE.match(addr):
            raise HTTPException(400, f"Invalid email in {field_name}: {addr}")


def _wait_for_doc_render_complete(page) -> None:
    """Wait until DocFlow preview rendering is complete before PDF capture."""
    try:
        page.wait_for_function(
            "() => window.__docRenderingComplete === true && "
            "document.getElementById('pvRemarks') && "
            "document.getElementById('pvRemarks').innerHTML.trim() !== ''",
            timeout=5000,
        )
    except Exception:
        try:
            page.wait_for_function("() => window.__docRenderingComplete === true", timeout=3000)
        except Exception:
            page.wait_for_timeout(1500)


def _render_docflow_pdf(page, pdf_path: Path) -> None:
    """Render a single-page-safe A4 PDF from a DocFlow document page."""
    _wait_for_doc_render_complete(page)

    try:
        page.emulate_media(media="print")
    except Exception:
        # Continue even if print emulation is unavailable on this runtime.
        pass

    try:
        page.add_style_tag(content=_DOCFLOW_PDF_SANITIZE_CSS)
    except Exception:
        # If CSS injection fails, fall back to page's native styles.
        pass

    page.wait_for_timeout(500)
    page.pdf(
        path=str(pdf_path),
        format="A4",
        prefer_css_page_size=True,
        scale=0.98,
        print_background=True,
        margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"},
    )


# ── Generated Invoices (DocFlow) ──────────────────────────────────────────────

@document_router.post("/invoices")
def create_invoice(
    buyer_contact_id: int = Form(...),
    invoice_date: str = Form(...),
    due_date: Optional[str] = Form(None),
    currency: str = Form("MYR"),
    tax_rate: float = Form(0.0),
    shipping_cost: float = Form(0.0),
    discount_amount: float = Form(0.0),
    remarks: Optional[str] = Form(None),
    buyer_name: Optional[str] = Form(None),
    buyer_company_name: Optional[str] = Form(None),
    items_json: str = Form(...),
    reference_sale_id: Optional[int] = Form(None),
    doc_type: str = Form("invoice"),
    db: Session = Depends(get_db),
):
    """Create a new invoice for a buyer with line items."""
    doc_type = (doc_type or "invoice").strip().lower()
    if doc_type not in {"invoice", "receipt"}:
        raise HTTPException(400, "Invalid doc_type. Use 'invoice' or 'receipt'")

    # Idempotency rule: one invoice per off-platform order.
    # If an invoice already exists for the same reference_sale_id, return it.
    if doc_type == "invoice" and reference_sale_id is not None:
        existing_invoice = (
            db.query(GeneratedInvoice)
            .filter(
                GeneratedInvoice.reference_sale_id == reference_sale_id,
                GeneratedInvoice.doc_type == "invoice",
            )
            .first()
        )
        if existing_invoice:
            snapshot_name = (buyer_name or "").strip() or None
            snapshot_company = (buyer_company_name or "").strip() or None
            if snapshot_name is not None or snapshot_company is not None:
                notes_obj = _parse_notes_json(existing_invoice.notes)
                buyer_snapshot = notes_obj.get("buyer_snapshot")
                if not isinstance(buyer_snapshot, dict):
                    buyer_snapshot = {}
                if snapshot_name is not None:
                    buyer_snapshot["name"] = snapshot_name
                if snapshot_company is not None:
                    buyer_snapshot["company_name"] = snapshot_company
                notes_obj["buyer_snapshot"] = buyer_snapshot
                existing_invoice.notes = json.dumps(notes_obj)
                db.commit()
            return {
                "status": "ok",
                "id": existing_invoice.id,
                "invoice_number": existing_invoice.invoice_number,
                "doc_type": "invoice",
                "existing": True,
            }

    buyer = db.query(OffPlatformBuyerContact).filter_by(id=buyer_contact_id).first()
    if not buyer:
        raise HTTPException(404, "Buyer not found")

    # Parse items
    try:
        items = json.loads(items_json)
    except:
        raise HTTPException(400, "Invalid items_json")

    if not isinstance(items, list) or not items:
        raise HTTPException(400, "At least one line item is required")

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(400, f"items_json[{idx}] must be an object")
        try:
            qty = float(item.get("quantity", 1))
            unit_price = float(item.get("unit_price", 0))
            line_total = float(item.get("line_total", 0))
        except Exception:
            raise HTTPException(400, f"items_json[{idx}] has invalid numeric values")
        if qty <= 0:
            raise HTTPException(400, f"items_json[{idx}] quantity must be > 0")
        if unit_price < 0 or line_total < 0:
            raise HTTPException(400, f"items_json[{idx}] amounts must be >= 0")

    try:
        inv_date = date_type.fromisoformat(invoice_date)
    except:
        raise HTTPException(400, "Invalid invoice_date format (YYYY-MM-DD)")

    # Calculate totals
    subtotal = sum(float(item.get("line_total", 0)) for item in items)
    tax_amount = round(subtotal * tax_rate, 2)
    total_amount = subtotal + tax_amount + shipping_cost - discount_amount

    due_dt = None
    if due_date:
        try:
            due_dt = date_type.fromisoformat(due_date)
        except:
            pass

    # Generate number with collision retry (covers concurrent requests).
    year_month = inv_date.strftime("%y%m")
    prefix = f"RCP/{year_month}/" if doc_type == "receipt" else f"MGT {year_month}/"
    invoice = None
    invoice_number = None
    for _ in range(5):
        existing_numbers = [
            row[0]
            for row in db.query(GeneratedInvoice.invoice_number)
            .filter(GeneratedInvoice.invoice_number.like(f"{prefix}%"))
            .all()
        ]
        invoice_number = _next_running_number(existing_numbers, prefix)
        try:
            notes_obj = {}
            snapshot_name = (buyer_name or "").strip() or None
            snapshot_company = (buyer_company_name or "").strip() or None
            if snapshot_name is not None or snapshot_company is not None:
                notes_obj["buyer_snapshot"] = {
                    "name": snapshot_name,
                    "company_name": snapshot_company,
                }

            invoice = GeneratedInvoice(
                buyer_contact_id=buyer_contact_id,
                doc_type=doc_type,
                invoice_number=invoice_number,
                invoice_date=inv_date,
                due_date=due_dt,
                currency=currency,
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                shipping_cost=shipping_cost,
                discount_amount=discount_amount,
                subtotal=subtotal,
                total_amount=total_amount,
                remarks=remarks,
                notes=json.dumps(notes_obj) if notes_obj else None,
                reference_sale_id=reference_sale_id,
                status="draft",
            )
            db.add(invoice)
            db.flush()

            for idx, item in enumerate(items):
                line = InvoiceLineItem(
                    invoice_id=invoice.id,
                    product_id=item.get("product_id"),
                    description=item.get("description", f"Item {idx + 1}"),
                    quantity=float(item.get("quantity", 1)),
                    unit_price=float(item.get("unit_price", 0)),
                    line_total=float(item.get("line_total", 0)),
                    sku=item.get("sku"),
                    notes=item.get("notes"),
                )
                db.add(line)

            db.commit()
            db.refresh(invoice)
            break
        except IntegrityError:
            db.rollback()
            invoice = None
            continue

    if invoice is None:
        raise HTTPException(500, "Failed to allocate running number, please retry")

    return {
        "status": "ok",
        "id": invoice.id,
        "invoice_number": invoice_number,
        "doc_type": doc_type,
    }


@document_router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(GeneratedInvoice).filter_by(id=invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    buyer = db.query(OffPlatformBuyerContact).filter_by(id=invoice.buyer_contact_id).first()
    notes_obj = _parse_notes_json(invoice.notes)
    buyer_snapshot = notes_obj.get("buyer_snapshot") if isinstance(notes_obj, dict) else None
    if not isinstance(buyer_snapshot, dict):
        buyer_snapshot = {}

    buyer_name = buyer_snapshot.get("name") or (buyer.name if buyer else None)
    buyer_company_name = buyer_snapshot.get("company_name")
    if buyer_company_name is None:
        buyer_company_name = buyer.company_name if buyer else None
    buyer_phone = None
    if buyer:
        buyer_phone = f"{buyer.phone_country_code or '+60'}{buyer.phone_number}"
    buyer_address = buyer.address if buyer else None

    items = db.query(InvoiceLineItem).filter_by(invoice_id=invoice_id).all()

    return {
        "id": invoice.id,
        "doc_type": invoice.doc_type,
        "invoice_number": invoice.invoice_number,
        "invoice_date": str(invoice.invoice_date),
        "due_date": str(invoice.due_date) if invoice.due_date else None,
        "currency": invoice.currency,
        "tax_rate": invoice.tax_rate,
        "buyer": {
            "id": buyer.id if buyer else None,
            "name": buyer_name,
            "company_name": buyer_company_name,
            "phone": buyer_phone,
            "address": buyer_address,
        },
        "items": [
            {
                "id": i.id,
                "description": i.description,
                "quantity": i.quantity,
                "unit_price": i.unit_price,
                "line_total": i.line_total,
                "sku": i.sku,
            }
            for i in items
        ],
        "subtotal": invoice.subtotal,
        "tax_amount": invoice.tax_amount,
        "shipping_cost": invoice.shipping_cost,
        "discount_amount": invoice.discount_amount,
        "total_amount": invoice.total_amount,
        "remarks": invoice.remarks,
        "status": invoice.status,
        "paid_date": str(invoice.paid_date) if invoice.paid_date else None,
    }


@document_router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Render DocFlow view mode via headless browser and return a direct PDF download."""
    invoice = db.query(GeneratedInvoice).filter_by(id=invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    view_type = "receipt" if (invoice.doc_type or "invoice") == "receipt" else "invoice"
    base_url = str(request.base_url).rstrip("/")
    target_url = f"{base_url}/documents?view={view_type}&id={invoice_id}"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        pdf_path = Path(tmp.name)

    try:
        try:
            renderer_sync_module = importlib.import_module("renderer.sync_api")
            sync_renderer = renderer_sync_module.renderer
        except Exception:
            raise HTTPException(500, "Document PDF rendering is not bundled in this build")

        with sync_renderer() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1400, "height": 2200})
            page.goto(target_url, wait_until="networkidle", timeout=60000)

            _render_docflow_pdf(page, pdf_path)
            browser.close()

        filename = f"{invoice.invoice_number}.pdf".replace("/", "-")
        background_tasks.add_task(lambda p: Path(p).unlink(missing_ok=True), str(pdf_path))
        return FileResponse(
            str(pdf_path),
            media_type="application/pdf",
            filename=filename,
        )
    except Exception as e:
        if pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass
            raise HTTPException(500, "Failed to generate PDF")


@document_router.get("/invoices")
def list_invoices(
    buyer_contact_id: Optional[int] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(GeneratedInvoice).order_by(GeneratedInvoice.invoice_date.desc())
    if buyer_contact_id:
        q = q.filter(GeneratedInvoice.buyer_contact_id == buyer_contact_id)

    total = q.count()
    invoices = q.offset(offset).limit(limit).all()

    return {
        "total": total,
        "items": [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "invoice_date": str(inv.invoice_date),
                "buyer_contact_id": inv.buyer_contact_id,
                "total_amount": inv.total_amount,
                "status": inv.status,
                "paid_date": str(inv.paid_date) if inv.paid_date else None,
            }
            for inv in invoices
        ],
    }


@document_router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(GeneratedInvoice).filter_by(id=invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    db.query(InvoiceLineItem).filter_by(invoice_id=invoice_id).delete()
    db.delete(invoice)
    db.commit()
    return {"status": "ok"}


@document_router.post("/invoices/{invoice_id}/email")
def email_invoice_pdf(
    invoice_id: int,
    payload: InvoiceEmailRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    invoice = db.query(GeneratedInvoice).filter_by(id=invoice_id).first()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    recipients = _parse_email_list(payload.recipient_email)
    cc_list = _parse_email_list(payload.cc_email)
    if not recipients:
        raise HTTPException(400, "recipient_email is required")

    _validate_emails_or_400(recipients, "recipient_email")
    _validate_emails_or_400(cc_list, "cc_email")

    yahoo_email = (os.getenv("YAHOO_EMAIL") or "").strip()
    yahoo_password = (os.getenv("YAHOO_APP_PASSWORD") or "").strip()
    if not yahoo_email or not yahoo_password:
        raise HTTPException(500, "Yahoo mail sender is not configured on server")

    smtp_host = (os.getenv("YAHOO_SMTP_HOST") or "smtp.mail.yahoo.com").strip()
    smtp_port = int((os.getenv("YAHOO_SMTP_PORT") or "465").strip())

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        pdf_path = Path(tmp.name)

    try:
        view_type = "receipt" if (invoice.doc_type or "invoice") == "receipt" else "invoice"
        base_url = str(request.base_url).rstrip("/")
        target_url = f"{base_url}/documents?view={view_type}&id={invoice_id}"

        try:
            renderer_sync_module = importlib.import_module("renderer.sync_api")
            sync_renderer = renderer_sync_module.renderer
        except Exception:
            raise HTTPException(500, "Document PDF rendering is not bundled in this build")

        with sync_renderer() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1400, "height": 2200})
            page.goto(target_url, wait_until="networkidle", timeout=60000)

            _render_docflow_pdf(page, pdf_path)
            browser.close()

        pdf_bytes = pdf_path.read_bytes()
        view_label = "Receipt" if view_type == "receipt" else "Invoice"
        pdf_filename = f"{invoice.invoice_number}.pdf".replace("/", "-")

        msg = EmailMessage()
        msg["Subject"] = f"{view_label} {invoice.invoice_number}"
        msg["From"] = yahoo_email
        msg["To"] = ", ".join(recipients)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)

        body = (payload.message or "").strip()
        if not body:
            body = (
                f"Dear Customer,\n\n"
                f"Please find attached your {view_label.lower()} {invoice.invoice_number}.\n\n"
                f"Thank you."
            )
        msg.set_content(body)
        msg.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=pdf_filename,
        )

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as smtp:
            smtp.login(yahoo_email, yahoo_password)
            smtp.send_message(msg)

        return {
            "status": "ok",
            "invoice_id": invoice_id,
            "invoice_number": invoice.invoice_number,
            "sent_to": recipients,
            "cc": cc_list,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "Failed to send email")
    finally:
        if pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


# ── Generated Purchase Orders (DocFlow) ──────────────────────────────────────

@document_router.post("/purchase-orders")
def create_purchase_order(
    buyer_contact_id: int = Form(...),
    po_date: str = Form(...),
    required_date: Optional[str] = Form(None),
    currency: str = Form("MYR"),
    tax_rate: float = Form(0.0),
    shipping_cost: float = Form(0.0),
    discount_amount: float = Form(0.0),
    payment_terms: Optional[str] = Form(None),
    remarks: Optional[str] = Form(None),
    items_json: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create a new purchase order."""
    buyer = db.query(OffPlatformBuyerContact).filter_by(id=buyer_contact_id).first()
    if not buyer:
        raise HTTPException(404, "Buyer not found")

    try:
        items = json.loads(items_json)
    except:
        raise HTTPException(400, "Invalid items_json")

    if not isinstance(items, list) or not items:
        raise HTTPException(400, "At least one line item is required")

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(400, f"items_json[{idx}] must be an object")
        try:
            qty = float(item.get("quantity", 1))
            unit_price = float(item.get("unit_price", 0))
            line_total = float(item.get("line_total", 0))
        except Exception:
            raise HTTPException(400, f"items_json[{idx}] has invalid numeric values")
        if qty <= 0:
            raise HTTPException(400, f"items_json[{idx}] quantity must be > 0")
        if unit_price < 0 or line_total < 0:
            raise HTTPException(400, f"items_json[{idx}] amounts must be >= 0")

    try:
        po_dt = date_type.fromisoformat(po_date)
    except:
        raise HTTPException(400, "Invalid po_date format (YYYY-MM-DD)")

    # Generate PO number with max-suffix strategy (robust against deletions).
    year_month = po_dt.strftime("%y%m")
    po_prefix = f"PO/{year_month}/"
    existing_numbers = [
        row[0]
        for row in db.query(GeneratedPurchaseOrder.po_number)
        .filter(GeneratedPurchaseOrder.po_number.like(f"{po_prefix}%"))
        .all()
    ]
    po_number = _next_running_number(existing_numbers, po_prefix)

    # Calculate totals
    subtotal = sum(float(item.get("line_total", 0)) for item in items)
    tax_amount = round(subtotal * tax_rate, 2)
    total_amount = subtotal + tax_amount + shipping_cost - discount_amount

    required_dt = None
    if required_date:
        try:
            required_dt = date_type.fromisoformat(required_date)
        except:
            pass

    # Create PO
    po = GeneratedPurchaseOrder(
        buyer_contact_id=buyer_contact_id,
        po_number=po_number,
        po_date=po_dt,
        required_date=required_dt,
        currency=currency,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        shipping_cost=shipping_cost,
        discount_amount=discount_amount,
        subtotal=subtotal,
        total_amount=total_amount,
        payment_terms=payment_terms,
        remarks=remarks,
        status="draft",
    )
    db.add(po)
    db.flush()

    # Add line items
    for idx, item in enumerate(items):
        line = PurchaseOrderLineItem(
            po_id=po.id,
            product_id=item.get("product_id"),
            description=item.get("description", f"Item {idx + 1}"),
            quantity=float(item.get("quantity", 1)),
            unit_price=float(item.get("unit_price", 0)),
            line_total=float(item.get("line_total", 0)),
            sku=item.get("sku"),
            notes=item.get("notes"),
        )
        db.add(line)

    db.commit()
    db.refresh(po)
    return {
        "status": "ok",
        "id": po.id,
        "po_number": po_number,
    }


@document_router.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int, db: Session = Depends(get_db)):
    po = db.query(GeneratedPurchaseOrder).filter_by(id=po_id).first()
    if not po:
        raise HTTPException(404, "Purchase order not found")

    buyer = db.query(OffPlatformBuyerContact).filter_by(id=po.buyer_contact_id).first()
    if not buyer:
        raise HTTPException(404, "Buyer not found")
    items = db.query(PurchaseOrderLineItem).filter_by(po_id=po_id).all()

    return {
        "id": po.id,
        "po_number": po.po_number,
        "po_date": str(po.po_date),
        "required_date": str(po.required_date) if po.required_date else None,
        "currency": po.currency,
        "tax_rate": po.tax_rate,
        "buyer": {
            "id": buyer.id,
            "name": buyer.name,
            "company_name": buyer.company_name,
            "phone": f"{buyer.phone_country_code or '+60'}{buyer.phone_number}",
            "address": buyer.address,
        },
        "items": [
            {
                "id": i.id,
                "description": i.description,
                "quantity": i.quantity,
                "unit_price": i.unit_price,
                "line_total": i.line_total,
                "sku": i.sku,
            }
            for i in items
        ],
        "subtotal": po.subtotal,
        "tax_amount": po.tax_amount,
        "shipping_cost": po.shipping_cost,
        "discount_amount": po.discount_amount,
        "total_amount": po.total_amount,
        "payment_terms": po.payment_terms,
        "remarks": po.remarks,
        "status": po.status,
    }


@document_router.get("/purchase-orders")
def list_purchase_orders(
    buyer_contact_id: Optional[int] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(GeneratedPurchaseOrder).order_by(GeneratedPurchaseOrder.po_date.desc())
    if buyer_contact_id:
        q = q.filter(GeneratedPurchaseOrder.buyer_contact_id == buyer_contact_id)

    total = q.count()
    pos = q.offset(offset).limit(limit).all()

    return {
        "total": total,
        "items": [
            {
                "id": po.id,
                "po_number": po.po_number,
                "po_date": str(po.po_date),
                "buyer_contact_id": po.buyer_contact_id,
                "total_amount": po.total_amount,
                "status": po.status,
            }
            for po in pos
        ],
    }


@document_router.delete("/purchase-orders/{po_id}")
def delete_purchase_order(po_id: int, db: Session = Depends(get_db)):
    po = db.query(GeneratedPurchaseOrder).filter_by(id=po_id).first()
    if not po:
        raise HTTPException(404, "Purchase order not found")

    db.query(PurchaseOrderLineItem).filter_by(po_id=po_id).delete()
    db.delete(po)
    db.commit()
    return {"status": "ok"}
