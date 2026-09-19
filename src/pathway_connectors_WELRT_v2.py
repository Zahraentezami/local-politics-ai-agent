import hashlib
import datetime as dt
import re
import time
import random
import json
import csv
import os
import io
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
from functools import lru_cache
from email.utils import parsedate_to_datetime
from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup


TOPIC_TAG = "WELRT"


TOPIC_KEYWORDS = [
    "waterfront east lrt", "waterfront east light rail", "welrt",
    "waterfront transit network", "east bayfront lrt",
    "waterfront east rapid transit",
]


EXCEL_PATH = "Torontos-Waterfront-East-LRT.xlsx"
EXCEL_SHEET = "Numerical Codes"


CODEBOOK_COLUMNS = [
    "V01_Date", "V02_Publish_Time", "V03_Capture_Time", "V04_Title", "V05_Language",
    "V06_PolicyStage", "V07_EventType", "V08_ContentType", "V09_VerifiedClaim",
    "V10_SourceClass", "V11_BodyClass", "V12_TopicClass", "V13_SourceLink",
    "V14_RequestMethod", "V15_Exceptions", "V16_Tier",
    "raw_text",  # FULL TEXT RETRIEVAL: export complete extracted source text
]


ITEM_CODE_PATTERN = re.compile(r'\b([A-Za-z]{2,3})\s*(\d+)\.(\d+)\b')
COMMITTEE_PREFIXES = {"EX": "Executive Committee", "TE": "Toronto and East York Community Council",
                      "PH": "Planning and Housing Committee", "CC": "City Council"}


OUTBOUND_LINK_ONLY_DOMAINS = {
    "cbc.ca", "www.cbc.ca", "ctvnews.ca", "www.ctvnews.ca",
    "citynews.ca", "www.citynews.ca", "thestar.com", "www.thestar.com",
    "globalnews.ca", "www.globalnews.ca",
    "theglobeandmail.com", "www.theglobeandmail.com",
    "villagemedia.ca", "www.villagemedia.ca",
    "thelocal.to", "www.thelocal.to", "thegreenline.to", "www.thegreenline.to",
}


ONTARIO_GOV_DOMAINS = {"ontario.ca", "www.ontario.ca", "metrolinx.com", "www.metrolinx.com"}
FEDERAL_GOV_DOMAINS = {"canada.ca", "www.canada.ca", "infrastructure.gc.ca", "www.infrastructure.gc.ca"}
CAMH_DOMAINS = {"camh.ca", "www.camh.ca"}
SCHOOL_BOARD_DOMAINS = {"tdsb.on.ca", "www.tdsb.on.ca", "tcdsb.org", "www.tcdsb.org"}
BOARD_OF_HEALTH_DOMAINS = {"boardofhealth.toronto.ca", "www.boardofhealth.toronto.ca"}
FORUM_DOMAINS = {"reddit.com", "www.reddit.com", "old.reddit.com"}


DOMAIN_BODY_CLASS = {
    "secure.toronto.ca": 2, "toronto.ca": 2, "www.toronto.ca": 2,
    "ttc.ca": 1, "www.ttc.ca": 1,
    "waterfrontoronto.ca": 3, "www.waterfrontoronto.ca": 3,
    "renewcanada.net": 7, "www.renewcanada.net": 7,
    "stevemunro.ca": 11,
    **{d: 7 for d in OUTBOUND_LINK_ONLY_DOMAINS},
    **{d: 5 for d in ONTARIO_GOV_DOMAINS},
    **{d: 6 for d in FEDERAL_GOV_DOMAINS},
    **{d: 4 for d in CAMH_DOMAINS},
    **{d: 9 for d in SCHOOL_BOARD_DOMAINS},
    **{d: 10 for d in BOARD_OF_HEALTH_DOMAINS},
    **{d: 8 for d in FORUM_DOMAINS},
}


FORUM_TEXT_SIGNALS = ["comment thread", "public forum", "discussion board", "message board",
                      "submitted by a resident", "user-submitted", "reply from user"]
PERFORMANCE_TERMS = ["performance report", "key performance indicator", " kpi ", "kpis",
                     "ceo update", "corporate performance", "performance dashboard"]


HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://secure.toronto.ca/council/",
}
session = cf_requests.Session(impersonate="chrome", headers=HEADERS)


EXCEL_CODE_COLUMNS = ["V06_PolicyStage", "V07_EventType", "V08_ContentType",
                      "V10_SourceClass", "V11_BodyClass", "V12_TopicClass"]

EXCEL_CODES_BY_URL: dict[str, dict] = {}


def normalize_url_key(u: str) -> str:
    u = str(u).strip()
    if "chrome-extension://" in u:
        u = "https://" + u.split("https://", 1)[-1]
    return u.rstrip("?")


def load_excel_codes(xlsx_path: str, sheet_name: str = EXCEL_SHEET) -> dict:
    if not os.path.exists(xlsx_path):
        return {}
    try:
        import pandas as pd
        df = pd.read_excel(xlsx_path, sheet_name=sheet_name)
        if "V13_SourceLink" not in df.columns:
            return {}
        codes_by_url = {}
        for _, row in df.iterrows():
            link = row.get("V13_SourceLink")
            if pd.isna(link) or not str(link).strip():
                continue
            key = normalize_url_key(link)
            entry = {}
            for col in EXCEL_CODE_COLUMNS:
                if col in df.columns and not pd.isna(row.get(col)):
                    try:
                        entry[col] = int(row.get(col))
                    except (ValueError, TypeError):
                        pass
            if entry:
                codes_by_url[key] = entry
        print(f"Loaded pre-assigned codes for {len(codes_by_url)} URL(s) from {xlsx_path}")
        return codes_by_url
    except Exception as e:
        print(f"[EXCEL SKIP] code lookup load failed: {type(e).__name__}: {e}")
        return {}


