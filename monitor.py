#!/usr/bin/env python3
import os
import sys
import json
import logging
from datetime import datetime
from typing import Tuple, Optional, Dict, Any, List
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# HTML parsing
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

import smtplib
from email.message import EmailMessage

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Email config (from env)
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

# === CONSTANTS ===
PRODUCT_URL = "https://www.keychron.com/products/keychron-k5-ultra-8k-wireless-custom-mechanical-keyboard"

# Require this many consecutive confirmations before notifying (use 2 for production)
CONFIRMATIONS = 2

# State file
LAST_STATE_FILE = "last_state.json"

# Keywords
HIGH_KEYWORDS = ["nordic", "danish", "danish layout", "iso nordic", "scandinavian"]
LOW_KEYWORDS = ["iso", "ansi"]  # low-confidence; do not notify on these alone

# Whether the script should exit non-zero on unexpected errors (env)
FAIL_ON_ERROR = os.environ.get("FAIL_ON_ERROR", "0") == "1"
# === end CONSTANTS ===


def make_session_with_retries(retries: int = 4, backoff_factor: float = 1.0, timeout: int = 20) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        backoff_factor=backoff_factor,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "keychron-dk-monitor/1.0 (+https://github.com/MadsR/keychron-dk-monitor)"
    })
    session.request_timeout = timeout
    return session


def safe_get(session: requests.Session, url: str) -> Optional[requests.Response]:
    try:
        resp = session.get(url, timeout=getattr(session, "request_timeout", 20))
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        body_snippet = (e.response.text[:1000] if (e.response is not None and e.response.text) else "")
        logging.warning("HTTP error fetching %s: %s. Body (truncated): %s", url, status, body_snippet)
        return None
    except requests.exceptions.RequestException as e:
        logging.error("Network error fetching %s: %s", url, e, exc_info=False)
        return None


def contains_high_keyword(text: str) -> Optional[str]:
    if not text:
        return None
    l = text.lower()
    for kw in HIGH_KEYWORDS:
        if kw in l:
            return kw
    return None


def contains_low_keyword(text: str) -> Optional[str]:
    if not text:
        return None
    l = text.lower()
    for kw in LOW_KEYWORDS:
        if kw in l:
            return kw
    return None


