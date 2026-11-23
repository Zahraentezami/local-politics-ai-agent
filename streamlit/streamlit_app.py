"""
Local Streamlit UI for the Sample-Corpus Civic Search Agent.

Run locally:
    streamlit run streamlit_app.py --server.address=127.0.0.1

Expected files:
    civic_search_agent.py
    streamlit_app.py
    retrieval_index/
        chunks.jsonl
        faiss.index
"""

from pathlib import Path

import streamlit as st
import json

from civic_search_agent import (
    Settings,
    DEFAULT_AUTHOR_MAP,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_TOP_K,
    DEFAULT_RERANK_CANDIDATE_MULTIPLIER,
    DEFAULT_MIN_TOP_SEMANTIC_SCORE,
    DEFAULT_MAX_SYNTHESIS_SOURCES,
    DEFAULT_MAX_SENTENCES_PER_SOURCE,
    DEFAULT_MAX_TIMELINE_EVENTS,
    load_retrieval_system,
    retrieve_evidence,
    make_evidence_bundle,
    generate_grounded_answer,
    validate_citations,
    render_inline_links,
    write_query_log,
)


# ============================================================
# PAGE
# ============================================================

st.set_page_config(
    page_title="Civic Search",
    page_icon="🔎",
    layout="wide",
)

st.title("Civic Search")
st.caption(
    "Sample-corpus public-record search with"
    "synthesis and in-text source citations."
)


# ============================================================
# HELPERS
# ============================================================

def valid_index_dir(path_value: str) -> tuple[bool, str]:
    if not path_value.strip():
        return False, "Enter the path to the retrieval index."

    path = Path(path_value).expanduser()

    if not path.exists():
        return False, f"Directory does not exist: {path}"

    missing = [
        name
        for name in ("chunks.jsonl", "faiss.index")
        if not (path / name).exists()
    ]

    if missing:
        return (
            False,
            "Index directory is missing: " + ", ".join(missing),
        )

    return True, ""


@st.cache_resource(show_spinner="Loading search index and embedding model...")
def load_cached_retrieval_system(
    index_dir: str,
    embedding_model: str,
):
    """
    Cache expensive model/index loading across Streamlit reruns.
    """
    minimal_settings = Settings(
        index_dir=Path(index_dir).expanduser().resolve(),
        embedding_model=embedding_model,
        enable_query_log=False,
    )

    return load_retrieval_system(
        minimal_settings
    )


def build_settings(
    index_dir: str,
    top_k: int,
    min_score: float,
    max_sources: int,
    max_sentences: int,
    max_timeline_events: int,
    enable_log: bool,
):
    return Settings(
        index_dir=Path(index_dir).expanduser().resolve(),
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        top_k=top_k,
        rerank_candidate_multiplier=DEFAULT_RERANK_CANDIDATE_MULTIPLIER,
        min_top_semantic_score=min_score,
        max_synthesis_sources=max_sources,
        max_sentences_per_source=max_sentences,
        max_timeline_events=max_timeline_events,
        enable_query_log=enable_log,
        author_map=dict(DEFAULT_AUTHOR_MAP),
    )


def answer_with_evidence(
    question,
    model,
    index,
    chunks,
    settings,
):
    """
    Run one retrieval + synthesis pass and return both the answer and
    retrieved evidence for optional inspection in the UI.
    """
    intent, evidence = retrieve_evidence(
        question,
        model,
        index,
        chunks,
        settings,
    )

    if not evidence:
        return (
            "The available corpus does not contain retrievable evidence "
            "for this question.",
            intent,
            [],
        )

    top_semantic_score = max(
        record["semantic_score"]
        for record in evidence
    )

    if top_semantic_score < settings.min_top_semantic_score:
        return (
            "The available corpus does not contain sufficiently relevant "
            "evidence to answer this question reliably.",
            intent,
            evidence,
        )

    evidence_bundle, citation_map = make_evidence_bundle(
        evidence
    )

    raw_answer = generate_grounded_answer(
        question,
        intent,
        evidence_bundle,
        evidence_records=evidence,
        settings=settings,
    )

    validate_citations(
        raw_answer,
        citation_map,
    )

    rendered_answer = render_inline_links(
        raw_answer,
        citation_map,
    )

    write_query_log(
        settings,
        question,
        intent,
        evidence,
        raw_answer,
        rendered_answer,
    )

    return (
        rendered_answer,
        intent,
        evidence,
    )


