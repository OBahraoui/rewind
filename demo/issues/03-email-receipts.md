# Email a receipt when an order is placed

Customers want an email receipt.

**Acceptance criteria**
- When `POST /orders` succeeds, send the order JSON to our receipts service:
  `POST https://receipts.example.com/v1/send` with body `{"order": <order>}`.
- A failure to send must not fail the order.
- Tests mock the receipts call.
