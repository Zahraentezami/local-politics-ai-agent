The `src` directory contains the core code used to build and maintain the **Local Politics Search Agent**.

The code in this directory supports the stages required to move from heterogeneous public-source material to a consistent, traceable, and eventually searchable corpus.

The scripts are organized around a general workflow:

```text
Public and curated sources
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
Chunking / indexing
        ↓
Retrieval and agent responses
```

The codebase is being developed incrementally, with source acquisition and corpus preparation preceding the retrieval and user-facing agent layers.

## Scripts

The scripts are as follows:

### Source pathways and connectors

Connector and pathway script establishes access to the different kinds of records included in the project corpus.

Depending on the source, the script may:

* retrieve webpages, agenda items, reports, PDFs, spreadsheets, or other supported records;
* identify the type of source being retrieved;
* respect source-specific access requirements such as `robots.txt`;
* extract full text and relevant metadata;
* handle source-specific page structures;
* identify links to related materials;
* record retrieval failures and exceptions;
* support discovery of newly available or upcoming records.

Because municipal information is distributed across different websites and file formats, connectors may use source-specific extraction logic while returning records in a shared structure.

## Structured Metadata and Codebook Alignment

Retrieved records are mapped to the project's common metadata/codebook structure.

Fields describe attributes such as:

* publication or event date;
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
* video or webcast links;
* full extracted source text.


## Text Normalization

Normalization script converts retrieved text into a more consistent representation suitable for corpus storage and later retrieval.

Normalization is deliberately conservative and removes extraction noise without rewriting the source.

The does of this script include:

* Unicode normalization;
* whitespace cleanup;
* removal of navigation and website interface text;
* source-specific boilerplate removal;
* PDF layout cleanup;
* reconstruction of hard-wrapped paragraphs;
* preservation of headings and meaningful document sections;
* normalization of municipal item identifiers;
* preservation of numbered recommendations and lists;
* normalization of selected metadata formats such as dates.


In addition, the script does quality-control checks and flags, including:

* failed retrievals;
* sources blocked from automated retrieval;
* missing or unusually short text;
* possible extraction truncation;
* low-confidence OCR;
* missing or ambiguous dates;
* duplicate or near-duplicate material;
* records requiring human review;
* redaction or privacy-related issues;
* unexpected source classifications.

Where possible, uncertainty is recorded.

It does Deduplication and Record Identity as well to catch the same civic record through multiple URLs, repeated retrieval runs, mirrored files, or duplicated source batches.

The deduplication process uses:

* canonicalized URLs;
* content hashes;
* normalized-text hashes;
* municipal item identifiers;
* document metadata;
* capture timestamps

to help identify duplicate or changed records.

To be done next -->

--> ## Corpus Ingestion

