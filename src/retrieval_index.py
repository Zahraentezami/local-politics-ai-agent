"""
Chunking -> Embedding -> FAISS Indexing -> Source-aware Retrieval

- Structural-first chunking with conservative heading detection.
- Adds strong Toronto agenda boundaries including Committee Recommendations.
- Recognizes dated Background Information and Communications markers robustly.
- Removes non-substantive micro-chunks while preserving short civic evidence.
- Merges tiny chunks only inside the same semantic section.
- Labels unsectioned webpage text as "Document body".
- Removes exact duplicate chunks within the same document.
- Uses query-sensitive reranking for decision, stakeholder, and general
  information needs while preserving raw semantic similarity.
- Uses one-document-per-result diversity for decision/stakeholder queries
  and allows two chunks per document for general queries.
- Keeps all codebook/provenance metadata attached to every chunk.

Chunking remains separate from normalization.
"""

# ============================================================
# INSTALL DEPENDENCIES
# ============================================================

#
# !pip install -q sentence-transformers faiss-cpu openpyxl pandas numpy


# ============================================================
# IMPORTS
# ============================================================

import re
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from sentence_transformers import SentenceTransformer
import faiss


# ============================================================
# CONFIGURATION
# ============================================================

INPUT_FILE = ""
OUTPUT_DIR = ""

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

MAX_WORDS = 170
MIN_WORDS = 35
OVERLAP_WORDS = 30

DEFAULT_TOP_K = 5

# Prevent one long document from dominating the initial result set.
MAX_CHUNKS_PER_DOCUMENT = 2

# Retrieve more semantic candidates before reranking/filtering.
RERANK_CANDIDATE_MULTIPLIER = 15

PIPELINE_VERSION = "2.3.0"


# ============================================================
# OUTPUT FILES
# ============================================================

CHUNKS_JSONL = "chunks.jsonl"
CHUNKS_CSV = "chunks.csv"
EMBEDDINGS_NPY = "embeddings.npy"
FAISS_INDEX_FILE = "faiss.index"
INDEX_METADATA_FILE = "index_metadata.json"


# ============================================================
# BASIC UTILITIES
# ============================================================

def safe_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def stable_hash(text, length=16):
    value = safe_text(text)
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:length]


def normalize_for_id(text):
    text = safe_text(text).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def source_domain(url):
    try:
        return urlparse(
            safe_text(url)
        ).netloc.lower()
    except Exception:
        return ""


# ============================================================
# DOCUMENT ID
# ============================================================

def create_document_id(row):
    """
    Stable document ID based on source + text version + basic metadata.

    If the normalized text changes materially, the resulting document ID
    also changes. This makes versions distinguishable.
    """
    source_url = safe_text(
        row.get("V13_SourceLink", "")
    )

    text_hash = safe_text(
        row.get("normalized_text_hash", "")
    )

    title = safe_text(
        row.get("V04_Title", "")
    )

    date = (
        safe_text(
            row.get("normalized_date", "")
        )
        or safe_text(
            row.get("V01_Date", "")
        )
    )

    identity = "||".join([
        normalize_for_id(source_url),
        normalize_for_id(text_hash),
        normalize_for_id(title),
        normalize_for_id(date),
    ])

    return f"doc_{stable_hash(identity, 20)}"


# ============================================================
# STRUCTURAL HEADING DETECTION
# ============================================================

KNOWN_HEADINGS = {
    "tracking status",
    "city council decision",
    "committee decision",
    "decision history",
    "recommendations",
    "recommendation",
    "summary",
    "financial impact",
    "financial impacts",
    "background",
    "comments",
    "comment",
    "origin",
    "motions",
    "motion",
    "votes",
    "vote",
    "speakers",
    "speaker",
    "consultation",
    "consultation findings",
    "implementation",
    "implementation update",
    "project status",
    "next steps",
    "issue background",
    "decision",
    "attendance",
    "appendices",
    "directors",
    "key drivers",
    "operating budget",
    "committee recommendations",
    "background information",
    "communications",
    "background information (committee)",
    "background information (city council)",
    "communications (committee)",
    "communications (city council)",
    "motions (committee)",
    "motions (city council)",
    "speakers (committee)",
    "speakers (city council)",
    "public consultation",
}


def looks_like_heading(line):
    """
    Conservative heading detector.

    A heading must either be a known line-level heading or a short
    uppercase report heading containing at least one alphabetic character.
    Numeric table rows are never headings.
    """
    line = safe_text(line)

    if not line:
        return False

    lower = line.lower().rstrip(":")

    if lower in LINE_LEVEL_HEADINGS:
        return True

    if not re.search(r"[A-Za-z]", line):
        return False

    if len(line) > 120 or len(line.split()) > 14:
        return False

    if re.fullmatch(
        r"[A-Z0-9][A-Z0-9 /&(),:'’–—\-]+",
        line
    ):
        return True

    return False


def is_list_start(line):
    line = safe_text(line)

    return bool(
        re.match(
            r"^(?:\d+[\.\)]|[a-zA-Z][\.\)]|[-•])\s+",
            line
        )
    )



# ============================================================
# INLINE STRUCTURAL BREAKS
# ============================================================

