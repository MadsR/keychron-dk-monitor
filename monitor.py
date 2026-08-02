#!/usr/bin/env python3
import os
import sys
import json
import time
import logging
from datetime import datetime
from typing import Tuple, Optional, Dict, Any, List
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional: BeautifulSoup is used for HTML parsing. Add to requirements: beautifulsoup4, lxml
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None  # we'll handle missing dependency gracefully

import smtplib
from email.message import EmailMessage

# Configuration from environment
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

# Main product URL to check (the original product page)
PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://www.keychron.com/products/keychron-k5-ultra-8k-wireless-custom-mechanical-keyboard"
)

# Optional list of retailer product pages to check as well (comma-separated env var)
RETAILER_PAGES = [
    u.strip() for u in os.environ.get("RETAILER_PAGES", "").split(",") if u.strip()
]

# How many consecutive positive runs are required before sending a notification
CONSECUTIVE_REQUIRED = int(os.environ.get("CONFIRMATIONS", "2"))

# Whether the script should exit non-zero on unexpected errors
FAIL_ON_ERROR = os.environ.get("FAIL_ON_ERROR", "0") == "1"

# File to persist last-known state (written into workflow workspace)
LAST_STATE_FILE = os.environ.get("LAST_STATE_FILE", "last_state.json")

# Keywords to detect Nordic (case-insensitive)
KEYWORDS = ["nordic", "danish", "iso nordic", "danish layout", "iso"]

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


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


def _search_keywords_in_text(text: str, keywords: List[str]) -> Optional[str]:
    ltext = text.lower()
    for kw in keywords:
        if kw in ltext:
            return kw
    return None


def try_product_json_endpoints(session: requests.Session, url: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # Common patterns: append .json or index.json (works for some stores like Shopify)
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


def _recursive_search_for_keywords(obj: Any, keywords: List[str]) -> Optional[str]:
    # Walk dicts/lists/strings recursively to find keywords; return the first matching snippet
    if isinstance(obj, str):
        found = _search_keywords_in_text(obj, keywords)
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            # check key and value (string keys may contain 'layout' etc)
            if isinstance(k, str):
                fk = _search_keywords_in_text(k, keywords)
                if fk:
                    return fk
            res = _recursive_search_for_keywords(v, keywords)
            if res:
                return res
    if isinstance(obj, list):
        for item in obj:
            res = _recursive_search_for_keywords(item, keywords)
            if res:
                return res
    return None


def inspect_product_json(payload: Any, keywords: List[str]) -> Optional[str]:
    # Look for variant/option names and values and arbitrary strings containing keywords
    if not payload:
        return None
    # Many product JSONs put data under product, product->variants/options, or are the product object directly.
    # Use recursive search to be generic.
    return _recursive_search_for_keywords(payload, keywords)


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


def inspect_html_for_layouts(html_text: str, keywords: List[str]) -> Optional[str]:
    if not html_text:
        return None
    if BeautifulSoup is None:
        # Fallback to plain text search if BeautifulSoup isn't installed
        return _search_keywords_in_text(html_text, keywords)
    soup = BeautifulSoup(html_text, "lxml")
    # 1) Look at select/options (common for variant selections)
    for select in soup.find_all("select"):
        name = (select.get("name") or select.get("id") or "") .lower()
        # if select name or nearby label contains 'layout' or 'variant'
        if "layout" in name or "keyboard" in name or "variant" in name or "option" in name:
            for option in select.find_all("option"):
                text = (option.text or "").strip()
                if text and _search_keywords_in_text(text, keywords):
                    return f"select:{name}:{text}"
    # 2) Look for radio/label groups
    for label in soup.find_all("label"):
        text = (label.get_text(" ", strip=True) or "")
        if text and _search_keywords_in_text(text, keywords):
            return f"label:{text[:100]}"
    # 3) JSON-LD
    ld, src = parse_json_ld(soup)
    if ld:
        fk = inspect_product_json(ld, keywords)
        if fk:
            return f"{src}:{fk}"
    # 4) Fallback: page text search
    page_text = soup.get_text(" ", strip=True)[:200000]  # cap
    fk = _search_keywords_in_text(page_text, keywords)
    if fk:
        return f"body:{fk}"
    return None


def detect_nordic_layout(session: requests.Session, url: str, keywords: List[str]) -> Tuple[bool, str]:
    """
    Try multiple strategies to detect presence of Nordic/Danish/ISO layout.
    Returns (found, evidence_string).
    """
    # 0) quick fetch (with retries inside session)
    resp = safe_get(session, url)
    if resp is None:
        return False, f"fetch-failed:{url}"

    # 1) product JSON endpoints
    payload, src = try_product_json_endpoints(session, url)
    if payload:
        match = inspect_product_json(payload, keywords)
        if match:
            return True, f"{src}:{match}"

    # 2) JSON-LD and HTML parsing
    html = resp.text
    if BeautifulSoup:
        soup = BeautifulSoup(html, "lxml")
    else:
        soup = None

    # JSON-LD
    ld, ld_src = parse_json_ld(soup)
    if ld:
        match = inspect_product_json(ld, keywords)
        if match:
            return True, f"{ld_src}:{match}"

    # HTML selects/options, labels, page text
    match = inspect_html_for_layouts(html, keywords)
    if match:
        return True, f"html:{match}"

    # 3) Fallback: attempt to find keywords in raw text
    fk = _search_keywords_in_text(html, keywords)
    if fk:
        return True, f"raw_body:{fk}"

    # If nothing found
    return False, "no-evidence"


def load_last_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"found": False, "consecutive": 0, "evidence": None, "updated_at": None}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        logging.warning("Failed to read last state file, starting fresh: %s", path)
        return {"found": False, "consecutive": 0, "evidence": None, "updated_at": None}


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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logging.info("Notification email sent to %s", EMAIL_TO)
        return True
    except Exception as e:
        logging.error("Failed to send email: %s", e, exc_info=False)
        return False