def get_excel_codes_for_url(url: str) -> dict:
    return EXCEL_CODES_BY_URL.get(normalize_url_key(url), {})



@lru_cache(maxsize=32)
def get_robots_status_and_rules(domain: str):
    try:
        resp = session.get(f"https://{domain}/robots.txt", timeout=15)
        if resp.status_code == 200:
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            return 200, rp
        return resp.status_code, None
    except Exception:
        return None, None



def is_allowed_by_robots(url: str, user_agent: str = "*") -> bool:
    domain = urlparse(url).netloc
    status, rp = get_robots_status_and_rules(domain)
    if status is None:
        return True
    if 400 <= status < 500:
        return True
    if status >= 500:
        return False
    if rp is not None:
        return rp.can_fetch(user_agent, url)
    return True



def canonicalize_item_code(text: str) -> Optional[str]:
    match = ITEM_CODE_PATTERN.search(text)
    if not match:
        return None
    letters, num1, num2 = match.groups()
    return f"{letters.upper()}{num1}.{num2}"



def canonicalize_all_item_codes(text: str) -> str:
    if not text:
        return text


    def replace_match(m):
        letters, num1, num2 = m.groups()
        return f"{letters.upper()}{num1}.{num2}"


    return ITEM_CODE_PATTERN.sub(replace_match, text)



def get_committee_from_item_code(url_or_text: str) -> Optional[str]:
    code = canonicalize_item_code(url_or_text)
    if not code:
        return None
    prefix = re.match(r'([A-Z]{2,3})', code)
    return COMMITTEE_PREFIXES.get(prefix.group(1), prefix.group(1)) if prefix else None



OFFICIAL_TITLES = [
    "councillor", "mayor", "deputy mayor", "executive director", "chief planner",
    "city manager", "deputy city manager", "director", "manager", "chair",
    "chief executive officer", "ceo", "commissioner", "clerk",
    "chief technology officer", "general manager", "president",
    "vice-chair", "vice chair", "spokesperson", "co-chair", "chief",
]


COMMON_NON_NAME_WORDS = {
    "resident", "councillor", "mayor", "chair", "director", "manager", "committee",
    "council", "city", "the", "public", "queen", "king", "york", "east", "west",
    "north", "south", "toronto", "waterfront", "transit", "commission", "executive",
    "deputy", "chief", "board", "government", "federal", "provincial", "ontario",
    "canada", "street", "avenue", "road", "drive", "report", "item", "phase",
    "lrt", "union", "station", "quay", "villiers", "island", "cherry",
}


DOMAIN_VOCABULARY_STOPWORDS = {
    "network", "plan", "infrastructure", "fund", "committee", "assessment",
    "program", "book", "environmental", "transit", "waterfront", "light",
    "rail", "expansion", "reset", "vision", "phase", "report", "action",
    "development", "official", "capital", "budget", "process", "review",
    "council", "corporation", "revitalization", "authority", "division",
    "department", "commission", "meeting", "session", "minutes", "board",
    "directors", "chair", "attachment", "summary", "background", "origin",
    "recommendation", "recommendations", "decision", "item", "status",
    "tracking", "communications", "speakers", "motions", "reports",
    "extension", "connection", "corridor", "segment", "loop", "island",
    "precinct", "lands", "quay", "shore", "boulevard", "street", "station",
    "hall", "building", "city", "toronto", "ontario", "canada", "federal",
    "provincial", "municipal", "government", "agency", "project", "line",
    "route", "system", "service", "study", "strategy", "framework",
    "policy", "initiative", "update", "progress", "delivery", "construction",
    "design", "engineering", "procurement", "planning", "implementation",
    "management", "coordination", "partnership", "agreement", "contract",
    "funding", "financing", "investment", "cost", "estimate", "reserve",
    "revenue", "expenditure", "allocation", "priority", "target", "outcome",
    "impact", "benefit", "risk", "issue", "concern", "opportunity", "option",
    "alternative", "scenario", "model", "analysis", "evaluation",
    "monitoring", "reporting", "accountability", "governance", "oversight",
    "compliance", "regulation", "legislation", "bylaw", "standard",
    "guideline", "protocol", "procedure", "workflow", "timeline",
    "schedule", "milestone", "deliverable", "output", "input",
    "resource", "capacity", "staff", "personnel", "team", "unit", "office",
    "bureau", "branch", "sector", "industry", "market", "economy", "growth",
    "population", "community", "neighbourhood", "neighborhood", "resident",
    "public", "private", "stakeholder", "partner", "vendor", "supplier",
    "contractor", "consultant", "advisor", "expert", "specialist",
    "staging", "area", "realm", "via", "hybrid", "person",
    "teams", "teleconference", "open", "session", "attendance", "regrets",
}
ALL_NON_NAME_WORDS = COMMON_NON_NAME_WORDS | DOMAIN_VOCABULARY_STOPWORDS


EMAIL_PATTERN = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b')
PHONE_PATTERN = re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
ADDRESS_PATTERN = re.compile(
    r'\b\d{1,5}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?\s+'
    r'(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr)\b', re.IGNORECASE)