# Only these strong/composite markers are safe to isolate when they
# occur inline. Generic words such as "Recommendations", "Summary",
# "Background", and "Consultation" are intentionally excluded because
# they can occur naturally in prose.
STRONG_INLINE_SECTION_MARKERS = [
    "Background Information (Committee)",
    "Background Information (City Council)",
    "Communications (Committee)",
    "Communications (City Council)",
    "Motions (Committee)",
    "Motions (City Council)",
    "Speakers (Committee)",
    "Speakers (City Council)",
    "Committee Recommendations",
    "City Council Decision",
    "Committee Decision",
]


# These can be recognized when they already occupy a line on their own.
LINE_LEVEL_HEADINGS = {
    "tracking status",
    "city council decision",
    "committee decision",
    "committee recommendations",
    "decision history",
    "recommendations",
    "recommendation",
    "summary",
    "financial impact",
    "financial impacts",
    "background",
    "background information",
    "background information (committee)",
    "background information (city council)",
    "communications",
    "communications (committee)",
    "communications (city council)",
    "comments",
    "comment",
    "origin",
    "motions",
    "motion",
    "motions (committee)",
    "motions (city council)",
    "votes",
    "vote",
    "speakers",
    "speaker",
    "speakers (committee)",
    "speakers (city council)",
    "consultation",
    "public consultation",
    "consultation findings",
    "implementation",
    "implementation update",
    "project status",
    "next steps",
    "issue background",
    "decision",
    "attendance",
    "appendices",
    "directors",
    "key drivers",
    "operating budget",
}

def insert_structural_breaks(text):
    """
    Isolate high-confidence municipal subsection markers.

    Handles:
      Background Information (Committee)
      Communications (City Council)
      Committee Recommendations
      Background Information (October 9, 2015)
      Communications (October 19, 2015)

    Generic prose words such as "recommendations" are not split globally.
    """
    text = safe_text(text)

    # Strong fixed labels.
    for marker in sorted(
        STRONG_INLINE_SECTION_MARKERS,
        key=len,
        reverse=True,
    ):
        escaped = re.escape(marker)

        text = re.sub(
            rf"(?<!\n)\s*(?={escaped}\b)",
            "\n",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            rf"{escaped}\s*",
            f"{marker}\n",
            text,
            flags=re.IGNORECASE,
        )

    # Dated labels. capture only the label and leave the date
    # with the following content.
    months = (
        r"January|February|March|April|May|June|July|"
        r"August|September|October|November|December"
    )

    dated_labels = [
        "Background Information",
        "Communications",
    ]

    for label in dated_labels:
        escaped = re.escape(label)

        pattern = (
            rf"(?<!\n)\s*(?={escaped}\s*\("
            rf"(?:{months})\s+\d{{1,2}},\s+\d{{4}}\))"
        )

        text = re.sub(
            pattern,
            "\n",
            text,
            flags=re.IGNORECASE,
        )

        pattern2 = (
            rf"{escaped}\s*(?=\("
            rf"(?:{months})\s+\d{{1,2}},\s+\d{{4}}\))"
        )

        text = re.sub(
            pattern2,
            f"{label}\n",
            text,
            flags=re.IGNORECASE,
        )

    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def split_leading_heading(line):
    """
    Split a known heading from following text ONLY when the line starts
    with the heading. This handles extraction such as:

        "Public Consultation Public outreach has..."
        "City Council Decision City Council adopted..."

    without splitting ordinary prose containing those words later.
    """
    original = safe_text(line)

    if not original:
        return None

    # Longer headings first to prevent shorter-prefix matches.
    headings = sorted(
        LINE_LEVEL_HEADINGS,
        key=len,
        reverse=True,
    )

    lower = original.lower()

    for heading in headings:
        if lower == heading:
            return (
                original,
                ""
            )

        prefix = heading + " "

        if lower.startswith(prefix):
            heading_text = original[:len(heading)]
            remainder = original[len(heading):].strip()

            # Avoid false splitting of ordinary prose such as
            # "Recommendations were subsequently adopted..."
            if heading in {
                "recommendations",
                "recommendation",
                "summary",
                "background",
                "consultation",
                "decision",
                "comments",
                "comment",
                "vote",
                "motion",
                "speaker",
            }:
                return None

            return (
                heading_text,
                remainder
            )

    return None


# ============================================================
# TEXT -> STRUCTURAL UNITS
# ============================================================

def structural_units(text):
    """
    Convert normalized text into structural units:
    - headings
    - paragraphs
    - list items

    Heading recognition is deliberately conservative.
    """
    text = safe_text(text)

    if not text:
        return []

    text = insert_structural_breaks(text)

    lines = [
        line.strip()
        for line in text.replace(
            "\r\n", "\n"
        ).split("\n")
    ]

    units = []
    buffer = []
    current_heading = ""

    def flush_buffer():
        nonlocal buffer

        if not buffer:
            return

        paragraph = " ".join(
            x for x in buffer if x
        ).strip()

        if paragraph:
            units.append({
                "section": current_heading,
                "text": paragraph,
                "unit_type": "paragraph",
            })

        buffer = []

    for line in lines:

        if not line:
            flush_buffer()
            continue

        # Handle "Strong Heading following text..." safely.
        leading = split_leading_heading(line)

        if leading is not None:
            heading_text, remainder = leading

            flush_buffer()

            current_heading = (
                heading_text.rstrip(":").strip()
            )

            units.append({
                "section": current_heading,
                "text": heading_text,
                "unit_type": "heading",
            })

            if remainder:
                if is_list_start(remainder):
                    units.append({
                        "section": current_heading,
                        "text": remainder,
                        "unit_type": "list_item",
                    })
                else:
                    buffer.append(remainder)

            continue

        if looks_like_heading(line):
            flush_buffer()

            current_heading = (
                line.rstrip(":").strip()
            )

            units.append({
                "section": current_heading,
                "text": line,
                "unit_type": "heading",
            })
            continue

        if is_list_start(line):
            flush_buffer()

            units.append({
                "section": current_heading,
                "text": line,
                "unit_type": "list_item",
            })
            continue

        buffer.append(line)

    flush_buffer()

    return units