def try_product_json_endpoints(session: requests.Session, url: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    candidates = [url + ".json", urljoin(url, "index.json"), urljoin(url, ".json")]
    for c in candidates:
        try:
            r = session.get(c, timeout=getattr(session, "request_timeout", 20))
            if r.status_code == 200 and "json" in r.headers.get("content-type", "").lower():
                try:
                    payload = r.json()
                    return payload, f"product_json:{c}"
                except Exception:
                    continue
        except Exception:
            continue
    return None, None


def _recursive_search_for_keywords(obj: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Recursively search a JSON-like object for HIGH keywords (preferred) and
    low keywords. Returns (high_match, low_match) where each is the matched keyword or None.
    """
    high = None
    low = None
    if isinstance(obj, str):
        if not high:
            high = contains_high_keyword(obj)
        if not low:
            low = contains_low_keyword(obj)
        return high, low
    if isinstance(obj, dict):
        for k, v in obj.items():
            # check keys
            if isinstance(k, str):
                if not high:
                    high = contains_high_keyword(k)
                if not low:
                    low = contains_low_keyword(k)
            # recurse into value
            h, l = _recursive_search_for_keywords(v)
            if h and not high:
                high = h
            if l and not low:
                low = l
            if high:
                return high, low
        return high, low
    if isinstance(obj, list):
        for item in obj:
            h, l = _recursive_search_for_keywords(item)
            if h and not high:
                high = h
            if l and not low:
                low = l
            if high:
                return high, low
        return high, low
    return None, None


def inspect_product_json(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    if not payload:
        return None, None
    return _recursive_search_for_keywords(payload)


def parse_json_ld(soup) -> Tuple[Optional[Any], Optional[str]]:
    if soup is None:
        return None, None
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            return data, "json-ld"
        except Exception:
            continue
    return None, None


def inspect_html_for_layouts(html_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (high_match, low_match).
    high_match is a HIGH_KEYWORD found in a relevant context (option under a layout-like select, JSON-LD, label, etc).
    low_match is a LOW_KEYWORD found (e.g. 'iso') but not accompanied by HIGH keyword.
    We only treat HIGH as a true availability indicator.
    """
    if not html_text:
        return None, None
    if BeautifulSoup is None:
        # fallback to plain text
        return contains_high_keyword(html_text), contains_low_keyword(html_text)

    soup = BeautifulSoup(html_text, "lxml")

    # 1) Inspect selects/options — only consider options in selects that look like layout selectors
    for select in soup.find_all("select"):
        # derive a contextual label for the select
        select_label = (select.get("name") or select.get("id") or "")
        # also check nearby text: previous headings or labels
        prev = select.find_previous(["label", "h2", "h3", "h4", "p", "span"])
        prev_text = (prev.get_text(" ", strip=True) if prev else "") or ""
        context = " ".join([select_label, prev_text]).lower()

        # consider this select relevant if context mentions layout/keyboard/format/locale/etc
        if any(kw in context for kw in ("layout", "keyboard", "format", "locale", "language", "variant")):
            for option in select.find_all("option"):
                text = (option.get_text(" ", strip=True) or "")
                high = contains_high_keyword(text)
                if high:
                    return high, None
                low = contains_low_keyword(text)
                if low:
                    # record low but don't return as high
                    return None, low

    # 2) Inspect radio/label groups similarly (check label text)
    for label in soup.find_all("label"):
        text = (label.get_text(" ", strip=True) or "")
        high = contains_high_keyword(text)
        if high:
            return high, None

    # 3) JSON-LD inside HTML
    ld, src = parse_json_ld(soup)
    if ld:
        high, low = inspect_product_json(ld)
        if high:
            return high, low
        if low:
            return None, low

    # 4) Fallback: search the body for high keywords (less authoritative)
    body_text = soup.get_text(" ", strip=True)[:200000]
    high = contains_high_keyword(body_text)
    if high:
        return high, None
    low = contains_low_keyword(body_text)
    if low:
        return None, low

    return None, None


def detect_nordic_layout(session: requests.Session, url: str) -> Tuple[bool, str]:
    """
    Returns (found, evidence) where found is True only when a HIGH_KEYWORD is detected
    in an appropriate context (not merely 'ISO' alone).
    """
    resp = safe_get(session, url)
    if resp is None:
        return False, f"fetch-failed:{url}"

    # 1) product JSON endpoints
    payload, src = try_product_json_endpoints(session, url)
    if payload:
        high, low = inspect_product_json(payload)
        if high:
            return True, f"{src}:high:{high}"
        if low:
            # log low and continue to HTML parsing
            logging.info("%s low-confidence keyword in product JSON: %s", url, low)

    # 2) JSON-LD and HTML parsing
    html = resp.text
    high, low = inspect_html_for_layouts(html)
    if high:
        return True, f"html:high:{high}"
    if low:
        logging.info("%s low-confidence only (e.g. ISO present) - ignoring for notification: %s", url, low)
        return False, f"html:low:{low}"

    # 3) raw fallback
    fk = contains_high_keyword(html)
    if fk:
        return True, f"raw_body:high:{fk}"

    return False, "no-evidence"


def load_last_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"found": False, "consecutive": 0, "evidence": [], "updated_at": None}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        logging.warning("Failed to read last state file, starting fresh: %s", path)
        return {"found": False, "consecutive": 0, "evidence": [], "updated_at": None}


def save_last_state(path: str, state: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("Failed to write state file %s: %s", path, e)


def send_mail(subject: str, body: str) -> bool:
    if not (EMAIL_FROM and EMAIL_PASSWORD and EMAIL_TO):
        logging.error("Email credentials not configured. Skipping send_mail.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        logging.info("Attempting to send email to %s", EMAIL_TO)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logging.info("Notification email sent to %s", EMAIL_TO)
        return True
    except Exception as e:
        logging.error("Failed to send email: %s", e, exc_info=True)
        return False


def run_check() -> int:
    session = make_session_with_retries()
    found, evidence = detect_nordic_layout(session, PRODUCT_URL)
    logging.info("Checked %s -> found=%s evidence=%s", PRODUCT_URL, found, evidence)

    # Load/update state
    state = load_last_state(LAST_STATE_FILE)
    prev_found = bool(state.get("found"))
    prev_consecutive = int(state.get("consecutive", 0))

    if found:
        new_consecutive = prev_consecutive + 1
    else:
        new_consecutive = 0

    notify = False
    now = datetime.utcnow().isoformat() + "Z"

    if found and new_consecutive >= CONFIRMATIONS and not prev_found:
        notify = True
        subject = "Keychron K5 Ultra — Nordic layout detected"
        body = f"Detected Nordic/Danish layout on {PRODUCT_URL}\n\nEvidence:\n{evidence}\n\nConfirmed {new_consecutive} consecutive times.\n\nTime: {now}"
        state["found"] = True
        state["consecutive"] = new_consecutive
        state["evidence"] = [evidence]
        state["updated_at"] = now
    elif not found and prev_found:
        notify = True
        subject = "Keychron K5 Ultra — Nordic layout no longer detected"
        body = f"Previously-detected Nordic layout is no longer present on {PRODUCT_URL}\n\nTime: {now}"
        state["found"] = False
        state["consecutive"] = 0
        state["evidence"] = []
        state["updated_at"] = now
    else:
        state["consecutive"] = new_consecutive
        state["evidence"] = state.get("evidence", [])
        state["updated_at"] = now

    save_last_state(LAST_STATE_FILE, state)

    if notify:
        ok = send_mail(subject, body)
        if not ok and FAIL_ON_ERROR:
            logging.error("Failed to send notification and FAIL_ON_ERROR is set.")
            return 1

    return 0


if __name__ == "__main__":
    try:
        if BeautifulSoup is None:
            logging.warning("BeautifulSoup is not installed. Install beautifulsoup4 and lxml for best detection results.")
        exit_code = run_check()
        sys.exit(exit_code)
    except Exception as e:
        logging.exception("Unhandled exception in monitor: %s", e)
        if FAIL_ON_ERROR:
            sys.exit(1)
        else:
            sys.exit(0)
