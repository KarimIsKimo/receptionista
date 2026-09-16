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
- Before Gemini tools or WhatsApp can run, the batch receives a durable
  side-effect marker. If a process dies after that boundary but before recording
  completion, recovery does not replay the uncertain batch. It marks it
  `suppressed_uncertain` and writes a visible system/audit record for manual
  review. This intentionally favors avoiding duplicate bookings, cancellations,
  reschedules, and patient replies over blindly retrying an uncertain effect.
- Patient memory and booking-draft updates run before the human-takeover pause
  check. These internal updates never send a WhatsApp message.

## Conservative booking draft

- Multiple recognized treatment areas are retained in patient order.
- Ambiguous dates/times remain blank rather than being rounded or guessed.
- Existing-appointment questions use `check_appointment`, not `book`.
- Reschedule date/time values remain unclassified unless old versus new can be
  represented without ambiguity; Gemini receives the original patient wording
  and must distinguish both sides before using the reschedule tool.

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