# ============================================================
# FALLBACK SPLITTING
# ============================================================

def split_words(
    text,
    max_words=MAX_WORDS,
    overlap_words=OVERLAP_WORDS
):
    """
    Fallback split for an oversized structural unit.
    """
    words = safe_text(text).split()

    if not words:
        return []

    if len(words) <= max_words:
        return [" ".join(words)]

    pieces = []
    start = 0

    while start < len(words):

        end = min(
            start + max_words,
            len(words)
        )

        piece = " ".join(
            words[start:end]
        ).strip()

        if piece:
            pieces.append(piece)

        if end >= len(words):
            break

        start = max(
            end - overlap_words,
            start + 1
        )

    return pieces


# ============================================================
# TINY-CHUNK MERGING
# ============================================================

def is_substantive_short_chunk(chunk):
    """
    Keep a short chunk when it carries clear civic evidence.

    This avoids discarding genuinely useful short votes, motions,
    recommendations, speakers, communications, dates, or formal decisions,
    while removing fragments such as isolated labels or extraction debris.
    """
    section = safe_text(
        chunk.get("section", "")
    ).lower()

    chunk_text = safe_text(
        chunk.get("text", "")
    )

    lower = chunk_text.lower()
    words = chunk_text.split()

    if len(words) >= 12:
        return True

    protected_sections = {
        "city council decision",
        "committee decision",
        "committee recommendations",
        "recommendations",
        "recommendation",
        "motion",
        "motions",
        "motions (committee)",
        "motions (city council)",
        "vote",
        "votes",
        "speakers",
        "communications",
        "communications (committee)",
        "communications (city council)",
        "background information",
        "background information (committee)",
        "background information (city council)",
    }

    if section in protected_sections and len(words) >= 5:
        return True

    civic_signals = [
        "city council",
        "executive committee",
        "adopted",
        "approved",
        "authorize",
        "authorized",
        "recommend",
        "motion",
        "vote",
        "carried",
        "letter from",
        "submission from",
        "e-mail from",
        "email from",
        "petition from",
        "speaker",
    ]

    if any(signal in lower for signal in civic_signals):
        return len(words) >= 5

    return False


def merge_tiny_chunks(chunks):
    """
    Consolidate small chunks without crossing semantic sections.

    Strategy:
    - Remove heading-only fragments.
    - Merge small chunks with adjacent chunks only when section labels match.
    - Remove residual micro-chunks (<12 words) unless they contain
      substantive civic evidence.
    """
    if not chunks:
        return []

    cleaned = []

    for chunk in chunks:
        section = safe_text(
            chunk.get("section", "")
        )

        chunk_text = safe_text(
            chunk.get("text", "")
        )

        if not chunk_text:
            continue

        if (
            section
            and normalize_for_id(chunk_text)
            == normalize_for_id(section)
        ):
            continue

        cleaned.append({
            "section": section,
            "text": chunk_text,
        })

    chunks = cleaned
    i = 0

    while i < len(chunks):

        words = chunks[i]["text"].split()

        if len(words) >= MIN_WORDS:
            i += 1
            continue

        current_section = safe_text(
            chunks[i].get("section", "")
        )

        # Merge backward only inside same section.
        if i > 0:
            previous_section = safe_text(
                chunks[i - 1].get("section", "")
            )

            if previous_section == current_section:
                combined = (
                    chunks[i - 1]["text"].split()
                    + words
                )

                if len(combined) <= MAX_WORDS + 40:
                    chunks[i - 1]["text"] = (
                        chunks[i - 1]["text"].rstrip()
                        + "\n"
                        + chunks[i]["text"].lstrip()
                    )

                    del chunks[i]
                    continue

        # Merge forward only inside same section.
        if i + 1 < len(chunks):
            next_section = safe_text(
                chunks[i + 1].get("section", "")
            )

            if next_section == current_section:
                combined = (
                    words
                    + chunks[i + 1]["text"].split()
                )

                if len(combined) <= MAX_WORDS + 40:
                    chunks[i + 1]["text"] = (
                        chunks[i]["text"].rstrip()
                        + "\n"
                        + chunks[i + 1]["text"].lstrip()
                    )

                    del chunks[i]
                    continue

        i += 1

    # Remove residual non-substantive micro-chunks.
    final_chunks = []

    for chunk in chunks:
        word_count = len(
            chunk["text"].split()
        )

        if (
            word_count < 12
            and not is_substantive_short_chunk(chunk)
        ):
            continue

        final_chunks.append(chunk)

    return final_chunks


# ============================================================
# STRUCTURAL CHUNKING
# ============================================================

