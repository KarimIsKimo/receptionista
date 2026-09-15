import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types


DATABASE_URL = os.environ["DATABASE_URL"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
LIMIT = int(os.getenv("LIMIT", "10"))

client = genai.Client(api_key=GEMINI_API_KEY)


def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
    )


def get_patients():
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT phone_number
                FROM chat_history
                WHERE phone_number IS NOT NULL
                ORDER BY phone_number
            """)
            return cur.fetchall()


def get_chat_history(phone_number):
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT role, content
                FROM chat_history
                WHERE phone_number = %s
                ORDER BY id ASC
            """, (phone_number,))
            return cur.fetchall()


def get_existing_patient(phone_number):
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT name, preferences
                FROM patients
                WHERE phone_number = %s
            """, (phone_number,))
            return cur.fetchone()


def extract_patient_info(messages, existing_name="", existing_preferences=""):
    conversation = []

    for msg in messages:
        conversation.append(
            f"{msg['role']}: {msg['content']}"
        )

    chat_text = "\n".join(conversation)

    # Keep this simple and cheap for old chats.
    if len(chat_text) > 30000:
        chat_text = chat_text[-30000:]

    prompt = f"""
You are reviewing an old WhatsApp conversation between a clinic and a patient.

Extract ONLY useful, durable information that a clinic receptionist could use later.

Existing patient name:
{existing_name or "unknown"}

Existing notes:
{existing_preferences or "none"}

Rules:
- Do not invent anything.
- Do not diagnose medical conditions.
- Do not infer sensitive medical facts.
- Ignore greetings and temporary conversation details.
- Prefer explicit facts stated by the patient.
- Keep preferences short and useful.
- If there is no useful information, return an empty preferences string.
- If an existing patient name already exists, do not replace it.

Useful information includes:
- explicitly stated patient name
- treatment areas mentioned
- services they are interested in
- preferred appointment times or days
- communication preferences
- stable non-medical preferences

Return JSON only:

{{
  "name": "",
  "preferences": ""
}}

Conversation:
{chat_text}
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    text = (response.text or "").strip()

    if not text:
        return {"name": "", "preferences": ""}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("Invalid JSON:", text)
        return {"name": "", "preferences": ""}


def update_patient(phone_number, extracted, existing):
    extracted_name = str(
        extracted.get("name") or ""
    ).strip()

    extracted_preferences = str(
        extracted.get("preferences") or ""
    ).strip()

    existing_name = (
        existing.get("name") if existing else ""
    ) or ""

    existing_preferences = (
        existing.get("preferences") if existing else ""
    ) or ""

    final_name = existing_name or extracted_name

    if extracted_preferences:
        if existing_preferences:
            if extracted_preferences.lower() in existing_preferences.lower():
                final_preferences = existing_preferences
            else:
                final_preferences = (
                    existing_preferences
                    + " | "
                    + extracted_preferences
                )
        else:
            final_preferences = extracted_preferences
    else:
        final_preferences = existing_preferences

    print("  Final name:", final_name)
    print("  Final preferences:", final_preferences)

    if DRY_RUN:
        print("  DRY RUN - nothing written")
        return

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO patients (
                    phone_number,
                    name,
                    preferences
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (phone_number)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    preferences = EXCLUDED.preferences,
                    updated_at = NOW()
            """, (
                phone_number,
                final_name,
                final_preferences,
            ))

        conn.commit()

    print("  SAVED")


def main():
    patients = get_patients()

    if LIMIT > 0:
        patients = patients[:LIMIT]

    print(f"Processing {len(patients)} patients")
    print("DRY_RUN =", DRY_RUN)

    for index, patient in enumerate(patients, start=1):
        phone = patient["phone_number"]

        print("")
        print(f"[{index}/{len(patients)}] {phone}")

        try:
            messages = get_chat_history(phone)

            if not messages:
                print("  No messages")
                continue

            existing = get_existing_patient(phone) or {}

            extracted = extract_patient_info(
                messages,
                existing_name=existing.get("name", ""),
                existing_preferences=existing.get("preferences", ""),
            )

            print("  Extracted:", extracted)

            update_patient(
                phone,
                extracted,
                existing,
            )

        except Exception as exc:
            print("  ERROR:", exc)

    print("")
    print("Finished")


if __name__ == "__main__":
    main()
