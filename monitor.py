import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import smtplib
from email.message import EmailMessage
import os
import sys

# Configuration from environment
EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

URL = "https://www.keychron.com/products/keychron-k5-ultra-8k-wireless-custom-mechanical-keyboard"

WORDS = [
    "Nordic",
    "Danish",
    "ISO Nordic",
    "ANSI",
    "ISO"
]

# Configure logging so CI output shows warnings/errors
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

def make_session_with_retries(retries: int = 4, backoff_factor: float = 1.0, timeout: int = 20):
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "OPTIONS"],
        backoff_factor=backoff_factor,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "keychron-dk-monitor/1.0 (+https://github.com/MadsR/keychron-dk-monitor)"
    })
    # store default timeout on session for convenience
    session.request_timeout = timeout
    return session

def safe_get(session: requests.Session, url: str):
    try:
        resp = session.get(url, timeout=getattr(session, "request_timeout", 20))
        # If server returns 5xx after retries (or 4xx), do not let the exception crash the script
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        body_snippet = (e.response.text[:1000] if (e.response is not None and e.response.text) else "")
        logging.warning("HTTP error fetching %s: %s. Body (truncated): %s", url, status, body_snippet)
        return None
    except requests.exceptions.RequestException as e:
        logging.error("Request exception fetching %s: %s", url, e, exc_info=False)
        return None

def check_keychron():
    session = make_session_with_retries()
    response = safe_get(session, URL)
    if response is None:
        # treat as transient/unavailable; caller can decide what to do
        return False, None

    page = response.text.lower()
    for word in WORDS:
        if word.lower() in page:
            # return the original word (non-lowered) for more readable emails/logs
            return True, word

    return False, None

def send_mail(found_word):
    msg = EmailMessage()
    msg["Subject"] = "Keychron K5 Ultra Nordic fundet!"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.set_content(
        f"""
Keychron K5 Ultra ser ud til at have fået dansk/nordisk layout.

Fundet ord:
{found_word}

Link:
{URL}
"""
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logging.info("Notification email sent to %s", EMAIL_TO)
    except Exception as e:
        logging.error("Failed to send notification email: %s", e, exc_info=False)

if __name__ == "__main__":
    found, word = check_keychron()
    logging.info("Result: %s %s", found, word)
    if found:
        send_mail(word)
    # exit 0 in all normal cases so a transient 500 won't fail the workflow;
    # if you want the workflow to fail on persistent errors, return a non-zero exit code here.
    sys.exit(0)
