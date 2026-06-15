# DocFlow + ProcSync Integration Summary

## Overview
Successfully integrated document generation (Invoice & Purchase Order) functionality into ProcSync's off-platform buyer management system. Users can now auto-generate invoices and purchase orders directly from buyer records, with data pre-filled from the buyer contact information.

## What Was Added

### 1. **Database Models** (`app/financials/models.py`)
Four new ORM models for persisting generated documents:

- **GeneratedInvoice**: Stores invoice records with buyer details, line items, amounts, tax, shipping, discounts, and status tracking
- **InvoiceLineItem**: Line items associated with invoices (product description, qty, unit price, line total)
- **GeneratedPurchaseOrder**: Stores purchase order records with similar structure to invoices
- **PurchaseOrderLineItem**: Line items for purchase orders

**Key Features**:
- Auto-generated document numbers (MGT YYMM/NNN for invoices, PO/YYMM/NNN for POs)
- Linked to `OffPlatformBuyerContact` for buyer details
- Support for tax rates, shipping costs, discounts
- Status tracking (draft, finalized, paid/confirmed)
- Timestamp tracking for creation/updates

### 2. **API Endpoints** (`app/financials/document_routes.py`)
New document management API routes:

#### Invoices
- `POST /api/financials/invoices` - Create new invoice with line items
- `GET /api/financials/invoices` - List invoices (with buyer filter)
- `GET /api/financials/invoices/{invoice_id}` - Get invoice details
- `DELETE /api/financials/invoices/{invoice_id}` - Delete invoice

#### Purchase Orders
- `POST /api/financials/purchase-orders` - Create new PO with line items
- `GET /api/financials/purchase-orders` - List POs (with buyer filter)
- `GET /api/financials/purchase-orders/{po_id}` - Get PO details
- `DELETE /api/financials/purchase-orders/{po_id}` - Delete PO

**Request Format**:
```json
{
  "buyer_contact_id": 1,
  "invoice_date": "2026-03-27",
  "due_date": "2026-04-10",
  "currency": "MYR",
  "tax_rate": 0.06,
  "shipping_cost": 50.00,
  "discount_amount": 0.00,
  "remarks": "Special order",
  "items_json": "[{\"description\":\"Item 1\",\"quantity\":5,\"unit_price\":100.00,\"line_total\":500.00,\"sku\":\"SKU-001\"}]"
}
```

### 3. **Frontend UI Updates** (`app/static/purchases.html`)
Enhanced buyer management interface:

- **New Buttons in Buyer Table**: 
  - "Invoice" button - Generate invoice for buyer
  - "PO" button - Generate purchase order for buyer
  - Buttons appear in both desktop and mobile views

- **Navigation Flow**:
  1. User clicks "Invoice" or "PO" button on buyer record
  2. System pre-fills buyer information (name, company, phone, address)
  3. Pre-populates document date with today's date
  4. Redirects to `/documents` page with buyer context

### 4. **Main App Integration** (`app/main.py`)
- Imported new document models for Alembic migration tracking
- Registered document router with FastAPI app
- Routes available at `/api/financials/` prefix

## Database Migration

New tables created:
- `generated_invoices` - Invoice records
- `invoice_line_items` - Invoice line items
- `generated_purchase_orders` - PO records
- `purchase_order_line_items` - PO line items

To apply migrations:
```bash
alembic upgrade head
```

## Usage Flow

### From Buyer List:
1. Go to **ProcSync** → **Manage Contacts** tab
2. Select **Off-Platform Buyers** sub-tab
3. Find desired buyer in table
4. Click **Invoice** or **PO** button
5. User is directed to DocFlow with pre-filled buyer data
6. Complete document details and save

### API Usage (Direct):
```bash
# Create invoice
curl -X POST http://localhost:8000/api/financials/invoices \
  -F "buyer_contact_id=1" \
  -F "invoice_date=2026-03-27" \
  -F "currency=MYR" \
  -F "tax_rate=0.06" \
  -F "items_json=[{\"description\":\"Item 1\",\"quantity\":5,\"unit_price\":100,\"line_total\":500}]"

# List invoices for buyer
curl http://localhost:8000/api/financials/invoices?buyer_contact_id=1

# Get invoice details
curl http://localhost:8000/api/financials/invoices/1
```

## Data Pre-Fill Parameters

When redirecting to DocFlow, the following buyer information is passed:
- `doc_type`: "invoice" or "po"
- `buyer_id`: Buyer contact ID
- `buyer_name`: Full name
- `buyer_company`: Company name (if set)
- `buyer_phone`: Full phone number with country code
- `buyer_address`: Buyer address

Example redirect URL:
```
/documents?doc_type=invoice&buyer_id=1&buyer_name=John%20Doe&buyer_company=ABC%20Inc&buyer_phone=%2B60123456789&buyer_address=123%20Main%20St
```

## Features & Benefits

✅ **Buyer Context Preservation** - No manual re-entry of buyer information
✅ **Automatic Numbering** - Sequential invoice/PO numbers by month
✅ **Multi-line Items Support** - Add multiple products per document
✅ **Flexible Pricing** - Tax rates, shipping, discounts supported
✅ **Status Tracking** - Draft/Finalized/Paid states
✅ **Database Persistence** - Documents stored for audit trail
✅ **RESTful API** - Full CRUD operations available
✅ **Mobile Responsive** - Works on desktop and mobile devices

## Next Steps (Optional Enhancements)

1. **PDF Export from API** - Generate server-side PDFs
2. **Email Integration** - Send documents directly to buyers
3. **Document Templates** - Customize invoice/PO formats
4. **Audit Log** - Track all document modifications
5. **Bulk Document Generation** - Create multiple documents at once
6. **Payment Tracking** - Link payments to invoices
7. **Document Linking** - Connect invoices to off-platform sales records
8. **Search & Filters** - Advanced document search capabilities

## Technical Stack

- **Backend**: FastAPI + SQLAlchemy ORM
- **Database**: SQLite with Alembic migrations
- **Frontend**: Vanilla JavaScript with HTML/CSS
- **API Format**: JSON with FormData for multipart requests

## File Changes Summary

| File | Changes |
|------|---------|
| `app/financials/models.py` | Added 4 new models (Invoice, InvoiceLineItem, PO, POLineItem) |
| `app/financials/document_routes.py` | New file with 8 API endpoints |
| `app/main.py` | Added imports and router registration |
| `app/static/purchases.html` | Added Invoice/PO buttons and generation functions |

## Testing Checklist

- [ ] Start application server
- [ ] Navigate to ProcSync page
- [ ] Open Manage Contacts modal
- [ ] Add a test buyer contact
- [ ] Click "Invoice" button on buyer
- [ ] Verify redirect to DocFlow with pre-filled buyer data
- [ ] Click "PO" button on buyer
- [ ] Verify redirect with PO parameters
- [ ] Create sample invoice via API
- [ ] List invoices via API
- [ ] Delete invoice via API

## Support & Maintenance

For issues or questions:
1. Check DocFlow documentation
2. Verify buyer contact is properly saved
3. Check API logs for errors
4. Ensure database migrations are applied
5. Test with sample data using curl/Postman