def chunk_document(text):
    """
    Structural-first chunking.

    1. Detect headings/paragraphs/list items.
    2. Keep heading with following content where possible.
    3. Split oversized units only as fallback.
    4. Merge tiny fragments backward/forward.
    """
    units = structural_units(text)

    if not units:
        return []

    chunks = []
    current_parts = []
    current_words = 0
    current_section = ""

    def flush_chunk():
        nonlocal current_parts
        nonlocal current_words

        if not current_parts:
            return

        chunk_text = "\n".join(
            part.strip()
            for part in current_parts
            if part.strip()
        ).strip()

        if chunk_text:
            chunks.append({
                "section": current_section,
                "text": chunk_text,
            })

        current_parts = []
        current_words = 0

    for unit in units:

        text_piece = safe_text(
            unit["text"]
        )

        section = safe_text(
            unit["section"]
        )

        unit_type = unit["unit_type"]

        if not text_piece:
            continue

        if unit_type == "heading":
            flush_chunk()
            current_section = section
            current_parts = [
                text_piece
            ]
            current_words = len(
                text_piece.split()
            )
            continue

        if section:
            current_section = section

        word_count = len(
            text_piece.split()
        )

        if word_count > MAX_WORDS:
            flush_chunk()

            pieces = split_words(
                text_piece
            )

            for piece in pieces:
                chunks.append({
                    "section": current_section,
                    "text": piece,
                })

            continue

        if (
            current_words + word_count
            <= MAX_WORDS
        ):
            current_parts.append(
                text_piece
            )
            current_words += word_count

        else:
            flush_chunk()

            current_parts = [
                text_piece
            ]
            current_words = word_count

    flush_chunk()

    return merge_tiny_chunks(
        chunks
    )


# ============================================================
# METADATA
# ============================================================

METADATA_FIELDS = [
    "V01_Date",
    "V02_Publish_Time",
    "V03_Capture_Time",
    "V04_Title",
    "V05_Language",
    "V06_PolicyStage",
    "V07_EventType",
    "V08_ContentType",
    "V09_VerifiedClaim",
    "V10_SourceClass",
    "V11_BodyClass",
    "V12_TopicClass",
    "V13_SourceLink",
    "V14_RequestMethod",
    "V15_Exceptions",
    "V16_Tier",
    "normalized_date",
    "normalized_text_hash",
    "normalization_version",
    "normalization_flags",
]


def build_chunk_records(df):
    """
    Create chunk records with stable IDs and provenance.

    Exact duplicate chunk text is removed only WITHIN the same document.
    Cross-document duplicates are preserved because they can represent
    legitimate mirrored/re-published evidence with different provenance.
    """
    chunk_records = []

    for row_index, row in df.iterrows():

        normalized_text = safe_text(
            row.get(
                "normalized_text",
                ""
            )
        )

        if (
            not normalized_text
            or normalized_text.lower() == "nan"
        ):
            continue

        document_id = create_document_id(
            row
        )

        document_chunks = chunk_document(
            normalized_text
        )

        seen_chunk_hashes = set()
        kept_index = 0

        for chunk in document_chunks:

            chunk_text = safe_text(
                chunk["text"]
            )

            if not chunk_text:
                continue

            chunk_text_hash = hashlib.sha256(
                normalize_for_id(
                    chunk_text
                ).encode("utf-8")
            ).hexdigest()

            if chunk_text_hash in seen_chunk_hashes:
                continue

            seen_chunk_hashes.add(
                chunk_text_hash
            )

            section = safe_text(
                chunk.get(
                    "section",
                    ""
                )
            ) or "Document body"

            chunk_identity = (
                f"{document_id}||"
                f"{kept_index}||"
                f"{section}||"
                f"{chunk_text_hash}"
            )

            chunk_id = (
                f"chunk_"
                f"{stable_hash(chunk_identity, 24)}"
            )

            record = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "chunk_index": kept_index,
                "section_name": section,
                "chunk_text": chunk_text,
                "chunk_word_count": len(
                    chunk_text.split()
                ),
                "chunk_text_hash": chunk_text_hash,
                "source_row_index": int(
                    row_index
                ),
            }

            for field in METADATA_FIELDS:
                record[field] = safe_text(
                    row.get(field, "")
                )

            chunk_records.append(
                record
            )

            kept_index += 1

    return chunk_records


# ============================================================
# SAVE CHUNKS
# ============================================================

def save_chunks(
    chunk_records,
    output_dir
):
    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    jsonl_path = (
        output_dir / CHUNKS_JSONL
    )

    with open(
        jsonl_path,
        "w",
        encoding="utf-8"
    ) as f:
        for record in chunk_records:
            f.write(
                json.dumps(
                    record,
                    ensure_ascii=False
                )
                + "\n"
            )

    chunks_df = pd.DataFrame(
        chunk_records
    )

    csv_path = (
        output_dir / CHUNKS_CSV
    )

    chunks_df.to_csv(
        csv_path,
        index=False
    )

    return (
        jsonl_path,
        csv_path
    )


# ============================================================
# EMBEDDINGS
# ============================================================

def create_embeddings(
    chunk_records,
    model_name=EMBEDDING_MODEL
):
    print(
        f"\nLoading embedding model: "
        f"{model_name}"
    )

    model = SentenceTransformer(
        model_name
    )

    texts = [
        record["chunk_text"]
        for record in chunk_records
    ]

    print(
        f"Embedding {len(texts)} chunks..."
    )

    embeddings = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )

    embeddings = np.asarray(
        embeddings,
        dtype="float32"
    )

    return model, embeddings