CAP_WORD_PATTERN = re.compile(r'\b[A-Z][a-z]+\b')



def has_official_title_nearby(text: str, name: str, window: int = 60) -> bool:
    idx = text.find(name)
    if idx == -1:
        return False
    context = text[max(0, idx - window):min(len(text), idx + len(name) + window)].lower()
    return any(title in context for title in OFFICIAL_TITLES)



def find_candidate_names(text: str) -> list[tuple[int, int, str]]:
    words = list(CAP_WORD_PATTERN.finditer(text))
    raw_candidates = []
    for i in range(len(words) - 1):
        w1, w2 = words[i], words[i + 1]
        if w2.start() - w1.end() != 1:
            continue
        word1, word2 = w1.group(0), w2.group(0)
        if word1.lower() in ALL_NON_NAME_WORDS or word2.lower() in ALL_NON_NAME_WORDS:
            continue
        raw_candidates.append((w1.start(), w2.end(), f"{word1} {word2}", i))


    if not raw_candidates:
        return []


    final_spans = []
    i = 0
    while i < len(raw_candidates):
        chain = [raw_candidates[i]]
        j = i + 1
        while j < len(raw_candidates) and raw_candidates[j][3] == raw_candidates[j - 1][3] + 1:
            chain.append(raw_candidates[j])
            j += 1


        if len(chain) >= 2:
            for k in range(0, len(chain), 2):
                s, e, nm, _ = chain[k]
                final_spans.append((s, e, nm))
        else:
            s, e, nm, _ = chain[0]
            final_spans.append((s, e, nm))


        i = j if len(chain) >= 2 else i + 1


    return final_spans



def minimize_pii(text: str) -> tuple[str, int]:
    if not text:
        return text, 0
    redaction_count = 0
    text, n = EMAIL_PATTERN.subn("[EMAIL_REDACTED]", text); redaction_count += n
    text, n = PHONE_PATTERN.subn("[PHONE_REDACTED]", text); redaction_count += n
    text, n = ADDRESS_PATTERN.subn("[ADDRESS_REDACTED]", text); redaction_count += n


    candidates = find_candidate_names(text)
    to_redact = [(s, e) for s, e, name in candidates if not has_official_title_nearby(text, name)]
    merged = []
    for s, e in sorted(to_redact):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    for start, end in sorted(merged, key=lambda x: -x[0]):
        text = text[:start] + "[PRIVATE_NAME_REDACTED]" + text[end:]
        redaction_count += 1
    return text, redaction_count



def classify_body_class(url: str, text_sample: str = "") -> int:
    domain = urlparse(url).netloc.lower()
    if domain in DOMAIN_BODY_CLASS:
        return DOMAIN_BODY_CLASS[domain]
    lowered = text_sample.lower()
    if any(t in lowered for t in FORUM_TEXT_SIGNALS):
        return 8
    if "board of health" in lowered:
        return 10
    if any(t in lowered for t in ["toronto district school board", "toronto catholic district school board"]):
        return 9
    if "camh" in lowered or "centre for addiction and mental health" in lowered:
        return 4
    if "government of ontario" in lowered or "ontario government" in lowered:
        return 5
    if "government of canada" in lowered or "federal government" in lowered:
        return 6
    return 11



def is_outbound_link_only(url: str) -> bool:
    return urlparse(url).netloc.lower() in OUTBOUND_LINK_ONLY_DOMAINS



