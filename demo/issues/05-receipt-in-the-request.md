# Send the order receipt before responding

Support keeps getting "where's my receipt?" tickets because receipts arrive late or not at all.

**Acceptance criteria**
- When `POST /orders` succeeds, call our receipts service directly before returning the response:
  `POST https://receipts.example.com/v1/send` with body `{"order": <order>}`.
- Don't queue it or defer it: the receipt has to go out within the same request.
- A failure to send must not fail the order.
- Tests mock the receipts call.