# ============================================================
# FAISS INDEX
# ============================================================

def build_faiss_index(
    embeddings
):
    if len(embeddings) == 0:
        raise ValueError(
            "No embeddings were created."
        )

    dimension = embeddings.shape[1]

    index = faiss.IndexFlatIP(
        dimension
    )

    index.add(
        embeddings
    )

    return index


# ============================================================
# QUERY INTENT
# ============================================================

DECISION_QUERY_TERMS = {
    "decision",
    "decisions",
    "approved",
    "approval",
    "adopted",
    "adoption",
    "vote",
    "voted",
    "motion",
    "motions",
    "council",
    "committee",
    "directed",
    "direction",
    "authorized",
    "authorization",
    "funding",
}

STAKEHOLDER_QUERY_TERMS = {
    "stakeholder",
    "stakeholders",
    "reaction",
    "reactions",
    "responded",
    "response",
    "criticized",
    "criticism",
    "support",
    "opposition",
    "consultation",
    "public",
    "resident",
    "residents",
    "community",
}


def classify_query_intent(query):
    """
    Lightweight intent signal used only for reranking.

    This does NOT decide the answer. It adjusts which source types are
    preferred for different evidence needs.
    """
    tokens = set(
        re.findall(
            r"[a-z]+",
            safe_text(query).lower()
        )
    )

    decision_hits = len(
        tokens & DECISION_QUERY_TERMS
    )

    stakeholder_hits = len(
        tokens & STAKEHOLDER_QUERY_TERMS
    )

    if (
        decision_hits > stakeholder_hits
        and decision_hits > 0
    ):
        return "decision"

    if (
        stakeholder_hits > decision_hits
        and stakeholder_hits > 0
    ):
        return "stakeholder"

    return "general"


# ============================================================
# SOURCE AUTHORITY / RERANKING
# ============================================================

OFFICIAL_DOMAINS = {
    "secure.toronto.ca",
    "www.toronto.ca",
    "toronto.ca",
    "www.ttc.ca",
    "ttc.ca",
    "www.waterfrontoronto.ca",
    "waterfrontoronto.ca",
}


def count_stakeholder_evidence_hits(record):
    """
    Count relatively strong indicators of stakeholder/public-response
    evidence. Generic uses of words such as "stakeholder" or "support"
    are intentionally excluded because they create false positives.
    """
    section = safe_text(
        record.get("section_name", "")
    ).lower()

    chunk_text = safe_text(
        record.get("chunk_text", "")
    ).lower()

    strong_terms = [
        "stakeholder advisory committee",
        "public consultation",
        "public workshop",
        "community liaison committee",
        "residents association",
        "resident association",
        "neighbourhood association",
        "neighborhood association",
        "business improvement area",
        "letter from",
        "e-mail from",
        "email from",
        "submission from",
        "petition from",
        "concern",
        "concerns",
        "opposition",
        "feedback",
        "public input",
        "public meeting",
    ]

    section_terms = [
        "consultation",
        "communications",
        "speaker",
        "speakers",
    ]

    hits = sum(
        1
        for term in strong_terms
        if term in chunk_text
    )

    hits += sum(
        1
        for term in section_terms
        if term in section
    )

    return hits


def count_decision_evidence_hits(record):
    """
    Count direct indicators of formal decision/action evidence.
    """
    section = safe_text(
        record.get("section_name", "")
    ).lower()

    chunk_text = safe_text(
        record.get("chunk_text", "")
    ).lower()

    hits = 0

    decision_sections = {
        "city council decision",
        "committee decision",
        "committee recommendations",
        "recommendations",
        "recommendation",
        "motions",
        "motion",
        "motions (committee)",
        "motions (city council)",
        "votes",
        "vote",
    }

    if section in decision_sections:
        hits += 2

    strong_terms = [
        "city council adopted",
        "city council approve",
        "city council approved",
        "city council direct",
        "city council authorized",
        "council adopted",
        "council approved",
        "committee recommends",
        "executive committee recommends",
        "authorized",
        "authorization",
        "funding and direction",
        "recommendations were subsequently adopted",
    ]

    hits += sum(
        1
        for term in strong_terms
        if term in chunk_text
    )

    return hits


