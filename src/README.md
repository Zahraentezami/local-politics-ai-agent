# Local Politics Search Agent — Core Source Code

The `src` directory contains the core code used to build and maintain the **Local Politics Search Agent**.

| Stage | Primary script/file |
|---|---|
| Source pathways and retrieval | `pathway_connectors_WELRT_v2.py` |
| Metadata/codebook alignment | `pathway_connectors_WELRT_v2.py` |
| Text normalization | `normalizer_script.py` |
| Normalization quality control | `normalizer_script.py` |
| Structural chunking | `retrieval_index.py` |
| Chunk quality validation | `retrieval_index.py` |
| Sentence-transformer embeddings | `retrieval_index.py` |
| FAISS index construction | `retrieval_index.py` |
| Retrieval testing | `retrieval_index.py` |
| Runtime evidence retrieval | `search_agent.py` |
| Query-intent reranking | `search_agent.py` |
| Timeline evidence handling | `search_agent.py` |
| Deterministic extractive answers | `search_agent.py` |
| Clickable author-date citations | `search_agent.py` |
| Local search interface | `streamlit_app.py` |
| Local Streamlit server settings | `.streamlit/config.toml` |
| Dependency specification | `requirements.txt` |
| Example runtime configuration | `example_config.json` |

The scripts are organized around the following general workflow:

```text
Public sources
        ↓
Source discovery / pathways
        ↓
Retrieval and extraction
        ↓
Metadata and codebook alignment
        ↓
Text normalization
        ↓
Validation and quality control
        ↓
Corpus ingestion
        ↓
Structural chunking
        ↓
Embedding and FAISS indexing
        ↓
Evidence retrieval and reranking
        ↓
Extractive answer generation
        ↓
Local Streamlit search interface
```

The codebase is being developed incrementally. Source acquisition and corpus preparation establish the evidence base, while retrieval, answer generation, and the user-facing interface operate on the processed corpus.

Retrieval uses local sentence-transformer embeddings and FAISS, while the current answer layer uses deterministic extractive synthesis over retrieved evidence.

---

# Scripts

## Source Pathways and Connectors

**Related script:** `pathway_connectors_WELRT.py`

The connector and pathway script establishes access to the different kinds of records included in the project corpus.

Depending on the source, the script may:

* retrieve webpages, agenda items, reports, PDFs, spreadsheets, or other supported records;
* identify the type of source being retrieved;
* respect source-specific access requirements such as `robots.txt`;
* extract full text and relevant metadata;
* handle source-specific webpage or document structures;
* identify links to related materials;
* record retrieval failures and exceptions;
* support discovery of newly available or upcoming records;
* preserve the original source URL and retrieval provenance.

Because municipal information is distributed across different websites, systems, and file formats, connectors may use source-specific extraction logic and will return records in a shared structure.

The connector layer is responsible for retrieval and extraction and does not supprt text cleaning, indexing, or answer generation.

The current pathway script was initially developed around the Waterfront East LRT pilot corpus. Source-specific logic should therefore remain distinguishable from reusable connector infrastructure as the corpus expands.

---

## Structured Metadata and Codebook Alignment

**Related script:** `pathway_connectors_WELRT_v2.py`

Metadata and codebook alignment currently begins at the connector stage, where retrieved records are mapped to the project's common metadata structure.

Fields may describe attributes such as:

* publication or event date;
* publication time;
* capture time;
* title;
* language;
* policy stage;
* event type;
* content type;
* verified claim;
* source class;
* institutional or body class;
* topic class;
* source URL;
* retrieval method;
* exceptions or review notes;
* source or processing tier.

Additional structured information may also be retained where available, including:

* committee or decision-making body;
* voting information;
* councillor vote breakdowns;
* related consultation links;
* related reports or agenda items;
* video or webcast links;
* full extracted source text.

The shared metadata structure allows records from different source systems to be processed consistently while preserving their original provenance.

---

## Text Normalization

**Related script:** `normalizer_script.py`

The normalization script converts retrieved text into a more consistent representation suitable for corpus storage, chunking, and later retrieval.

Normalization is deliberately conservative. Its purpose is to remove extraction noise and standardize document structure without rewriting, summarizing, or changing the substantive meaning of the source.

The script may perform:

* Unicode normalization;
* whitespace cleanup;
* removal of navigation and website-interface text;
* source-specific boilerplate removal;
* PDF layout cleanup;
* reconstruction of hard-wrapped paragraphs;
* preservation of headings and meaningful document sections;
* normalization of municipal item identifiers;
* preservation of numbered recommendations, motions, and lists;
* normalization of selected metadata formats such as dates;
* creation of normalized-text hashes;
* preservation of the original retrieved text alongside normalized text where required.

