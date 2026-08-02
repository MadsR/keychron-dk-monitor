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

# Configure logging early
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Email config (still read from env)
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

# === CONSTANTS ===
# Set the product URL you want to monitor here (constant, not env)
PRODUCT_URL = "https://www.keychron.com/products/keychron-v1-qmk-custom-mechanical-keyboard-iso-layout-collection?variant=40283343487065"

# Set how many consecutive positive checks are required before notifying
CONFIRMATIONS = 2

# Last state file name (kept as env or constant)
LAST_STATE_FILE = os.environ.get("LAST_STATE_FILE", "last_state.json")

# Keywords to detect Nordic
KEYWORDS = ["nordic", "danish", "iso nordic", "danish layout", "iso"]

# Whether the script should exit non-zero on unexpected errors (still env-controlled)
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
        allowed_methods=["HEAD", "GET", "OPTIONS", "GET"],
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
    if isinstance(obj, str):
        return _search_keywords_in_text(obj, keywords)
    if isinstance(obj, dict):
        for k, v in obj.items():
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
    if not payload:
        return None
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
        return _search_keywords_in_text(html_text, keywords)
    soup = BeautifulSoup(html_text, "lxml")

    # 1) Check select/options
    for select in soup.find_all("select"):
        name = (select.get("name") or select.get("id") or "").lower()
        if "layout" in name or "keyboard" in name or "variant" in name or "option" in name:
            for option in select.find_all("option"):
                text = (option.text or "").strip()
                if text and _search_keywords_in_text(text, keywords):
                    return f"select:{name}:{text}"

    # 2) Labels/radios
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

    # 4) body text fallback
    page_text = soup.get_text(" ", strip=True)[:200000]
    fk = _search_keywords_in_text(page_text, keywords)
    if fk:
        return f"body:{fk}"

    return None


def detect_nordic_layout(session: requests.Session, url: str, keywords: List[str]) -> Tuple[bool, str]:
    resp = safe_get(session, url)
    if resp is None:
        return False, f"fetch-failed:{url}"

    payload, src = try_product_json_endpoints(session, url)
    if payload:
        match = inspect_product_json(payload, keywords)
        if match:
            return True, f"{src}:{match}"

    html = resp.text
    if BeautifulSoup:
        soup = BeautifulSoup(html, "lxml")
    else:
        soup = None

    ld, ld_src = parse_json_ld(soup)
    if ld:
        match = inspect_product_json(ld, keywords)
        if match:
            return True, f"{ld_src}:{match}"

    match = inspect_html_for_layouts(html, keywords)
    if match:
        return True, f"html:{match}"

    fk = _search_keywords_in_text(html, keywords)
    if fk:
        return True, f"raw_body:{fk}"

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
    urls_to_check = [PRODUCT_URL]  # constants only; retailer pages can be added in the file if desired

    overall_found = False
    evidence_items: List[str] = []

    # Skip empty urls defensively
    for u in urls_to_check:
        if not u or str(u).strip() == "":
            logging.debug("Skipping empty URL entry")
            continue

        found, evidence = detect_nordic_layout(session, u, KEYWORDS)
        logging.info("Checked %s -> found=%s evidence=%s", u, found, evidence)
        if found:
            overall_found = True
            evidence_items.append(f"{u} -> {evidence}")

    state = load_last_state(LAST_STATE_FILE)
    prev_found = bool(state.get("found"))
    prev_consecutive = int(state.get("consecutive", 0))

    if overall_found:
        new_consecutive = prev_consecutive + 1
    else:
        new_consecutive = 0

    notify = False
    subject = ""
    body = ""
    now = datetime.utcnow().isoformat() + "Z"

    if overall_found and new_consecutive >= CONFIRMATIONS and not prev_found:
        notify = True
        subject = "Keychron — Nordic layout detected"
        body = f"Detected Nordic/Danish layout on {PRODUCT_URL}\n\nEvidence:\n" + "\n".join(evidence_items) + f"\n\nConfirmed {new_consecutive} consecutive times.\n\nTime: {now}"
        state["found"] = True
        state["consecutive"] = new_consecutive
        state["evidence"] = evidence_items
        state["updated_at"] = now
    elif not overall_found and prev_found:
        notify = True
        subject = "Keychron — Nordic layout no longer detected"
        body = f"Previously-detected Nordic layout is no longer present on {PRODUCT_URL}\n\nChecked pages and found no evidence.\n\nTime: {now}"
        state["found"] = False
        state["consecutive"] = 0
        state["evidence"] = evidence_items
        state["updated_at"] = now
    else:
        state["consecutive"] = new_consecutive
        state["evidence"] = evidence_items
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