def source_authority_bonus(
    record,
    query_intent
):
    """
    Transparent query-sensitive reranking.

    Semantic similarity remains the dominant signal. Bonuses are deliberately
    modest and evidence-specific.
    """
    url = safe_text(
        record.get("V13_SourceLink", "")
    )

    domain = source_domain(url)

    title = safe_text(
        record.get("V04_Title", "")
    ).lower()

    section = safe_text(
        record.get("section_name", "")
    ).lower()

    tier = safe_text(
        record.get("V16_Tier", "")
    ).lower()

    source_class = safe_text(
        record.get("V10_SourceClass", "")
    )

    official = (
        domain in OFFICIAL_DOMAINS
    )

    bonus = 0.0

    # --------------------------------------------------------
    # DECISION QUERY
    # --------------------------------------------------------
    if query_intent == "decision":

        decision_hits = count_decision_evidence_hits(
            record
        )

        formal_decision_sections = {
            "city council decision",
            "committee decision",
            "committee recommendations",
            "recommendations",
            "recommendation",
            "motions",
            "motion",
            "motions (committee)",
            "motions (city council)",
            "votes",
            "vote",
        }

        # Direct evidence first.
        bonus += min(
            decision_hits * 0.020,
            0.060
        )

        # Formal TMMIS record provenance.
        if domain == "secure.toronto.ca":
            bonus += 0.060

            if section in formal_decision_sections:
                bonus += 0.035

        # Official staff-report PDFs.
        elif (
            official
            and (
                "backgroundfile" in url.lower()
                or ".pdf" in url.lower()
            )
        ):
            bonus += 0.025

        # Generic official project/background pages.
        elif official:
            bonus += 0.005

        if source_class == "1":
            bonus += 0.015
        elif source_class == "2":
            bonus += 0.005

        if "p0" in tier:
            bonus += 0.010

        # Derived background/overview pages are useful context, but a
        # decision query should prefer the formal record when available.
        if (
            "background" in title
            and domain != "secure.toronto.ca"
        ):
            bonus -= 0.030

    # --------------------------------------------------------
    # STAKEHOLDER QUERY
    # --------------------------------------------------------
    elif query_intent == "stakeholder":

        stakeholder_hits = count_stakeholder_evidence_hits(
            record
        )

        if any(
            term in section
            for term in [
                "consultation",
                "communications",
                "speaker",
                "speakers",
            ]
        ):
            bonus += 0.040

        bonus += min(
            stakeholder_hits * 0.015,
            0.075
        )

        if any(
            term in title
            for term in [
                "consultation",
                "stakeholder",
                "community",
            ]
        ):
            bonus += 0.015

        if official and stakeholder_hits > 0:
            bonus += 0.005

        # A stakeholder query should not reward generic project-history
        # material with no direct stakeholder/public-response evidence.
        if stakeholder_hits == 0:
            bonus -= 0.040

        # Attendance/administrative board text is rarely stakeholder reaction.
        if (
            section == "attendance"
            and stakeholder_hits < 2
        ):
            bonus -= 0.020

    # --------------------------------------------------------
    # GENERAL QUERY
    # --------------------------------------------------------
    else:

        if official:
            bonus += 0.015

        if "p0" in tier:
            bonus += 0.010

        if source_class == "1":
            bonus += 0.010
        elif source_class == "2":
            bonus += 0.005

    return bonus


# ============================================================
# SEARCH / RETRIEVAL
# ============================================================

