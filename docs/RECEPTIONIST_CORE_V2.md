# Receptionist core v2

## Runtime behavior

- Every incoming WhatsApp message is deduplicated and saved together with a
  durable queue row in one PostgreSQL transaction.
- A per-patient PostgreSQL lease keeps message ordering consistent across
  multiple application workers.
- Pending fragments are combined after the debounce window and sent to Gemini
  as one turn. The saved fragments are excluded from Gemini history so the
  current patient text is not duplicated.
- The lifecycle queue scanner recovers pending work after a process restart.
- Patient memory and booking-draft updates run before the human-takeover pause
  check. These internal updates never send a WhatsApp message.

## Optional environment variables

| Variable | Default | Allowed range | Purpose |
| --- | ---: | ---: | --- |
| `MESSAGE_DEBOUNCE_SECONDS` | `3` | `0.25`–`15` seconds | Quiet period used to combine rapid patient fragments. |
| `MESSAGE_PROCESSING_LEASE_SECONDS` | `300` | `30`–`900` seconds | Cross-worker ownership period for one patient's queue. |
| `BOOKING_DRAFT_TIMEOUT_MINUTES` | `30` | `5`–`240` minutes | Inactivity timeout for persisted booking drafts. |

The existing startup migration creates `inbound_message_queue`,
`conversation_processing_leases`, and `booking_drafts` with additive
`CREATE TABLE IF NOT EXISTS` statements. No existing patient, chat, or booking
data is replaced.

## Verification

```bash
python -m py_compile main.py receptionist/*.py
python -m unittest discover -s tests -v
```
