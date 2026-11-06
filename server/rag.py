"""RAG (retrieval-augmented generation) over a document drop-folder,
built on TF-IDF rather than neural embeddings — deliberate choice, not a
compromise nobody would notice: this server's Python (3.8.10, EOL) can't
get a compatible onnxruntime for fastembed, and full PyTorch (via
sentence-transformers) is heavier than this needs. For a small, focused
corpus (a handful of documents, not millions), TF-IDF keyword/lexical
matching is genuinely adequate — the tradeoff is weaker semantic/
paraphrase matching, not "worse in general."

Mirrors the existing inbox/ drop-folder pattern from file_bridge.py:
knowledge/ on the Mac is where the user adds documents; they get synced
up here (see jarvis_client.py's sync_knowledge_base / always_on.py's
"update my knowledge base" command) and indexed. Answers should be
GROUNDED in retrieved chunks, same anti-fabrication principle as
answer_lookup()'s real task-log grounding — never let a query with zero
real matches produce an invented-sounding answer.
"""
import pickle
import re
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

WORKSPACE_DIR = Path.home() / "jarvis-workspace"
KNOWLEDGE_DIR = WORKSPACE_DIR / "knowledge"
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = WORKSPACE_DIR / "rag_index.pkl"

# Character-based chunking, not token-based — simple, no extra
# tokenizer dependency needed, and "close enough" at this corpus scale.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# Below this cosine-similarity score, a "match" is really just shared
# stopwords/noise, not a real answer to the query — returning it would
# be presenting a non-answer as if it were grounded, exactly the
# fabrication risk RAG is supposed to prevent, not introduce.
MIN_RELEVANCE_SCORE = 0.05


def _read_text_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return path.read_text(errors="ignore")


def _fixed_size_chunks(text: str):
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return chunks


# Only level-2 ("## ") headers are section boundaries — the level-1
# ("# ") title line is document metadata, not a retrievable section.
# First version split on ALL header levels, which put the bare title
# line in its own chunk — and that chunk then out-ranked the ACTUAL
# answer for a real query ("what RAM does the raspberry pi 4 have"),
# because the title chunk contained the exact product name and nothing
# else, while the real answer chunk ("## Processor & Memory") never
# repeats "Raspberry Pi 4" anywhere in its own text. Verified live with
# real score numbers: title chunk 0.165, real answer chunk 0.043.
_MD_SECTION_RE = re.compile(r"^##\s+.+$", re.MULTILINE)


def _chunk_text(text: str, source: str):
    """Splits on markdown ## section headers when present, falling back
    to fixed-size chunking otherwise (plain text, PDFs). The doc title
    (first line, usually "# Title") gets PREPENDED to every section
    chunk rather than living in its own chunk — this is what actually
    fixed the retrieval bug above: every chunk now carries the
    identifying context ("Raspberry Pi 4 Model B...") alongside its own
    specific content, instead of only the title-only chunk having it.
    Still caps oversized sections at CHUNK_SIZE (fixed-size chunking
    within a section) so one huge section can't dominate the index.
    """
    text = text.strip()
    sections = list(_MD_SECTION_RE.finditer(text))
    if len(sections) < 2:
        return [{"text": c, "source": source} for c in _fixed_size_chunks(text)]

    doc_title = text.split("\n", 1)[0].lstrip("#").strip()

    chunks = []
    # Content between the title line and the FIRST "## " header (an
    # intro paragraph, common in real docs) used to fall through the
    # cracks entirely: the loop below only ever starts at a header's own
    # position, so that text was never placed into any chunk at all —
    # silently unsearchable forever. Same doc_title-prepended treatment
    # as every other chunk.
    first_newline = text.find("\n")
    title_end = first_newline if first_newline != -1 else len(text)
    intro = text[title_end:sections[0].start()].strip()
    if intro:
        chunks.append({"text": f"{doc_title}\n\n{intro}", "source": source})
    for i, h in enumerate(sections):
        start = h.start()
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        section = text[start:end].strip()
        if not section:
            continue
        bodies = [section] if len(section) <= CHUNK_SIZE * 1.5 else _fixed_size_chunks(section)
        for body in bodies:
            chunks.append({"text": f"{doc_title}\n\n{body}", "source": source})
    return chunks