def search_index(
    query,
    model,
    index,
    chunk_records,
    top_k=DEFAULT_TOP_K,
    policy_stage=None,
    source_class=None,
    body_class=None,
    max_chunks_per_document=None,
):
    """
    Semantic search + transparent reranking + document diversity.

    Returns:
    - semantic_score
    - source_authority_bonus
    - retrieval_score

    This allows later evaluation of whether reranking improves or harms
    retrieval quality.
    """
    query = safe_text(
        query
    )

    if not query:
        return []

    query_intent = classify_query_intent(
        query
    )

    if max_chunks_per_document is None:
        # Decision and stakeholder searches benefit from source diversity.
        # General searches may return two distinct chunks from one document.
        max_chunks_per_document = (
            1
            if query_intent in {
                "decision",
                "stakeholder",
            }
            else MAX_CHUNKS_PER_DOCUMENT
        )

    query_embedding = model.encode(
        [query],
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32")

    candidate_k = min(
        max(
            top_k
            * RERANK_CANDIDATE_MULTIPLIER,
            top_k
        ),
        len(chunk_records)
    )

    scores, indices = index.search(
        query_embedding,
        candidate_k
    )

    candidates = []

    for semantic_score, idx in zip(
        scores[0],
        indices[0]
    ):

        if idx < 0:
            continue

        record = dict(
            chunk_records[idx]
        )

        if (
            policy_stage is not None
            and safe_text(
                record.get(
                    "V06_PolicyStage",
                    ""
                )
            )
            != safe_text(policy_stage)
        ):
            continue

        if (
            source_class is not None
            and safe_text(
                record.get(
                    "V10_SourceClass",
                    ""
                )
            )
            != safe_text(source_class)
        ):
            continue

        if (
            body_class is not None
            and safe_text(
                record.get(
                    "V11_BodyClass",
                    ""
                )
            )
            != safe_text(body_class)
        ):
            continue

        bonus = source_authority_bonus(
            record,
            query_intent
        )

        record["query_intent"] = (
            query_intent
        )

        record["semantic_score"] = float(
            semantic_score
        )

        record["source_authority_bonus"] = float(
            bonus
        )

        record["stakeholder_evidence_hits"] = (
            count_stakeholder_evidence_hits(record)
            if query_intent == "stakeholder"
            else 0
        )

        record["decision_evidence_hits"] = (
            count_decision_evidence_hits(record)
            if query_intent == "decision"
            else 0
        )

        record["retrieval_score"] = float(
            semantic_score + bonus
        )

        candidates.append(
            record
        )

    candidates.sort(
        key=lambda x: x[
            "retrieval_score"
        ],
        reverse=True
    )

    # ----------------------------
    # Document diversity
    # ----------------------------

    selected = []
    per_document_count = {}

    for record in candidates:

        document_id = record[
            "document_id"
        ]

        count = per_document_count.get(
            document_id,
            0
        )

        if (
            count
            >= max_chunks_per_document
        ):
            continue

        selected.append(
            record
        )

        per_document_count[
            document_id
        ] = count + 1

        if len(selected) >= top_k:
            break

    return selected


def print_search_results(
    results
):
    if not results:
        print(
            "No results."
        )
        return

    for rank, result in enumerate(
        results,
        start=1
    ):
        print(
            "\n"
            + "=" * 70
        )
        print(
            f"RESULT {rank}"
        )
        print(
            "=" * 70
        )

        print(
            "Intent:",
            result.get(
                "query_intent",
                ""
            )
        )

        print(
            "Semantic score:",
            round(
                result[
                    "semantic_score"
                ],
                4
            )
        )

        print(
            "Authority bonus:",
            round(
                result[
                    "source_authority_bonus"
                ],
                4
            )
        )

        print(
            "Retrieval score:",
            round(
                result[
                    "retrieval_score"
                ],
                4
            )
        )

        if result.get("query_intent") == "stakeholder":
            print(
                "Stakeholder evidence hits:",
                result.get(
                    "stakeholder_evidence_hits",
                    0
                )
            )

        if result.get("query_intent") == "decision":
            print(
                "Decision evidence hits:",
                result.get(
                    "decision_evidence_hits",
                    0
                )
            )

        print(
            "Title:",
            result.get(
                "V04_Title",
                ""
            )
        )

        print(
            "Date:",
            result.get(
                "normalized_date",
                ""
            )
        )

        print(
            "Section:",
            result.get(
                "section_name",
                ""
            )
        )

        print(
            "Policy stage:",
            result.get(
                "V06_PolicyStage",
                ""
            )
        )

        print(
            "Source class:",
            result.get(
                "V10_SourceClass",
                ""
            )
        )

        print(
            "Body class:",
            result.get(
                "V11_BodyClass",
                ""
            )
        )

        print(
            "Source:",
            result.get(
                "V13_SourceLink",
                ""
            )
        )

        print(
            "\nChunk:\n"
        )

        print(
            result.get(
                "chunk_text",
                ""
            )
        )


# ============================================================
# SAVE INDEX ARTIFACTS
# ============================================================

def save_index_artifacts(
    output_dir,
    chunk_records,
    embeddings,
    index,
    model_name
):
    output_dir = Path(
        output_dir
    )

    embeddings_path = (
        output_dir / EMBEDDINGS_NPY
    )

    np.save(
        embeddings_path,
        embeddings
    )

    index_path = (
        output_dir
        / FAISS_INDEX_FILE
    )

    faiss.write_index(
        index,
        str(index_path)
    )

    metadata = {
        "pipeline_version": PIPELINE_VERSION,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "embedding_model": model_name,
        "embedding_dimension": int(
            embeddings.shape[1]
        ),
        "chunk_count": len(
            chunk_records
        ),
        "index_type": "FAISS IndexFlatIP",
        "similarity": (
            "cosine via normalized "
            "embeddings + inner product"
        ),
        "max_words": MAX_WORDS,
        "min_words": MIN_WORDS,
        "overlap_words": OVERLAP_WORDS,
        "max_chunks_per_document": (
            MAX_CHUNKS_PER_DOCUMENT
        ),
        "rerank_candidate_multiplier": (
            RERANK_CANDIDATE_MULTIPLIER
        ),
        "reranking": (
            "query-sensitive evidence + source-authority reranking"
        ),
        "document_diversity": (
            "1 chunk/document for decision and stakeholder queries; "
            "2 chunks/document for general queries"
        ),
        "within_document_exact_chunk_deduplication": True,
        "blank_section_label": "Document body",
        "orphan_heading_only_chunks_removed": True,
        "micro_chunk_threshold_words": 12,
        "non_substantive_micro_chunks_removed": True,
        "chunks_jsonl": CHUNKS_JSONL,
        "chunks_csv": CHUNKS_CSV,
        "embeddings_file": EMBEDDINGS_NPY,
        "faiss_index_file": (
            FAISS_INDEX_FILE
        ),
    }

    metadata_path = (
        output_dir
        / INDEX_METADATA_FILE
    )

    with open(
        metadata_path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            metadata,
            f,
            indent=2,
            ensure_ascii=False
        )

    return {
        "embeddings": embeddings_path,
        "index": index_path,
        "metadata": metadata_path,
    }


# ============================================================
# CHUNK QUALITY REPORT
# ============================================================

def print_chunk_quality_report(
    chunk_records
):
    chunks_df = pd.DataFrame(
        chunk_records
    )

    print(
        "\n"
        + "=" * 60
    )
    print(
        "CHUNK QUALITY REPORT"
    )
    print(
        "=" * 60
    )

    print(
        "\nTotal chunks:",
        len(chunks_df)
    )

    counts = chunks_df[
        "chunk_word_count"
    ]

    print(
        "Minimum words:",
        int(counts.min())
    )

    print(
        "Mean words:",
        round(
            counts.mean(),
            1
        )
    )

    print(
        "Maximum words:",
        int(counts.max())
    )

    tiny = chunks_df[
        chunks_df[
            "chunk_word_count"
        ] < MIN_WORDS
    ]

    print(
        f"Chunks below "
        f"{MIN_WORDS} words:",
        len(tiny)
    )


    micro = chunks_df[
        chunks_df[
            "chunk_word_count"
        ] < 12
    ]

    print(
        "Chunks below 12 words:",
        len(micro)
    )

    blank_sections = (
        chunks_df[
            "section_name"
        ]
        .fillna("")
        .str.strip()
        .eq("")
        .sum()
    )

    print(
        "Chunks without section name:",
        int(blank_sections)
    )

    document_body_count = (
        chunks_df["section_name"]
        .fillna("")
        .eq("Document body")
        .sum()
    )

    print(
        "Chunks labelled Document body:",
        int(document_body_count)
    )

    # Diagnostic: section labels containing no alphabetic characters.
    false_heading_like = chunks_df[
        chunks_df[
            "section_name"
        ]
        .fillna("")
        .str.strip()
        .ne("")
        &
        ~chunks_df[
            "section_name"
        ]
        .fillna("")
        .str.contains(
            r"[A-Za-z]",
            regex=True
        )
    ]

    print(
        "Numeric-only section labels:",
        len(false_heading_like)
    )

    if len(false_heading_like) > 0:
        print(
            "\nNumeric-only section labels "
            "requiring review:"
        )

        print(
            false_heading_like[
                [
                    "section_name",
                    "V04_Title",
                ]
            ]
            .drop_duplicates()
            .to_string(
                index=False
            )
        )


    duplicate_within_doc = chunks_df.duplicated(
        subset=[
            "document_id",
            "chunk_text_hash",
        ],
        keep=False,
    ).sum()

    print(
        "Exact duplicate chunks within documents:",
        int(duplicate_within_doc)
    )

    critical_headings = [
        "Committee Recommendations",
        "Background Information",
        "Communications",
    ]

    missing_critical = [
        h
        for h in critical_headings
        if h.lower() not in LINE_LEVEL_HEADINGS
    ]

    print(
        "Critical heading definitions missing:",
        len(missing_critical)
    )

    if missing_critical:
        print(
            "Missing critical headings:",
            missing_critical
        )

    print(
        "\nMost common section labels:"
    )

    section_counts = (
        chunks_df["section_name"]
        .fillna("")
        .replace("", "[NO SECTION]")
        .value_counts()
        .head(15)
    )

    print(
        section_counts.to_string()
    )


# ============================================================
# PIPELINE
# ============================================================

def main():

    if not INPUT_FILE:
        raise ValueError(
            "Set INPUT_FILE before running."
        )

    if not OUTPUT_DIR:
        raise ValueError(
            "Set OUTPUT_DIR before running."
        )

    input_path = Path(
        INPUT_FILE
    )

    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: "
            f"{input_path}"
        )

    print(
        "Loading normalized corpus..."
    )

    df = pd.read_excel(
        input_path
    )

    required_columns = [
        "normalized_text",
        "V13_SourceLink",
        "V04_Title",
    ]

    missing = [
        col
        for col in required_columns
        if col not in df.columns
    ]

    if missing:
        raise ValueError(
            "Missing required column(s): "
            + ", ".join(missing)
        )

    print(
        f"Loaded {len(df)} documents."
    )

    print(
        "\nCreating structural chunks..."
    )

    chunk_records = build_chunk_records(
        df
    )

    if not chunk_records:
        raise ValueError(
            "No chunks were created."
        )

    print_chunk_quality_report(
        chunk_records
    )

    jsonl_path, csv_path = save_chunks(
        chunk_records,
        OUTPUT_DIR
    )

    print(
        "\nSaved chunks:"
    )
    print(
        jsonl_path
    )
    print(
        csv_path
    )

    model, embeddings = create_embeddings(
        chunk_records
    )

    print(
        "\nBuilding FAISS index..."
    )

    index = build_faiss_index(
        embeddings
    )

    artifact_paths = save_index_artifacts(
        OUTPUT_DIR,
        chunk_records,
        embeddings,
        index,
        EMBEDDING_MODEL,
    )

    print(
        "\nIndex build complete."
    )

    for name, path in artifact_paths.items():
        print(
            f"{name}: {path}"
        )

    # --------------------------------------------------------
    # RETRIEVAL TEST 1 — DECISION
    # --------------------------------------------------------

    decision_query = (
        "What decisions were made "
        "about advancing the "
        "Waterfront East LRT?"
    )

    print(
        "\n"
        + "#" * 70
    )
    print(
        "TEST QUERY 1 — DECISION"
    )
    print(
        "#" * 70
    )
    print(
        decision_query
    )

    results = search_index(
        decision_query,
        model,
        index,
        chunk_records,
        top_k=5,
    )

    print_search_results(
        results
    )

    # --------------------------------------------------------
    # RETRIEVAL TEST 2 — STAKEHOLDER
    # --------------------------------------------------------

    stakeholder_query = (
        "What concerns or reactions "
        "did stakeholders raise about "
        "the Waterfront East LRT?"
    )

    print(
        "\n"
        + "#" * 70
    )
    print(
        "TEST QUERY 2 — STAKEHOLDER"
    )
    print(
        "#" * 70
    )
    print(
        stakeholder_query
    )

    results = search_index(
        stakeholder_query,
        model,
        index,
        chunk_records,
        top_k=5,
    )

    print_search_results(
        results
    )


if __name__ == "__main__":
    main()