The normalization stage also performs quality-control checks and creates review flags for issues such as:

* failed retrievals;
* sources blocked from automated retrieval;
* missing or unusually short text;
* possible extraction truncation;
* low-confidence OCR;
* missing, invalid, or ambiguous dates;
* duplicate or near-duplicate material;
* records requiring human review;
* redaction or privacy-related issues;
* unexpected source classifications.

Where possible, uncertainty is recorded rather than silently resolved.

Normalization also supports duplicate detection and record identity.

The same civic record may appear through:

* multiple URLs;
* repeated retrieval runs;
* mirrored files;
* revised documents;
* duplicated source batches;
* different access pathways to the same municipal item.

The deduplication process may use:

* canonicalized URLs;
* content hashes;
* normalized-text hashes;
* municipal item identifiers;
* document metadata;
* capture timestamps.

These signals help distinguish exact duplicates, repeated captures, and materially changed document versions.

---

**Retrieval Layer supports various functions stated below:**

**Related script:**
- `retrieval_index.py`

## Validation and Quality Control / Structural Chunking

Quality control occurs at more than one stage of the pipeline.

The normalization script identifies source- and text-level issues such as:

* missing text;
* failed retrievals;
* low-confidence OCR;
* possible truncation;
* missing or malformed dates;
* duplicate records;
* redaction-related issues;
* unusual source classifications.

The retrieval-index script performs a separate set of checks over the processed chunks, including:

* total chunk counts;
* minimum, mean, and maximum chunk size;
* unusually short chunks;
* missing section names;
* numeric-only or false section labels;
* duplicate chunks within documents;
* expected structural heading detection.

This separation is intentional: normalization quality concerns the source record, while index quality concerns the evidence units used for retrieval.
The retrieval-index script converts normalized civic documents into searchable evidence units.

The structural chunking stage follows:

```text
normalized documents
        ↓
structural section detection
        ↓
chunk construction
        ↓
chunk cleanup and merging
        ↓
chunk quality checks
```

The script uses structural-first chunking rather than relying only on fixed token or word windows.

Where available, municipal document structure is preserved through sections such as:

* City Council Decision;
* Committee Decision;
* Committee Recommendations;
* Recommendations;
* Motions;
* Votes;
* Speakers;
* Communications;
* Public Consultation;
* Background Information;
* Decision History;
* Financial Impact;
* Project Status;
* Implementation;
* Next Steps.

Long sections may be split into smaller chunks when necessary, while very small fragments are merged or removed where they do not contain meaningful civic evidence.

The chunking process is designed to avoid crossing meaningful section boundaries unnecessarily.

Each chunk retains provenance and document metadata so retrieved evidence can be traced back to its original source.

### Chunk Identity and Deduplication

The indexing pipeline creates stable document and chunk identifiers based on source information, normalized text, metadata, and content hashes.

Exact duplicate chunks within the same document are removed.

Cross-document duplicates are preserved when appropriate because identical or near-identical text may occur in separate records with different provenance or administrative meaning.

---

## Embeddings and FAISS Index Construction

After chunking, the script creates local semantic embeddings using:

```text
sentence-transformers/all-MiniLM-L6-v2
```

The embeddings are normalized before indexing.

No external embedding API is required.

The normalized embeddings are stored in a FAISS inner-product index. Because the embeddings are normalized, inner-product similarity functions as cosine similarity.

The script generates artifacts such as:

```text
chunks.jsonl
chunks.csv
embeddings.npy
faiss.index
index_metadata.json
```

At minimum, the search application requires:

```text
chunks.jsonl
faiss.index
```

The remaining files support inspection, reproducibility, evaluation, and rebuilding.

Generated index artifacts may be excluded from Git version control where they are large, reproducible, or contain corpus material that should not be distributed publicly.

---

## Retrieval Testing During Index Construction

The index-building script also contains retrieval functions used to test the newly created index.

These tests support inspection of:

* semantic similarity;
* source-authority reranking;
* decision-related evidence;
* stakeholder-related evidence;
* document diversity;
* provenance metadata;
* retrieved chunk quality.

This retrieval functionality is primarily intended for **index validation**.

The user-facing search and answer workflow is implemented separately in `search_agent.py`.

---

## Evidence Retrieval and Reranking

**Related script:** `search_agent.py`

The search-agent script loads an existing retrieval index and searches it using the same sentence-transformer model used to create the document embeddings.

The runtime retrieval process is:

```text
user question
        ↓
query embedding
        ↓
FAISS candidate retrieval
        ↓
query-intent detection
        ↓
transparent reranking
        ↓
document-diverse evidence set
```

Semantic similarity remains the primary retrieval signal.

A lightweight intent classifier currently distinguishes broad evidence needs such as:

* decision-related questions;
* stakeholder or consultation questions;
* timeline or historical-development questions;
* general information questions.

The intent classifier does not itself generate an answer. It affects how retrieved evidence is ranked and selected.

For example:

* decision questions may prioritize formal decision, recommendation, motion, or vote sections;
* stakeholder questions may prioritize consultation, communications, speaker, or public-response evidence;
* timeline questions may prioritize dated events and chronological development;
* general questions rely more directly on semantic relevance.

The reranking logic is deliberately transparent and heuristic so its effects can be inspected and evaluated.

The retrieval stage also applies document-diversity controls so that one long document does not dominate the evidence returned to the user.

---

## Extractive Answer Generation

**Related script:** `search_agent.py`

The current answer layer operates without a generative AI API.

Instead, it uses deterministic extractive synthesis.

The process is:

```text
retrieved evidence
        ↓
sentence segmentation
        ↓
question-relevance scoring
        ↓
selection of relevant source sentences
        ↓
chronological ordering where appropriate
        ↓
fixed connective language
        ↓
cited answer
```

Factual content is taken from retrieved corpus passages rather than generated from outside knowledge.

This design allows retrieval quality and evidence presentation to be tested independently from any future generative answer model.

The current answer layer:

* selects relevant sentences from retrieved chunks;
* produces more readable responses than raw retrieval output;
* organizes timeline questions chronologically where dates are available;
* distinguishes evidence retrieval from answer presentation;
* does not browse the public web when answering;
* does not use outside knowledge to fill evidence gaps;
* can return an insufficient-evidence response when retrieval confidence is too low.

The current extractive approach is intended as a transparent baseline. A future generative synthesis layer may be added after retrieval performance has been evaluated against a broader question set.

---

## Inline Source Citations

**Related script:** `search_agent.py`

Answers include clickable author-date citations.

For example:

```text
The project advanced to a later design stage
[City of Toronto, 2025](source-url).
```

Citation labels are generated programmatically rather than by an AI model.

The citation formatter uses source metadata such as:

* source domain;
* corporate or institutional author;
* normalized publication date;
* original source URL.

Domain-to-author mappings are configurable.

For example:

```text
toronto.ca              → City of Toronto
ttc.ca                  → Toronto Transit Commission
waterfrontoronto.ca     → Waterfront Toronto
```

Where no author mapping exists, the system can fall back to the source title.

Where no reliable publication year exists, the citation may use:

```text
n.d.
```

The source URL is embedded directly in the in-text citation so users can move from an answer claim to the underlying evidence.

---

## Query Logging

**Related script:** `search_agent.py`

The search layer can optionally record query activity in a local JSONL log.

Logged information may include:

* timestamp;
* agent version;
* embedding model;
* question;
* detected query intent;
* retrieved document and chunk identifiers;
* source title;
* section;
* source URL;
* citation label;
* semantic similarity score;
* reranking score;
* final answer.

Query logs are development and evaluation artifacts and should generally not be committed to the public repository.

Logging can be disabled when required.

---

## Local Streamlit Interface

**Related script:** `streamlit_app.py`

The Streamlit interface provides a local user-facing search environment over the indexed corpus.

The interface currently supports:

* chat-style question entry;
* display of extractive answers;
* clickable source citations;
* local loading of the FAISS index;
* configurable retrieval depth;
* configurable answer length;
* minimum semantic-similarity thresholds;
* optional query logging;
* optional display of retrieved evidence;
* inspection of semantic scores and reranking scores;
* direct links to original source records.

The interface imports retrieval and answer functions from:

```text
search_agent.py
```

rather than implementing a second search system.

This separation allows the search logic to be tested independently from the user interface.

---

## Local Streamlit Configuration

**Related file:** `.streamlit/config.toml`

The current Streamlit configuration is intended for local development.

The server is configured to bind to:

```text
127.0.0.1:8501
```

so the application is accessible only from the machine running it.

No purchased domain, public hosting environment, or authentication layer is required for the current development version.

A public deployment layer can be added separately if and when the project reaches that stage.

---

## Search Configuration

**Related file:** `example_config.json`

Reusable search settings can be specified through a configuration file rather than hard-coded machine-specific values.

Example settings include:

```json
{
  "index_dir": "./retrieval_index",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "top_k": 8,
  "rerank_candidate_multiplier": 15,
  "min_top_semantic_score": 0.3,
  "max_synthesis_sources": 7,
  "max_sentences_per_source": 3,
  "max_timeline_events": 10
}
```

Source-author mappings may also be configured without changing the core search code.

Personal absolute paths such as:

```text
/Users/.../
```

should not be committed to the public repository.

---

## Python Dependencies

**Related file:** `requirements.txt`

The project dependencies required by the current scripts are recorded in `requirements.txt`.