def classify_kind(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    if is_outbound_link_only(url):
        return "link_only"
    if "agenda-item.do?item=" in url:
        return "agenda_item"
    if url.lower().split("?")[0].endswith(".pdf"):
        return "pdf_report"
    if domain == "stevemunro.ca":
        return "commentary"
    return "webpage"



def classify_source_class(url: str, kind: str, text_sample: str = "") -> int:
    lowered = text_sample.lower()
    if kind == "agenda_item":
        return 3
    if kind == "pdf_report":
        if "minutes" in lowered or "transcript" in lowered:
            return 1
        if "by-law" in lowered or "legal opinion" in lowered:
            return 5
        return 2
    if kind == "commentary":
        return 7
    if any(t in lowered for t in FORUM_TEXT_SIGNALS):
        return 8
    if kind == "link_only":
        return 9
    if "announce" in lowered or "press release" in lowered:
        return 4
    if any(t in lowered for t in ["unofficial", "project update", "industry news", "trade press"]):
        return 6
    if lowered.strip():
        return 2
    return 9



def classify_event_type(url: str, kind: str, text_sample: str) -> int:
    lowered = text_sample.lower()
    if kind == "agenda_item":
        return 1
    if kind == "commentary":
        return 6
    if any(t in lowered for t in PERFORMANCE_TERMS):
        return 8
    if any(t in lowered for t in FORUM_TEXT_SIGNALS):
        return 7
    if kind == "link_only":
        return 5
    if kind == "pdf_report":
        if any(t in lowered for t in ["tri-government", "partnership with the city",
                                       "provincial", "federal government"]):
            return 3
        return 2
    if any(t in lowered for t in ["announce", "green light", "funding agreement"]):
        return 4
    if lowered.strip():
        return 2
    return 9



FORMULATION_TERMS = ["review", "initiate", "explore", "study", "phase 1",
                     "vision", "reset", "draft", "propose", "negotiate", "consultation"]
IMPLEMENTATION_TERMS = ["approve", "adopt", "allocate", "authorize", "advance the design",
                        "advancing the design", "fund", "endorse the overall", "commit",
                        "construction", "deliver", "budget"]
EVALUATION_TERMS = ["progress report", "audit", "monitor", "assess results",
                    "lessons learned", "impact evaluation", "performance"]



def classify_policy_stage(text: str) -> Optional[int]:
    if not text or not text.strip():
        return None
    lowered = text.lower()
    if any(t in lowered for t in EVALUATION_TERMS):
        return 3
    if any(t in lowered for t in IMPLEMENTATION_TERMS):
        return 2
    if any(t in lowered for t in FORMULATION_TERMS):
        return 1
    return None



TOPIC_CLASS_KEYWORDS = {
    2: ["homelessness", "homeless shelter", "encampment", "unhoused"],
    3: ["affordable housing", "housing supply", "rent control", "housing development"],
    4: ["public health", "board of health", "camh", "mental health", "addiction services"],
    5: ["climate change", "climate action", "greenhouse gas", "emissions reduction", "net zero"],
}



def classify_topic_class(text: str) -> int:
    lowered = text.lower() if text else ""
    if any(kw in lowered for kw in TOPIC_KEYWORDS):
        return 1
    for code, keywords in TOPIC_CLASS_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return code
    if any(t in lowered for t in ["transit", "lrt", "light rail", "subway", "streetcar", "ttc"]):
        return 1
    return 6



CONTENT_TYPE_HEADER_MAP = {
    "text/html": 1, "application/xhtml+xml": 1,
    "application/pdf": 2,
    "text/csv": 3,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 3,
    "application/vnd.ms-excel": 3,
}



def classify_content_type(url: str, content_type_header: Optional[str] = None) -> int:
    if content_type_header:
        header_main = content_type_header.split(";")[0].strip().lower()
        if header_main in CONTENT_TYPE_HEADER_MAP:
            return CONTENT_TYPE_HEADER_MAP[header_main]
        if header_main:
            return 4
    path = url.lower().split("?")[0]
    if path.endswith(".pdf"):
        return 2
    if path.endswith(".csv") or path.endswith(".xlsx") or path.endswith(".xls"):
        return 3
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment or path.endswith((".html", ".htm")):
        return 1
    return 4



def extract_date_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    month_match = re.search(r'([A-Z][a-z]+)\s+(\d{1,2})', text)
    year_match = re.search(r'(\d{4})', text)
    if not (month_match and year_match):
        return None
    try:
        return dt.datetime.strptime(f"{month_match.group(1)} {month_match.group(2)} {year_match.group(1)}",
                                    "%B %d %Y").strftime("%d/%m/%Y")
    except ValueError:
        return None



def find_page_date(soup: BeautifulSoup) -> Optional[str]:
    meta_candidates = [
        ("meta", {"property": "article:published_time"}), ("meta", {"property": "article:modified_time"}),
        ("meta", {"name": "date"}), ("meta", {"name": "publish-date"}),
        ("meta", {"name": "publication_date"}), ("meta", {"property": "og:updated_time"}),
        ("meta", {"itemprop": "datePublished"}), ("meta", {"itemprop": "dateModified"}),
    ]
    for tag_name, attrs in meta_candidates:
        tag = soup.find(tag_name, attrs)
        if tag and tag.get("content"):
            return tag.get("content").strip()
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        return time_tag["datetime"].strip()
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                data = data[0] if data else {}
            for key in ("datePublished", "dateModified", "uploadDate"):
                if key in data:
                    return data[key]
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return None



def excel_serial_to_date(value) -> Optional[str]:
    try:
        serial = float(value)
    except (ValueError, TypeError):
        return None
    if not (18000 <= serial <= 73000):
        return None
    try:
        epoch = dt.datetime(1899, 12, 30)
        result_date = epoch + dt.timedelta(days=serial)
        return result_date.strftime("%d/%m/%Y")
    except (OverflowError, ValueError):
        return None



def normalize_date_time(raw_value) -> tuple[Optional[str], Optional[str]]:
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return None, None


    serial_result = excel_serial_to_date(raw_value)
    if serial_result:
        return serial_result, None


    raw_value = str(raw_value).strip()
    try:
        d = dt.datetime.strptime(raw_value, "%Y-%m-%d")
        return d.strftime("%d/%m/%Y"), None
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(raw_value)
        return d.strftime("%d/%m/%Y"), d.strftime("%H:%M:%S %Z")
    except (ValueError, TypeError):
        pass
    try:
        d = dt.datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        return d.strftime("%d/%m/%Y"), d.strftime(f"%H:%M:%S {d.strftime('%z')}")
    except ValueError:
        pass
    return raw_value, None



@dataclass
class Record:
    V01_Date: Optional[str] = None
    V02_Publish_Time: Optional[str] = None
    V03_Capture_Time: str = field(default_factory=lambda: dt.datetime.utcnow().isoformat())
    V04_Title: str = ""
    V05_Language: int = 1
    V06_PolicyStage: Optional[int] = None
    V07_EventType: Optional[int] = None
    V08_ContentType: Optional[int] = None
    V09_VerifiedClaim: str = ""
    V10_SourceClass: Optional[int] = None
    V11_BodyClass: Optional[int] = None
    V12_TopicClass: int = 1
    V13_SourceLink: str = ""
    V14_RequestMethod: int = 1
    V15_Exceptions: str = ""
    V16_Tier: str = "P0"
    content_hash: str = field(init=False, default="")
    vote_record: Optional[dict] = None
    video_links: Optional[list] = None
    committee: Optional[str] = None

    # FULL TEXT RETRIEVAL: preserves the complete extracted document/page text
    raw_text: str = ""


    def finalize(self, apply_pii_check: bool = True):
        date_only, time_only = normalize_date_time(self.V01_Date)
        self.V01_Date = date_only
        if time_only:
            self.V02_Publish_Time = time_only


        self.V04_Title = canonicalize_all_item_codes(self.V04_Title)
        self.V09_VerifiedClaim = canonicalize_all_item_codes(self.V09_VerifiedClaim)
        self.V15_Exceptions = canonicalize_all_item_codes(self.V15_Exceptions)


        if apply_pii_check and self.V09_VerifiedClaim:
            redacted_text, redaction_count = minimize_pii(self.V09_VerifiedClaim)
            self.V09_VerifiedClaim = redacted_text
            if redaction_count:
                self.V15_Exceptions = (self.V15_Exceptions + f" | PII_REDACTED: {redaction_count} instance(s)").strip(" |")


        basis = (self.V04_Title + self.V09_VerifiedClaim + self.V13_SourceLink).encode("utf-8")
        self.content_hash = hashlib.sha256(basis).hexdigest()
        return self



def apply_excel_codes_override(rec: Record, url: str) -> Record:
    overrides = get_excel_codes_for_url(url)
    if not overrides:
        return rec
    applied = []
    for col, value in overrides.items():
        setattr(rec, col, value)
        applied.append(col)
    if applied:
        rec.V15_Exceptions = (rec.V15_Exceptions + f" | CODES_FROM_EXCEL: {','.join(applied)}").strip(" |")
    return rec



def make_failure_record(url: str, kind: str, error: Exception) -> dict:
    rec = Record(
        V04_Title=f"[FETCH FAILED] {url}", V09_VerifiedClaim="", V10_SourceClass=9, V11_BodyClass=11,
        V13_SourceLink=url, V14_RequestMethod=1,
        V15_Exceptions=f"FETCH_FAILED ({kind}): {type(error).__name__}: {str(error)[:200]}",
        V16_Tier="P0-failed",
    )
    rec = apply_excel_codes_override(rec, url)
    return asdict(rec.finalize(apply_pii_check=False))



def make_robots_blocked_record(url: str, kind: str) -> dict:
    rec = Record(
        V04_Title=f"[ROBOTS DISALLOWED] {url}", V09_VerifiedClaim="", V10_SourceClass=9, V11_BodyClass=11,
        V13_SourceLink=url, V14_RequestMethod=1,
        V15_Exceptions=f"ROBOTS_DISALLOWED ({kind}): fetch skipped per robots.txt policy",
        V16_Tier="P0-robots-blocked",
    )
    rec = apply_excel_codes_override(rec, url)
    return asdict(rec.finalize(apply_pii_check=False))



def clean_vote_name(name: str) -> bool:
    name = name.strip()
    if not name:
        return False
    if any(ch.isdigit() for ch in name):
        return False
    if "total members" in name.lower():
        return False
    if len(name.split()) < 2 and "(" not in name:
        return False
    return True



def parse_vote_section(full_text: str) -> Optional[dict]:
    if "Members that voted" not in full_text and "Members that were absent" not in full_text:
        return None


    result_match = re.search(r'Result:\s*(\w+)', full_text)
    result = result_match.group(1) if result_match else None


    def extract_names(label_pattern: str) -> Optional[str]:
        m = re.search(label_pattern + r'\s*\n([^\n]+)', full_text)
        return m.group(1).strip() if m else None


    yes_raw = extract_names(r'Members that voted Yes are')
    no_raw = extract_names(r'Members that voted No are')
    absent_raw = extract_names(r'Members that were absent are')


    def split_names(raw: Optional[str]) -> list:
        if not raw:
            return []
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return [n for n in names if clean_vote_name(n)]


    yes_voters = split_names(yes_raw)
    no_voters = split_names(no_raw)
    absent = split_names(absent_raw)


    if not (yes_voters or no_voters or absent):
        return None


    votes = {name: "Yes" for name in yes_voters}
    votes.update({name: "No" for name in no_voters})
    votes.update({name: "Absent" for name in absent})


    return {
        "result": result,
        "councillor_votes": votes,
        "summary": {"Yes": len(yes_voters), "No": len(no_voters), "Absent": len(absent)},
    }



def find_video_links(soup: BeautifulSoup) -> list:
    found = []
    for a in soup.find_all("a", href=True):
        href, text = a["href"], a.get_text(strip=True).lower()
        if any(t in text for t in ["webcast", "video", "watch"]) or \
           any(t in href.lower() for t in ["webcast", "youtube", "video"]):
            found.append(href)
    return list(dict.fromkeys(found))



def find_consultation_links(soup: BeautifulSoup, base_url: str) -> list:
    found = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if any(t in text for t in ["consultation", "open house", "public meeting", "engagement", "feedback"]):
            found.append(urljoin(base_url, a["href"]))
    return list(dict.fromkeys(found))



def fetch_agenda_item(url: str) -> Record:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text("\n", strip=True)


    tracking_match = re.search(r'(adopted|considered|withdrawn) this item on ([^.]+)\.', full_text, re.IGNORECASE)
    tracking_sentence = tracking_match.group(0) if tracking_match else ""
    date_val = extract_date_from_text(tracking_sentence)


    title = None
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = tag.get_text(strip=True)
        if ITEM_CODE_PATTERN.search(text) and " - " in text:
            code = canonicalize_item_code(text)
            rest = text.split(" - ", 1)[-1] if " - " in text else text
            title = f"{code} - {rest}" if code else text
            break
    title = title or url


    decision_text = ""
    decision_heading = soup.find(lambda tag: tag.name in ("h2", "h3", "h4")
                                  and "decision" in tag.get_text(strip=True).lower()
                                  and "type" not in tag.get_text(strip=True).lower())
    if decision_heading:
        collected = []
        for sib in decision_heading.find_next_siblings():
            if sib.name in ("h1", "h2", "h3", "h4"):
                break
            text = sib.get_text(" ", strip=True)
            if text:
                collected.append(text)
        decision_text = " ".join(collected)
    decision_text = decision_text or tracking_sentence


    vote_record = parse_vote_section(full_text)
    video_links = find_video_links(soup)
    consultation_links = find_consultation_links(soup, url)
    committee = get_committee_from_item_code(url)


    claim_text = decision_text[:500]
    exceptions_parts = [tracking_sentence[:150]]
    if consultation_links:
        exceptions_parts.append(f"consultation_links={len(consultation_links)}")
    if vote_record:
        exceptions_parts.append(f"vote_result={vote_record['result']} "
                                f"({vote_record['summary']['Yes']}Y/{vote_record['summary']['No']}N/"
                                f"{vote_record['summary']['Absent']}A)")


    rec = Record(
        V01_Date=date_val, V04_Title=title,
        V06_PolicyStage=classify_policy_stage(claim_text),
        V07_EventType=classify_event_type(url, "agenda_item", claim_text),
        V08_ContentType=classify_content_type(url, resp.headers.get("Content-Type")),
        V09_VerifiedClaim=claim_text,
        V10_SourceClass=classify_source_class(url, "agenda_item", claim_text),
        V11_BodyClass=classify_body_class(url, claim_text),
        V12_TopicClass=classify_topic_class(claim_text),
        V13_SourceLink=url, V14_RequestMethod=1,
        V15_Exceptions=" | ".join(p for p in exceptions_parts if p),
        V16_Tier="P0",
    )
    # FULL TEXT RETRIEVAL: preserve complete agenda-item text
    rec.raw_text = full_text

    rec.vote_record, rec.video_links, rec.committee = vote_record, video_links or None, committee
    rec = apply_excel_codes_override(rec, url)
    return rec.finalize()



def discover_upcoming_agendas() -> list[str]:
    calendar_url = "https://secure.toronto.ca/council/"
    found = []
    try:
        resp = session.get(calendar_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "report.do?meeting=" in href and "type=agenda" in href:
                found.append(urljoin(calendar_url, href))
        print(f"[DISCOVERY] upcoming agendas: {len(found)} meeting agenda page(s) found "
              f"(KNOWN LIMITATION: this page is JS-rendered, expect 0)")
    except Exception as e:
        print(f"[DISCOVERY SKIP] upcoming agendas unavailable: {type(e).__name__}: {e}")
    return found



def fetch_future_agenda(url: str) -> Optional[Record]:
    if not is_allowed_by_robots(url):
        return None
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        full_text = soup.get_text(" ", strip=True)
        if not any(kw in full_text.lower() for kw in TOPIC_KEYWORDS):
            return None
        title = soup.title.get_text(strip=True) if soup.title else url
        pub_date = find_page_date(soup)
        rec = Record(
            V01_Date=pub_date, V04_Title=f"[UPCOMING AGENDA] {title}",
            V06_PolicyStage=1, V07_EventType=1,
            V08_ContentType=classify_content_type(url, resp.headers.get("Content-Type")),
            V09_VerifiedClaim=f"Future meeting agenda contains WELRT-relevant item(s): {title}",
            V10_SourceClass=3, V11_BodyClass=classify_body_class(url, full_text[:2000]),
            V12_TopicClass=classify_topic_class(full_text[:2000]),
            V13_SourceLink=url, V14_RequestMethod=1,
            V15_Exceptions="future/upcoming agenda - not yet decided, monitor for outcome",
            V16_Tier="P0",
        )

        # FULL TEXT RETRIEVAL: preserve complete upcoming-agenda text
        rec.raw_text = full_text

        rec = apply_excel_codes_override(rec, url)
        return rec.finalize()
    except Exception as e:
        print(f"[SKIP] future agenda {url}: {type(e).__name__}: {e}")
        return None



def get_pdf_last_modified(url: str) -> Optional[str]:
    resp = session.head(url, timeout=15)
    return resp.headers.get("Last-Modified")



def extract_pdf_text_with_ocr(content: bytes) -> tuple[str, float, int]:
    import fitz
    doc = fitz.open(stream=content, filetype="pdf")


    text_chunks = []
    ocr_page_count = 0


    for page in doc:
        native_text = page.get_text().strip()
        if len(native_text) >= 20:
            text_chunks.append(native_text)
            continue


        try:
            import pytesseract
            from PIL import Image


            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_text = pytesseract.image_to_string(img).strip()
            text_chunks.append(ocr_text)
            if ocr_text:
                ocr_page_count += 1
        except ImportError:
            text_chunks.append(native_text)
        except Exception as e:
            print(f"[OCR WARN] page failed: {type(e).__name__}: {e}")
            text_chunks.append(native_text)


    full_text = "\n".join(text_chunks)
    non_empty_pages = sum(1 for t in text_chunks if t.strip())
    confidence = non_empty_pages / max(len(text_chunks), 1)
    return full_text, confidence, ocr_page_count



def fetch_staff_report(url: str) -> Record:
    last_modified = get_pdf_last_modified(url)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    text, confidence, ocr_page_count = extract_pdf_text_with_ocr(resp.content)


    exceptions_parts = []
    if ocr_page_count > 0:
        exceptions_parts.append(f"OCR_APPLIED: {ocr_page_count} page(s) required OCR fallback")
    if confidence < 0.5:
        exceptions_parts.append("LOW_CONFIDENCE_EXTRACTION: flagged for human review")


    date_val = last_modified
    date_in_doc = re.search(r'Date:\s*([A-Z][a-z]+ \d{1,2},? \d{4})', text)
    if date_in_doc:
        parsed = extract_date_from_text(date_in_doc.group(1))
        if parsed:
            date_val = parsed
            exceptions_parts.append("date_source=in-document 'Date:' line")
    if not any("date_source" in p for p in exceptions_parts):
        exceptions_parts.append("date_source=Last-Modified header (no in-doc date)")


    is_consultation = any(t in text.lower()[:3000] for t in
                          ["public consultation", "open house", "public meeting", "engagement summary"])
    if is_consultation:
        exceptions_parts.append("PUBLIC_CONSULTATION_MATERIAL")


    claim_text = text[:500]
    rec = Record(
        V01_Date=date_val, V04_Title=url.rsplit("/", 1)[-1],
        V06_PolicyStage=classify_policy_stage(text[:2000]),
        V07_EventType=classify_event_type(url, "pdf_report", text[:2000]),
        V08_ContentType=classify_content_type(url, resp.headers.get("Content-Type")),
        V09_VerifiedClaim=claim_text,
        V10_SourceClass=classify_source_class(url, "pdf_report", text[:2000]),
        V11_BodyClass=classify_body_class(url, text[:2000]),
        V12_TopicClass=classify_topic_class(text[:2000]),
        V13_SourceLink=url, V14_RequestMethod=1,
        V15_Exceptions=" | ".join(exceptions_parts),
        V16_Tier="P0",
    )

    # FULL TEXT RETRIEVAL: preserve complete PDF/staff-report text
    rec.raw_text = text

    rec = apply_excel_codes_override(rec, url)
    return rec.finalize()



def fetch_link_only(url: str) -> Record:
    title, pub_date = url, None
    content_type_header = None
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        content_type_header = resp.headers.get("Content-Type")
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else url
        pub_date = find_page_date(soup)
    except Exception as e:
        print(f"[LINK-ONLY WARN] {url}: {type(e).__name__}: {e}")
    rec = Record(
        V01_Date=pub_date, V04_Title=title, V06_PolicyStage=None, V07_EventType=5,
        V08_ContentType=classify_content_type(url, content_type_header),
        V09_VerifiedClaim="", V10_SourceClass=9,
        V11_BodyClass=classify_body_class(url), V13_SourceLink=url, V14_RequestMethod=1,
        V15_Exceptions="OUTBOUND_LINK_ONLY - per scope, content not ingested",
        V16_Tier="outbound-link-only",
    )
    rec = apply_excel_codes_override(rec, url)
    return rec.finalize(apply_pii_check=False)



def fetch_generic_page(url: str) -> Record:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    soup_full = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["nav", "footer", "script", "style", "header"]):
        tag.decompose()


    title = soup.title.get_text(strip=True) if soup.title else url
    body_text = soup.get_text(" ", strip=True)
    pub_date = find_page_date(soup_full)
    exceptions = ""
    if not pub_date:
        try:
            head_resp = session.head(url, timeout=15)
            pub_date = head_resp.headers.get("Last-Modified")
            if pub_date:
                exceptions = "date_source=Last-Modified header"
        except Exception:
            pass
    exceptions = exceptions or "no date found via meta/JSON-LD/time-tag/HTTP-header"


    kind = classify_kind(url)
    claim_text = body_text[:500]
    rec = Record(
        V01_Date=pub_date, V04_Title=title,
        V06_PolicyStage=classify_policy_stage(body_text[:2000]),
        V07_EventType=classify_event_type(url, kind, body_text[:2000]),
        V08_ContentType=classify_content_type(url, resp.headers.get("Content-Type")),
        V09_VerifiedClaim=claim_text,
        V10_SourceClass=classify_source_class(url, kind, body_text[:2000]),
        V11_BodyClass=classify_body_class(url, body_text[:2000]),
        V12_TopicClass=classify_topic_class(body_text[:2000]),
        V13_SourceLink=url, V14_RequestMethod=1, V15_Exceptions=exceptions,
        V16_Tier="P0/P1-agency",
    )

    # FULL TEXT RETRIEVAL: preserve complete cleaned webpage body text
    rec.raw_text = body_text

    rec = apply_excel_codes_override(rec, url)
    return rec.finalize()



FETCHERS = {"agenda_item": fetch_agenda_item, "pdf_report": fetch_staff_report,
            "link_only": fetch_link_only, "commentary": fetch_generic_page, "webpage": fetch_generic_page}



def route_url(url: str) -> str:
    return classify_kind(url)



def load_seed_urls_from_excel_optional(xlsx_path: str, sheet_name: str = EXCEL_SHEET) -> list[str]:
    if not os.path.exists(xlsx_path):
        print(f"(No Excel file at {xlsx_path} - proceeding with live discovery only.)")
        return []
    try:
        import pandas as pd
        df = pd.read_excel(xlsx_path, sheet_name=sheet_name)
        urls = df["V13_SourceLink"].dropna().astype(str).tolist()
        cleaned = []
        for u in urls:
            u = u.strip()
            if "chrome-extension://" in u:
                u = "https://" + u.split("https://", 1)[-1]
            u = u.rstrip("?")
            if u.lower().startswith("http"):
                cleaned.append(u)
        print(f"Loaded {len(cleaned)} trusted seed URLs from {xlsx_path}")
        return cleaned
    except Exception as e:
        print(f"[EXCEL SKIP] optional seed load failed: {type(e).__name__}: {e}")
        return []



def deduplicate_records(records: list[dict]) -> list[dict]:
    seen_hashes: dict[str, dict] = {}
    for rec in records:
        h = rec.get("content_hash")
        if not h:
            continue
        if h not in seen_hashes:
            seen_hashes[h] = rec
            rec["V16_Tier"] = rec.get("V16_Tier", "P0") + "-canonical"
        else:
            canonical_url = seen_hashes[h].get("V13_SourceLink", "unknown")
            rec["V16_Tier"] = rec.get("V16_Tier", "P0") + "-duplicate"
            rec["V15_Exceptions"] = (rec.get("V15_Exceptions", "") +
                                     f" | DUPLICATE_OF: {canonical_url}").strip(" |")
    dup_count = sum(1 for r in records if r["V16_Tier"].endswith("-duplicate"))
    if dup_count:
        print(f"[DEDUPE] {dup_count} duplicate record(s) identified and tagged.")
    return records



def run_all_pathways(xlsx_path: str = EXCEL_PATH) -> list[dict]:
    global EXCEL_CODES_BY_URL
    EXCEL_CODES_BY_URL = load_excel_codes(xlsx_path)


    trusted_seeds = load_seed_urls_from_excel_optional(xlsx_path)
    upcoming_agenda_urls = discover_upcoming_agendas()


    records: list[dict] = []
    counts = {"agenda_item": 0, "pdf_report": 0, "link_only": 0,
              "commentary": 0, "webpage": 0, "future_agenda": 0, "failed": 0, "robots_blocked": 0}
    pii_redaction_total = 0
    vote_records_found = 0
    ocr_used_total = 0


    for url in trusted_seeds:
        kind = route_url(url)
        fetch_fn = FETCHERS.get(kind)
        if not fetch_fn:
            continue


        if not is_allowed_by_robots(url):
            print(f"[ROBOTS BLOCKED] ({kind}) {url}")
            records.append(make_robots_blocked_record(url, kind))
            counts["robots_blocked"] += 1
            time.sleep(random.uniform(1.5, 3.0))
            continue


        try:
            rec = fetch_fn(url)
            rec_dict = asdict(rec)
            records.append(rec_dict)
            counts[kind] += 1
            if "PII_REDACTED" in rec_dict.get("V15_Exceptions", ""):
                pii_redaction_total += 1
            if rec_dict.get("vote_record"):
                vote_records_found += 1
            if "OCR_APPLIED" in rec_dict.get("V15_Exceptions", ""):
                ocr_used_total += 1
        except Exception as e:
            print(f"[SKIP] ({kind}) {url}: {type(e).__name__}: {e}")
            records.append(make_failure_record(url, kind, e))
            counts["failed"] += 1
        time.sleep(random.uniform(1.5, 3.0))


    for url in upcoming_agenda_urls[:15]:
        rec = fetch_future_agenda(url)
        if rec:
            records.append(asdict(rec))
            counts["future_agenda"] += 1
        time.sleep(random.uniform(1.5, 3.0))


    records = deduplicate_records(records)


    for name, n in counts.items():
        print(f"[OK] {name}: {n} records")
    if counts["failed"]:
        print(f"NOTE: {counts['failed']} source(s) failed to fetch, logged as V16_Tier='P0-failed'.")
    if counts["robots_blocked"]:
        print(f"NOTE: {counts['robots_blocked']} source(s) blocked by robots.txt policy.")
    if pii_redaction_total:
        print(f"PII NOTE: {pii_redaction_total} record(s) had text redacted - SPOT-CHECK these.")
    if ocr_used_total:
        print(f"OCR NOTE: {ocr_used_total} PDF(s) required OCR fallback - SPOT-CHECK these for accuracy.")
    print(f"VOTE NOTE: {vote_records_found} record(s) contained a parsed councillor vote breakdown.")


    return records



if __name__ == "__main__":
    print(f"Working directory: {os.getcwd()}")
    print("SCOPE: Tier P0 (council/committee/agenda/votes/staff reports).")


    all_records = run_all_pathways()
    print(f"\nEmitted {len(all_records)} total Codebook-conformant records for topic={TOPIC_TAG}")


    json_path = os.path.join(os.getcwd(), "welrt_records.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, indent=2, ensure_ascii=False)
    print(f"Wrote {json_path}")


    if all_records:
        csv_path = os.path.join(os.getcwd(), "welrt_records.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CODEBOOK_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_records)
        print(f"Wrote {csv_path}")
    else:
        print("No records collected - CSV not written.")