def build_index() -> int:
    """Rebuilds the index from scratch against whatever's currently in
    KNOWLEDGE_DIR. Returns the chunk count (0 if the folder is empty —
    callers should treat that as "nothing to index yet", not an error)."""
    all_chunks = []
    for path in sorted(KNOWLEDGE_DIR.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in (".md", ".txt", ".pdf"):
            continue
        try:
            text = _read_text_file(path)
        except Exception:
            continue  # skip unreadable files rather than failing the whole rebuild
        all_chunks.extend(_chunk_text(text, path.name))

    if not all_chunks:
        # Used to unconditionally delete the existing index here — but a
        # zero-chunk result can also mean every file was transiently
        # unreadable (disk hiccup, mid-sync race) rather than a genuine
        # "the user deleted everything." Deleting a working index on a
        # transient failure permanently wipes the knowledge base until
        # the next successful rebuild; leaving it untouched costs
        # nothing and only matters in the rare case this ever fires.
        return 0

    texts = [c["text"] for c in all_chunks]
    # Bigrams matter here: unigram-only TF-IDF treats "raspberry" and
    # "pi" as independent tokens, so a query naming a specific product
    # ("raspberry pi 4") doesn't get credit for the exact phrase — a real
    # contributor to the wrong-chunk-ranked-higher bug found in testing.
    # sublinear_tf: log-scales term frequency (1+log(tf)) instead of raw
    # counts — reduces the advantage a chunk gets purely from repeating a
    # word several times (verified case: a chunk about --mlock mentions
    # "RAM" three times in a swap-prevention context and was outranking
    # the actual RAM-capacity spec, which mentions it meaningfully once).
    # Helps, doesn't fully solve it — see retrieve()'s docstring for the
    # honest limitation this doesn't fix.
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
    matrix = vectorizer.fit_transform(texts)

    # Atomic write: a crash/kill/OOM mid pickle.dump used to leave a
    # truncated, permanently corrupt INDEX_PATH (retrieve() would then
    # raise on every future query). Write to a temp file and rename —
    # rename is atomic, so readers only ever see the old complete file
    # or the new complete file, never a partial one.
    tmp_path = INDEX_PATH.with_suffix(".pkl.tmp")
    with open(tmp_path, "wb") as f:
        pickle.dump({"chunks": all_chunks, "vectorizer": vectorizer, "matrix": matrix}, f)
    tmp_path.replace(INDEX_PATH)

    return len(all_chunks)


def retrieve(query: str, k: int = 4):
    """Returns up to k relevant chunks as [{"text", "source", "score"}, ...],
    filtered to MIN_RELEVANCE_SCORE — an empty list means "nothing in the
    knowledge base is actually relevant to this," which the caller should
    say plainly rather than answering from general knowledge and implying
    it came from the documents.

    Honest known limitation, not silently glossed over: this is TF-IDF
    (lexical/keyword matching), not neural embeddings — a deliberate
    tradeoff for this server's old Python (see module docstring). It can
    rank a chunk that repeats a query word several times in an unrelated
    sense above a chunk that uses it once in the actually-relevant sense
    — verified live with a real query where a "RAM" mention in a
    swap-prevention context outranked the actual RAM-capacity spec.
    k=4 (up from 3) and sublinear_tf partly mitigate this by giving the
    synthesizing model more candidates to sift through rather than
    trusting rank #1 alone, but this is a real, not fully solved,
    accuracy ceiling of the current approach — worth knowing before
    trusting an answer that seems oddly specific or slightly off."""
    if not INDEX_PATH.exists():
        return []
    # A corrupted index (crash mid-write, before the atomic-rename fix
    # above existed, or just disk corruption) or any sklearn error used
    # to raise straight up through answer_from_knowledge and into the
    # caller, uncaught — a knowledge-base query crashing instead of
    # giving a clean "couldn't search" is a worse failure than treating
    # it the same as "nothing relevant found," which the caller already
    # handles gracefully.
    try:
        with open(INDEX_PATH, "rb") as f:
            data = pickle.load(f)

        query_vec = data["vectorizer"].transform([query])
        sims = cosine_similarity(query_vec, data["matrix"])[0]
        top_idx = sims.argsort()[::-1][:k]

        results = []
        for i in top_idx:
            score = float(sims[i])
            if score >= MIN_RELEVANCE_SCORE:
                c = data["chunks"][i]
                results.append({"text": c["text"], "source": c["source"], "score": score})
        return results
    except Exception:
        return []


def answer_from_knowledge(query: str) -> str:
    """Full RAG turn: retrieve real chunks, ground an answer in them via
    the fast tier (same grounding pattern as answer_lookup — real data
    injected as context, explicit instruction not to add beyond it)."""
    import jarvis_respond as jr

    chunks = retrieve(query)
    if not chunks:
        return "Nothing in the knowledge base looks relevant to that, sir — it may not be something I have documents on yet."

    context = "\n\n".join(f"[from {c['source']}]\n{c['text']}" for c in chunks)
    grounding = (
        f"Here are the real, most relevant excerpts retrieved from the "
        f"knowledge base for this query — this is the only source "
        f"material that exists, never add anything beyond what these "
        f"excerpts actually say:\n\n{context}"
    )
    result = jr.ask_groq(query, context_text=grounding)
    # ask_groq can return "" on a 200 response with empty content (not
    # None) — that used to read as success here and silently hand back
    # an empty answer instead of this fallback message.
    if result:
        return result
    return "I retrieved relevant material but couldn't reach the model to synthesize an answer, sir."


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        n = build_index()
        print(f"Indexed {n} chunks from {KNOWLEDGE_DIR}")
    else:
        query = " ".join(sys.argv[1:]) or "what is this"
        print(answer_from_knowledge(query))
