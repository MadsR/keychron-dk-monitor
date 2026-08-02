#!/usr/bin/env python3
import os
import sys
import json
import logging
from datetime import datetime, timezone
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

# === CONFIGURATION / CONSTANTS ===
# Product to monitor (K5 Ultra)
PRODUCT_URL = "https://www.keychron.com/products/keychron-k5-ultra-8k-wireless-custom-mechanical-keyboard"

# How many consecutive confirmations required to notify (2 is safer; set to 1 for testing)
CONFIRMATIONS = 2

# Persisted state filename (workflow uploads/downloads this artifact)
LAST_STATE_FILE = "last_state.json"

# Email configuration is read from secrets/environment:
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

# Keyword lists: HIGH keywords indicate Nordic availability, LOW keywords are generic and ignored alone
HIGH_KEYWORDS = ["nordic", "danish", "danish layout", "iso nordic", "scandinavian"]
LOW_KEYWORDS = ["iso", "ansi"]

# Fail hard on notification errors if set in env
FAIL_ON_ERROR = os.environ.get("FAIL_ON_ERROR", "0") == "1"
# === end CONFIGURATION ===


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
    Recursively search JSON-like structure for HIGH and LOW keywords.
    Returns (high_match, low_match) - first found occurrences.
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
            if isinstance(k, str):
                if not high:
                    high = contains_high_keyword(k)
                if not low:
                    low = contains_low_keyword(k)
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


def _extract_json_objects_from_text(text: str) -> List[Any]:
    """
    Simple balanced-brace scanner that attempts to parse {...} JSON objects found in text.
    It will succeed for embedded JSON (most common case). It is conservative and skips invalid JSON.
    """
    objs: List[Any] = []
    if not text:
        return objs
    n = len(text)
    i = 0
    while i < n:
        if text[i] != '{':
            i += 1
            continue
        depth = 0
        start = i
        j = i
        while j < n:
            ch = text[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:j + 1]
                    try:
                        parsed = json.loads(candidate)
                        objs.append(parsed)
                        i = j + 1
                        break
                    except Exception:
                        pass
            j += 1
        i = start + 1
    return objs


def inspect_inline_script_variants(soup) -> Tuple[Optional[str], Optional[str]]:
    """
    Scan inline <script> tags for embedded JSON objects containing variant/option data.
    Returns (high_match, low_match).
    """
    if soup is None:
        return None, None

    for script in soup.find_all("script"):
        txt = script.string
        if not txt or len(txt) < 50:
            continue
        lower = txt.lower()
        if not any(k in lower for k in ("variant", "variants", "product", "option", "options")):
            continue
        for obj in _extract_json_objects_from_text(txt):
            h, l = _recursive_search_for_keywords(obj)
            if h:
                return h, None
            if l:
                return None, l
    return None, None


def inspect_html_for_layouts(html_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Inspect the HTML for layout selectors and return (high_match, low_match).
    high_match indicates explicit 'nordic'/'danish' etc in relevant context.
    low_match indicates only 'iso'/'ansi' found.
    """
    if not html_text:
        return None, None
    if BeautifulSoup is None:
        # fallback to plain text search
        return contains_high_keyword(html_text), contains_low_keyword(html_text)

    soup = BeautifulSoup(html_text, "lxml")

    # 1) Inspect select elements that are contextually layout/variant selectors
    for select in soup.find_all("select"):
        select_label = (select.get("name") or select.get("id") or "")
        prev = select.find_previous(["label", "h2", "h3", "h4", "p", "span"])
        prev_text = (prev.get_text(" ", strip=True) if prev else "") or ""
        context = " ".join([select_label, prev_text]).lower()

        if any(k in context for k in ("layout", "keyboard", "format", "locale", "language", "variant", "type")):
            for option in select.find_all("option"):
                text = (option.get_text(" ", strip=True) or "")
                high = contains_high_keyword(text)
                if high:
                    return high, None
                low = contains_low_keyword(text)
                if low:
                    return None, low

    # 2) Labels/radio groups
    for label in soup.find_all("label"):
        text = (label.get_text(" ", strip=True) or "")
        high = contains_high_keyword(text)
        if high:
            return high, None

    # 3) JSON-LD
    ld, src = parse_json_ld(soup)
    if ld:
        high, low = inspect_product_json(ld)
        if high:
            return high, low
        if low:
            return None, low

    # 4) Inline scripts (variants embedded in JS)
    inline_high, inline_low = inspect_inline_script_variants(soup)
    if inline_high:
        return inline_high, None
    if inline_low:
        return None, inline_low

    # 5) Fallback: body text
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
    Returns (found, evidence). found=True only when HIGH keyword is found in appropriate context.
    LOW-only findings are logged but do not trigger found=True.
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
            logging.info("%s low-confidence keyword in product JSON: %s", url, low)

    # 2) Parse page HTML and JSON-LD
    html = resp.text
    soup = None
    if BeautifulSoup:
        soup = BeautifulSoup(html, "lxml")

    # JSON-LD check
    ld, ld_src = parse_json_ld(soup) if soup is not None else (None, None)
    if ld:
        high, low = inspect_product_json(ld)
        if high:
            return True, f"{ld_src}:high:{high}"
        if low:
            logging.info("%s low-confidence keyword in JSON-LD: %s", url, low)

    # 3) Inspect inline scripts (variants embedded in JS)
    inline_high, inline_low = inspect_inline_script_variants(soup) if soup is not None else (None, None)
    if inline_high:
        return True, f"inline-script:high:{inline_high}"
    if inline_low:
        logging.info("%s low-confidence in inline scripts: %s", url, inline_low)

    # 4) Inspect HTML selects/labels/body
    high, low = inspect_html_for_layouts(html)
    if high:
        return True, f"html:high:{high}"
    if low:
        logging.info("%s low-confidence only (e.g. ISO/ANSI present) - ignoring for notification: %s", url, low)
        return False, f"html:low:{low}"

    # 5) Raw fallback
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

    state = load_last_state(LAST_STATE_FILE)
    prev_found = bool(state.get("found"))
    prev_consecutive = int(state.get("consecutive", 0))

    if found:
        new_consecutive = prev_consecutive + 1
    else:
        new_consecutive = 0

    notify = False
    now = datetime.now(timezone.utc).isoformat()

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
