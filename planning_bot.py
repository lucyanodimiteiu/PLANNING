#!/usr/bin/env python3
"""
Alert My Driver → Telegram Bot
Citeste emailuri de planning de la alertmydriver.com,
le traduce in romana si trimite rezumat pe Telegram.
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
import imaplib
import email
import os
import json
import re
import requests
import pytz
from email.header import decode_header
from datetime import datetime, timezone

# ── Configuratie din GitHub Secrets ────────────────────────────────────────
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY   = os.environ["DEEPSEEK_API_KEY"]

# Fisier local pentru a retine ID-urile deja trimise (via GitHub Actions cache)
PROCESSED_FILE = "processed_ids.json"

# ── Incarca ID-urile deja procesate ─────────────────────────────────────────
def load_processed():
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_processed(ids: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(ids), f)

# ── Conectare Gmail IMAP ─────────────────────────────────────────────────────
def fetch_new_emails(processed_ids: set) -> list:
    """Returneaza emailuri noi de la alertmydriver.com"""
    print("Conectare la IMAP...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    print("Logare...")
    mail.login(GMAIL_USER, GMAIL_PASSWORD)
    print("Selectare inbox...")
    mail.select("inbox")

    print("Cautare emailuri...")
    _, data = mail.search(None, 'FROM', '"no-reply@alertmydriver.com"')
    all_ids = data[0].split()
    print(f"Gasite {len(all_ids)} emailuri de la alertmydriver.")

    new_emails = []
    for uid in reversed(all_ids[-5:]):  # ultimele 5, cele mai noi primul
        uid_str = uid.decode()
        if uid_str in processed_ids:
            continue

        _, msg_data = mail.fetch(uid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        subject = decode_header(msg["Subject"])[0][0]
        if isinstance(subject, bytes):
            subject = subject.decode()

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ["text/plain", "text/html"]:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    if part.get_content_type() == "text/plain":
                        break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

        email_date = msg["Date"]
        parsed_date = email.utils.parsedate_to_datetime(email_date)
        
        import pytz
        amsterdam_tz = pytz.timezone('Europe/Amsterdam')
        
        if parsed_date.tzinfo is None:
            parsed_date = pytz.utc.localize(parsed_date)
            
        local_date = parsed_date.astimezone(amsterdam_tz)
        formatted_local_date = local_date.strftime("%d-%m-%Y %H:%M:%S")

        new_emails.append({
            "uid": uid_str,
            "subject": subject,
            "body": body.strip(),
            "date": formatted_local_date,
        })

    mail.logout()
    return new_emails

# ── Parser pentru campurile din email ────────────────────────────────────────
def parse_planning(body: str) -> dict:
    """Extrage campurile structurate din emailul de planning."""
    def find(pattern):
        m = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else "—"

    return {
        "kenteken":     find(r"Kenteken[:\t]+(.+)"),
        "trailer":      find(r"Trailer[:\t]+(.+)"),
        "starttijd":    find(r"Starttijd[:\t]+(.+)"),
        "omschrijving": find(r"Omschrijving[:\t]+(.+)"),
        "opmerking":    find(r"OPMERKING[:\t]+(.+)"),
        "laden":        find(r"Laden[:\t]+(.+)"),
        "lossen":       find(r"Lossen[:\t]+(.+)"),
    }

# ── Traducere + rezumat cu DeepSeek ──────────────────────────────────────────
def summarize_with_deepseek(subject: str, body: str) -> str:
    """Trimite emailul la DeepSeek si returneaza un rezumat in romana."""
    prompt = f"""Esti asistentul unui sofer de camion roman care lucreaza in Olanda.
Primesti un email de planning de transport in limba olandeza.
Traduce si rezuma in romana, clar si concis, sub forma de lista, structurat pe curse (Cursa 1, Cursa 2 etc.).

