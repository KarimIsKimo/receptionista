# Apps Script exact-target deployment requirement

The Apps Script version reviewed with PR #2 does **not** yet provide an exact
mutation contract:

- `cancel` reads `phone_number` and `date`, ignores `time` and
  `appointment_id`, and deletes the last matching row.
- `reschedule` has no handler.
- schedule rows contain time, name, phone, and area, but no stable appointment
  ID.

Sending `time` or `appointment_id` from Python therefore does not by itself
make the current script exact. Deploy an Apps Script revision before relying on
multiple appointments for one patient on the same date or enabling production
rescheduling.

## Required cancel contract

The script must normalize and match the requested target using either:

1. a stable `appointment_id`, or
2. `phone_number` + `date` + normalized `time`.

It must never fall back to phone + date when time or ID was supplied. A
successful response should identify the exact deleted target:

```json
{
  "status": "success",
  "deleted": true,
  "target": {
    "phone_number": "201012345678",
    "date": "2026-09-17",
    "time": "7:00 PM",
    "appointment_id": "optional-stable-id"
  }
}
```

Return `deleted: false` with a non-success status when there is no exact match
or more than one match.

## Required reschedule contract

The script must implement `action: "reschedule"` as one locked operation:

1. find exactly one old row by stable ID or phone + old date + normalized old
   time;
2. verify the new date/time is still available;
3. update that exact row;
4. return the old target and the new appointment.

```json
{
  "status": "success",
  "rescheduled": true,
  "target": {
    "phone_number": "201012345678",
    "date": "2026-09-17",
    "old_time": "7:00 PM",
    "appointment_id": "optional-stable-id"
  },
  "appointment": {
    "date": "2026-09-20",
    "time": "8:00 PM"
  }
}
```

Use `LockService` around the lookup, availability check, and row mutation so a
concurrent booking cannot change the target or take the destination slot.

## Python fail-safe behavior before deployment

Until the script is upgraded, the Python service:

- verifies the selected appointment against the authoritative date schedule;
- refuses cancellation/rescheduling when more than one appointment exists for
  the patient on that date;
- rejects echoed identifiers that do not match the requested target;
- re-reads the schedule after a positive response and returns `ok: false` if
  the requested post-condition cannot be verified;
- treats the current script's missing reschedule response as unconfirmed.

Deploying Apps Script is a separate manual production step. Updating this
repository or merging PR #2 does not deploy the script.