def show_evidence(evidence):
    if not evidence:
        st.info("No retrieved evidence to display.")
        return

    for rank, record in enumerate(
        evidence,
        start=1,
    ):
        title = (
            record.get("V04_Title")
            or "Untitled source"
        )

        section = (
            record.get("section_name")
            or "Document body"
        )

        source_url = (
            record.get("V13_SourceLink")
            or ""
        )

        with st.expander(
            f"{rank}. {title} — {section}"
        ):
            cols = st.columns(3)

            cols[0].metric(
                "Semantic",
                f"{record.get('semantic_score', 0):.3f}",
            )

            cols[1].metric(
                "Rerank bonus",
                f"{record.get('rerank_bonus', 0):.3f}",
            )

            cols[2].metric(
                "Final retrieval",
                f"{record.get('retrieval_score', 0):.3f}",
            )

            date = (
                record.get("normalized_date")
                or record.get("V01_Date")
                or "Unknown"
            )

            st.caption(
                f"Date: {date} · Section: {section}"
            )

            st.write(
                record.get("chunk_text", "")
            )

            if source_url:
                st.markdown(
                    f"[Open original source]({source_url})"
                )

WELRT_RECORDS_PATH = Path(__file__).resolve().with_name("welrt_records.json")


def load_welrt_snapshot(path: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Expected a list of source records.")

    snapshot = {}

    for row in rows:
        tier = str(row.get("V16_Tier") or "").casefold()
        if "failed" in tier or "robots-blocked" in tier:
            continue

        url = str(row.get("V13_SourceLink") or "").strip()
        if not url.startswith(("https://", "http://")):
            continue

        # Strip only a dangling '?' or fragment; preserve ?item=YYYY.EX...
        record_id = url.split("#", 1)[0].rstrip("?")
        title = str(row.get("V04_Title") or record_id)
        vote = row.get("vote_record")

        snapshot[record_id] = {
            "title": title,
            "url": record_id,
            "claim": " ".join(str(row.get("V09_VerifiedClaim") or "").split()),
            "text": " ".join(str(row.get("raw_text") or "").split()),
            "vote": vote,
        }

    return snapshot


ISSUE_FEEDS = {
    "waterfront-east-lrt": {
        "label": "Waterfront East LRT",
        "path": WELRT_RECORDS_PATH,
    },
    # Add more entries here when their JSON feeds are ready.
}


def check_issue_feed(issue, watch):
    try:
        current = load_welrt_snapshot(issue["path"])
    except (OSError, ValueError) as exc:
        st.warning(f"Could not check {issue['label']}: {exc}")
        return None

    added = 0

    for record_id, record in current.items():
        previous = watch["seen"].get(record_id)

        if previous is None:
            event = "New record"
        elif record == previous:
            continue
        elif record["vote"] and record["vote"] != previous["vote"]:
            event = (
                "Vote details updated" if previous["vote"]
                else "Vote details added"
            )
        else:
            event = "Updated record"

        watch["unread"].append((event, record["title"], record["url"]))
        added += 1

    # Keep the last successful version if a source is missing in this run.
    watch["seen"].update(current)
    return added


def render_issue_alerts():
    watches = st.session_state.setdefault("configured_issue_watches", {})

    with st.sidebar:
        st.divider()
        st.header("Issue alerts")

        issue_id = st.selectbox(
            "Issue or initiative",
            options=list(ISSUE_FEEDS),
            format_func=lambda key: ISSUE_FEEDS[key]["label"],
            key="configured_issue_picker",
        )
        issue = ISSUE_FEEDS[issue_id]

        if issue_id in watches:
            st.caption(f"Following {issue['label']}")
        elif st.button("Follow this issue", key="configured_issue_follow"):
            try:
                baseline = load_welrt_snapshot(issue["path"])
            except (OSError, ValueError) as exc:
                st.warning(f"Could not follow {issue['label']}: {exc}")
            else:
                watches[issue_id] = {"seen": baseline, "unread": []}
                st.rerun()

        active_ids = [key for key in watches if key in ISSUE_FEEDS]

        if st.button(
            "Check for source updates",
            key="configured_issue_check",
            disabled=not active_ids,
        ):
            for key in active_ids:
                feed = ISSUE_FEEDS[key]
                added = check_issue_feed(feed, watches[key])
                if added is not None:
                    st.caption(f"{feed['label']}: {added} new alerts.")

        for key in active_ids:
            watch = watches[key]
            label = ISSUE_FEEDS[key]["label"]
            unread = watch["unread"]

            with st.expander(f"{label} · {len(unread)} unread"):
                if st.button("Unfollow", key=f"configured_unfollow:{key}"):
                    del watches[key]
                    st.rerun()

                if not unread:
                    st.caption("No unread alerts.")
                elif st.button("Mark as read", key=f"configured_read:{key}"):
                    unread.clear()
                    st.rerun()

                for event, title, url in reversed(unread):
                    st.text(f"{event} · {title}")
                    st.link_button("Open source", url)


render_issue_alerts()

# ============================================================
# SIDEBAR SETTINGS
# ============================================================

with st.sidebar:
    st.header("Search settings")

    default_index = str(
        Path("./retrieval_index")
    )

    index_dir = st.text_input(
        "Retrieval index directory",
        value=default_index,
        help=(
            "Folder containing chunks.jsonl and faiss.index. "
            "Relative paths are resolved from the folder where "
            "Streamlit is started."
        ),
    )

    top_k = st.slider(
        "Retrieved evidence",
        min_value=3,
        max_value=15,
        value=DEFAULT_TOP_K,
        step=1,
    )

    min_score = st.slider(
        "Minimum semantic score",
        min_value=0.0,
        max_value=1.0,
        value=float(
            DEFAULT_MIN_TOP_SEMANTIC_SCORE
        ),
        step=0.01,
    )

    with st.expander(
        "Answer length"
    ):
        max_sources = st.slider(
            "Maximum sources",
            3,
            12,
            DEFAULT_MAX_SYNTHESIS_SOURCES,
        )

        max_sentences = st.slider(
            "Sentences per source",
            1,
            5,
            DEFAULT_MAX_SENTENCES_PER_SOURCE,
        )

        max_timeline_events = st.slider(
            "Maximum timeline events",
            4,
            15,
            DEFAULT_MAX_TIMELINE_EVENTS,
        )

    enable_log = st.checkbox(
        "Save query log",
        value=True,
        help=(
            "Writes agent_query_log.jsonl inside the "
            "retrieval-index directory."
        ),
    )

    show_debug = st.checkbox(
        "Show retrieved evidence",
        value=False,
    )

    if st.button(
        "Clear conversation",
        use_container_width=True,
    ):
        st.session_state.messages = []
        st.rerun()


# ============================================================
# LOAD INDEX
# ============================================================

is_valid, path_error = valid_index_dir(
    index_dir
)

if not is_valid:
    st.warning(path_error)
    st.markdown(
        """
Place the index artifacts in a folder such as:

```text
retrieval_index/
├── chunks.jsonl
├── faiss.index
├── chunks.csv
├── embeddings.npy
└── index_metadata.json
```

Then enter `./retrieval_index` in the sidebar.
"""
    )
    st.stop()

try:
    model, faiss_index, chunks = (
        load_cached_retrieval_system(
            str(
                Path(index_dir)
                .expanduser()
                .resolve()
            ),
            DEFAULT_EMBEDDING_MODEL,
        )
    )

except Exception as exc:
    st.error(
        f"Could not load the retrieval index: "
        f"{type(exc).__name__}: {exc}"
    )
    st.stop()

st.success(
    f"Index ready · {len(chunks):,} chunks",
    icon="✅",
)


# ============================================================
# CONVERSATION
# ============================================================

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(
        message["role"]
    ):
        st.markdown(
            message["content"]
        )

        if (
            message["role"] == "assistant"
            and show_debug
            and message.get("evidence")
        ):
            show_evidence(
                message["evidence"]
            )


question = st.chat_input(
    "Ask a question about the indexed civic records"
)

if question:
    st.session_state.messages.append({
        "role": "user",
        "content": question,
    })

    with st.chat_message("user"):
        st.markdown(question)

    settings = build_settings(
        index_dir=index_dir,
        top_k=top_k,
        min_score=min_score,
        max_sources=max_sources,
        max_sentences=max_sentences,
        max_timeline_events=max_timeline_events,
        enable_log=enable_log,
    )

    with st.chat_message("assistant"):
        try:
            with st.spinner(
                "Searching the corpus..."
            ):
                answer, intent, evidence = (
                    answer_with_evidence(
                        question,
                        model,
                        faiss_index,
                        chunks,
                        settings,
                    )
                )

            st.markdown(answer)

            st.caption(
                f"Detected query type: {intent}"
            )

            if show_debug:
                st.subheader(
                    "Retrieved evidence"
                )
                show_evidence(
                    evidence
                )

            st.session_state.messages.append({
                "role": "assistant",
                "content": answer,
                "intent": intent,
                "evidence": evidence,
            })

        except Exception as exc:
            error_message = (
                f"Search failed: "
                f"{type(exc).__name__}: {exc}"
            )

            st.error(error_message)

            st.session_state.messages.append({
                "role": "assistant",
                "content": error_message,
                "evidence": [],
            })