REGULI OBLIGATORII:
1. Pastreaza toate detaliile: ora de start, numar camion, trailer, remarci speciale.
2. Pentru absolut FIECARE adresa de INCARCARE sau DESCARCARE, respecta STRICT urmatorul format:
   [Numele Companiei], [Strada si Numarul], [Cod Postal si Localitatea], [Numele Tarii in romana] [Steagul Tarii Emoji]
   Exemplu: Trans-Imex, Doornhoek 4025, 5465 TD Veghel, Olanda 🇳🇱
3. Foloseste emoji-uri relevante (🚚 pentru incarcare, 📦 pentru descarcare).
4. Daca exista detalii despre marfa (ex: nr de coli, greutate, referinte), adauga-le la finalul fiecarei curse.
5. NU folosi Markdown (** sau *). Daca vrei sa ingrosi un text, foloseste taguri HTML: <b>text</b>.
6. Asigura-te ca nu pui taguri HTML neinchise (ex: < sau > singure).

Subiect email: {subject}

Continut email:
{body}

Scrie rezumatul in romana, usor de citit pe telefon."""

    response = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.3,
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()

# ── Trimitere mesaj Telegram ──────────────────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT,
        "text": text,
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=15)
    
    # Auto-handle supergroup migration
    if r.status_code == 400:
        try:
            err_data = r.json()
            if err_data.get("parameters", {}).get("migrate_to_chat_id"):
                new_chat_id = err_data["parameters"]["migrate_to_chat_id"]
                print(f"Chat migrated! Retrying with new chat_id: {new_chat_id}")
                payload["chat_id"] = new_chat_id
                r = requests.post(url, json=payload, timeout=15)
        except Exception:
            pass

    if r.status_code != 200:
        print(f"Telegram error response: {r.text}")
    r.raise_for_status()

# ── Construieste mesajul Telegram ────────────────────────────────────────────
def build_telegram_message(subject: str, parsed: dict, summary: str, date: str) -> str:
    import html
    s_sub = html.escape(subject)
    s_date = html.escape(str(date))
    s_k = html.escape(parsed['kenteken'])
    s_t = html.escape(parsed['trailer'])
    s_st = html.escape(parsed['starttijd'])
    s_l = html.escape(parsed['laden'])
    s_lo = html.escape(parsed['lossen'])
    s_o = html.escape(parsed['opmerking'])
    
    lines = [
        f"📋 <b>PLANNING NOU — {s_sub}</b>",
        f"📅 Primit: {s_date}",
        "",
        f"🚛 <b>Camion:</b> {s_k}",
        f"🔗 <b>Trailer:</b> {s_t}",
        f"⏰ <b>Start:</b> {s_st}",
        f"📦 <b>Incarcare:</b> {s_l}",
        f"📍 <b>Descarcare:</b> {s_lo}",
    ]
    if s_o != "—":
        lines.append(f"⚠️ <b>Observatie:</b> {s_o}")

    # Fix potential markdown ** leakage from deepseek (convert to <b>)
    import re
    summary = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', summary)
    
    # Telegram uraste tagurile neinchise sau ciudate. Daca mai sunt bold markdown sau altele, macar nu sunt taguri HTML.

    lines += [
        "",
        "─────────────────────",
        "🤖 <b>Rezumat in romana:</b>",
        "",
        summary,
    ]
    return "\n".join(lines)

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Verificare emailuri noi...")
    processed = load_processed()

    new_emails = fetch_new_emails(processed)
    print(f"  → {len(new_emails)} emailuri noi gasite.")

    for em in new_emails:
        try:
            print(f"  Procesez: {em['subject']}")
            parsed  = parse_planning(em["body"])
            summary = summarize_with_deepseek(em["subject"], em["body"])
            message = build_telegram_message(
                em["subject"], parsed, summary, em["date"]
            )
            send_telegram(message)
            processed.add(em["uid"])
            print(f"  ✓ Trimis pe Telegram.")
        except Exception as e:
            print(f"  ✗ Eroare la {em['subject']}: {e}")

    save_processed(processed)
    print("  Done.")

if __name__ == "__main__":
    main()