def run_check() -> int:
    session = make_session_with_retries()
    urls_to_check = [PRODUCT_URL] + RETAILER_PAGES

    overall_found = False
    evidence_items: List[str] = []

    for u in urls_to_check:
        found, evidence = detect_nordic_layout(session, u, KEYWORDS)
        logging.info("Checked %s -> found=%s evidence=%s", u, found, evidence)
        if found:
            overall_found = True
            evidence_items.append(f"{u} -> {evidence}")

    # Load and update state
    state = load_last_state(LAST_STATE_FILE)
    prev_found = bool(state.get("found"))
    prev_consecutive = int(state.get("consecutive", 0))

    # Determine new consecutive count
    if overall_found:
        new_consecutive = prev_consecutive + 1
    else:
        new_consecutive = 0

    # Action: only notify when we've reached the confirmation threshold and previously it wasn't confirmed
    notify = False
    subject = ""
    body = ""
    now = datetime.utcnow().isoformat() + "Z"

    if overall_found and new_consecutive >= CONSECUTIVE_REQUIRED and not prev_found:
        notify = True
        subject = "Keychron K5 Ultra — Nordic layout detected"
        body = f"Detected Nordic/Danish layout on {PRODUCT_URL}\n\nEvidence:\n" + "\n".join(evidence_items) + f"\n\nConfirmed {new_consecutive} consecutive times.\n\nTime: {now}"
        state["found"] = True
        state["consecutive"] = new_consecutive
        state["evidence"] = evidence_items
        state["updated_at"] = now
    elif not overall_found and prev_found:
        # We previously had a confirmed availability but it's gone now; notify once
        notify = True
        subject = "Keychron K5 Ultra — Nordic layout no longer detected"
        body = f"Previously-detected Nordic layout is no longer present on {PRODUCT_URL}\n\nChecked pages and found no evidence.\n\nTime: {now}"
        state["found"] = False
        state["consecutive"] = 0
        state["evidence"] = evidence_items
        state["updated_at"] = now
    else:
        # update consecutive count but do not notify
        state["consecutive"] = new_consecutive
        state["evidence"] = evidence_items
        state["updated_at"] = now
        # keep state['found'] unchanged unless we crossed threshold above

    save_last_state(LAST_STATE_FILE, state)

    if notify:
        ok = send_mail(subject, body)
        if not ok and FAIL_ON_ERROR:
            logging.error("Failed to send notification and FAIL_ON_ERROR is set.")
            return 1

    # Do not fail the run on transient fetch errors; return 0 normally.
    return 0


if __name__ == "__main__":
    try:
        if BeautifulSoup is None:
            logging.warning(
                "BeautifulSoup is not installed. Install beautifulsoup4 and lxml for best detection results."
            )
        exit_code = run_check()
        sys.exit(exit_code)
    except Exception as e:
        logging.exception("Unhandled exception in monitor: %s", e)
        if FAIL_ON_ERROR:
            sys.exit(1)
        else:
            sys.exit(0)