Current major dependencies include:

* `streamlit`;
* `faiss-cpu`;
* `sentence-transformers`;
* `numpy`;
* `pandas`;
* `openpyxl`.

A Python virtual environment is recommended rather than installing project dependencies into the system Python environment.

---

## Running the Local Search Application

**Related files:**
- `streamlit_app.py`
- `civic_search_agent.py`
- `requirements.txt`
- `.streamlit/config.toml`

From the project directory, create a virtual environment:

```bash
python3 -m venv .venv
```

Activate it on macOS or Linux:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the Streamlit application:

```bash
python -m streamlit run src/streamlit_app.py
```

If the scripts are stored under a different repository path, adjust the command accordingly.

The application should then be available locally at:

```text
http://127.0.0.1:8501
```

The retrieval-index directory must contain at minimum:

```text
chunks.jsonl
faiss.index
```

The path to that directory can be entered in the Streamlit interface or supplied through configuration.

---

## Retrieval Index Artifacts

**Produced by:** `retrieval_index.py`  
**Consumed by:** `search_agent.py` and `streamlit_app.py`

The retrieval index acts as the interface between the offline corpus-processing pipeline and the runtime search application.

Typical contents are:

```text
retrieval_index/
├── chunks.jsonl
├── chunks.csv
├── embeddings.npy
├── faiss.index
└── index_metadata.json
```

`retrieval_index.py` creates these artifacts.

`search_agent.py` requires the saved chunks and FAISS index to answer questions.

`streamlit_app.py` exposes the same search functionality through the local interface.

These are generated artifacts rather than source-code files and may be excluded from public version control.

---

## Validation and Evaluation

**Related scripts:**
- `retrieval_index.py`
- `search_agent.py`

The retrieval system is being evaluated incrementally.

Current validation has focused on whether the system:

* retrieves substantively relevant evidence;
* prioritizes formal records for decision questions;
* surfaces stakeholder and consultation evidence appropriately;
* preserves source provenance;
* avoids domination by a single long document;
* maintains meaningful structural boundaries;
* supports chronological questions;
* produces traceable cited answers.

`retrieval_index.py` currently supports lower-level retrieval and chunk-quality diagnostics.

`search_agent.py` allows testing of runtime evidence selection and extractive answer behavior.

A broader formal evaluation framework is still required.

Future evaluation should use human-defined **golden questions** covering categories such as:

* decisions;
* policy history;
* timelines;
* votes;
* stakeholder responses;
* consultations;
* funding;
* implementation;
* current status;
* future or scheduled action;
* source lookup;
* questions for which the corpus contains no answer.

Possible retrieval metrics include:

* Hit@K;
* Recall@K;
* Mean Reciprocal Rank;
* nDCG where graded relevance judgments are available.

Retrieval quality should be evaluated separately from answer-generation quality.

---

## Provenance

**Related scripts:**
- `pathway_connectors_WELRT.py`
- `local_politics_normalization_general.py`
- `retrieval_index.py`
- `search_agent.py`

Provenance is preserved throughout the pipeline rather than added only at the final search stage.

Relevant provenance may include:

* source URL;
* municipal document or item identifier;
* document title;
* publication date;
* event date where separately identified;
* capture time;
* source class;
* institutional body;
* retrieval method;
* document hash;
* normalized-text hash;
* normalization version;
* chunk identifier;
* processing version;
* quality and review flags.

A distinction should be maintained between a document's metadata date and dates of events described within that document, particularly for continuously updated webpages.

---

## Human Review

**Related scripts:**
- `local_politics_normalization_general.py`
- `retrieval_index.py`
- `search_agent.py`

Automated processing should not be treated as authoritative where evidence is uncertain.

Records may require human review when there is:

* low-confidence extraction;
* OCR uncertainty;
* unexpected document structure;
* ambiguous classification;
* uncertain deduplication;
* possible off-topic retrieval;
* substantive redaction;
* uncertain metadata;
* conflicting records;
* unusually low retrieval confidence.

Human review should complement rather than be hidden by automated processing.

---

## Script Relationships

The current scripts operate as connected but separate stages:

```text
pathway_connectors_WELRT.py
        │
        │ retrieves source records
        ▼
local_politics_normalization_general.py
        │
        │ cleans and standardizes text
        ▼
[Corpus ingestion — planned]
        │
        ▼
build_retrieval_index.py
        │
        │ creates chunks + embeddings + FAISS
        ▼
retrieval_index/
        │
        ├── chunks.jsonl
        ├── faiss.index
        └── supporting artifacts
        │
        ▼
civic_search_agent.py
        │
        │ retrieval + reranking
        │ extractive answers
        │ clickable citations
        ▼
streamlit_app.py
        │
        ▼
Local browser interface
```
