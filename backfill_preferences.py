import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from google import genai
from google.genai import types


DATABASE_URL = os.environ["DATABASE_URL"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

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
        role = msg["role"]
        content = msg["content"]

        conversation.append(
            f"{role}: {content}"
        )

    chat_text = "\n".join(conversation)

    # Avoid sending absurdly huge chats.
    # Keep the latest ~30k chars for this simple version.
    if len(chat_text) > 30000:
        chat_text = chat_text[-30000:]

    prompt = f"""
You are reviewing an old WhatsApp conversation between a clinic and a patient.

Extract ONLY useful, durable information that a clinic receptionist could use later.

Existing patient name:
{existing_name or "unknown"}

Existing staff notes:
{existing_preferences or "none"}

Important rules:
- Do not invent anything.
- Do not diagnose medical conditions.
- Do not infer sensitive medical facts.
- Ignore greetings, temporary conversation details, and irrelevant chatter.
- Prefer explicit facts stated by the patient.
- Keep the result short.
- Do not repeat existing notes unless useful for context.
- If there is no useful information, return an empty string.

Useful examples:
- stated patient name
- treatment areas they ask about
- services they are interested in
- preferred appointment times/days
- communication preferences
- other stable non-sensitive preferences

Return JSON only in this format:

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
        return {
            "name": "",
            "preferences": "",
        }

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("Invalid JSON from Gemini:")
        print(text)
        return {
            "name": "",
            "preferences": "",
        }


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

    # Preserve existing name if already present.
    final_name = existing_name or extracted_name

    # Keep existing notes and append only new extracted info.
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


def main():
    patients = get_patients()
    patients = patients[:10]

    print(f"Found {len(patients)} patients")

    for index, patient in enumerate(patients, start=1):
        phone = patient["phone_number"]

        print(
            f"[{index}/{len(patients)}] Processing {phone}"
        )

        try:
            messages = get_chat_history(phone)

            if not messages:
                print("  No messages, skipping")
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

            print("  Saved")

        except Exception as exc:
            print(
                f"  ERROR for {phone}: {exc}"
            )

    print("Done")


if __name__ == "__main__":
    main()
