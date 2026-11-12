import io
import os
import re
import hmac
import hashlib
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from google import genai
from google.genai.types import EmbedContentConfig
from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from pypdf import PdfReader
from apollo_v12 import file_type, extract_text_document, keyword_rank, fuse

PROJECT_ID = os.getenv("PROJECT_ID", "harmonia-dev-20260204")
DATABASE_ID = os.getenv("DATABASE_ID", "(default)")
LOCATION = os.getenv("LOCATION", "us-central1")
DOCUMENT_COLLECTION = os.getenv("DOCUMENT_COLLECTION", "apollo_documents")
CHUNK_COLLECTION = os.getenv("CHUNK_COLLECTION", "apollo_chunks")
DOCUMENT_BUCKET = os.getenv("APOLLO_DOCUMENT_BUCKET", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "8"))
MAX_VECTOR_DISTANCE = float(os.getenv("MAX_VECTOR_DISTANCE", "0.60"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "250"))
MAX_CHUNKS = int(os.getenv("MAX_CHUNKS", "400"))
MAX_KEYWORD_SCAN = int(os.getenv("MAX_KEYWORD_SCAN", "5000"))
MAX_PDF_BYTES = int(os.getenv("MAX_PDF_BYTES", str(20 * 1024 * 1024)))
WRITE_KEY = os.getenv("APOLLO_WRITE_KEY", "")
WRITE_KEY_HEADER = "X-Apollo-Write-Key"

app = Flask(__name__)


def stop_response(error_code, error_message, stop_reason, http_status, stage=None):
    body = {
        "decision": "STOP",
        "error_code": error_code,
        "error_message": error_message,
        "stop_reason": stop_reason,
    }
    if stage:
        body["stage"] = stage
    return jsonify(body), http_status


def require_write_access():
    if not WRITE_KEY:
        return stop_response(
            "WRITE_ACCESS_NOT_CONFIGURED",
            "Apollo write access is not configured.",
            "Write access is unavailable.",
            503,
            "authorization",
        )
    supplied = request.headers.get(WRITE_KEY_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, WRITE_KEY):
        return stop_response(
            "WRITE_ACCESS_DENIED",
            "A valid Apollo write key is required.",
            "Write access denied.",
            403,
            "authorization",
        )
    return None


def validate_identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        return f"A non-empty {name} is required."
    value = value.strip()
    if len(value) > 200:
        return f"{name} must be 200 characters or fewer."
    if "/" in value:
        return f"{name} must not contain '/'."
    return None


def safe_filename(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "document.pdf")
    return value[:180] or "document.pdf"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_clients():
    genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
    db = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    storage_client = storage.Client(project=PROJECT_ID) if DOCUMENT_BUCKET else None
    return genai_client, db, storage_client


def extract_pdf(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("Encrypted PDF is not supported.") from exc

    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append({"page_number": page_number, "text": text})
    if not pages:
        raise ValueError("No extractable text was found in the PDF.")
    return pages


def split_text(text, size, overlap):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("Invalid chunking configuration.")

    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = text.rfind(" ", start + max(1, size // 2), end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def build_chunks(product_id, document_id, document_name, pages):
    chunks = []
    ordinal = 0
    for page in pages:
        for text in split_text(page["text"], CHUNK_SIZE, CHUNK_OVERLAP):
            ordinal += 1
            chunk_id = hashlib.sha256(f"{product_id}\0{document_id}\0{ordinal}".encode()).hexdigest()
            chunks.append({
                "chunk_id": chunk_id,
                "product_id": product_id,
                "document_id": document_id,
                "document_name": document_name,
                "page_number": page["page_number"],
                "ordinal": ordinal,
                "content": text,
                "searchable_text": text,
            })
            if len(chunks) > MAX_CHUNKS:
                raise ValueError(f"Document exceeds configured MAX_CHUNKS={MAX_CHUNKS}.")
    if not chunks:
        raise ValueError("Document processing produced no searchable chunks.")
    return chunks


def embed_text(genai_client, text, task_type):
    response = genai_client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBEDDING_DIMENSIONS,
        ),
    )
    embedding = response.embeddings[0].values
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Generated embedding has dimension {len(embedding)}; expected {EMBEDDING_DIMENSIONS}."
        )
    return embedding


def source_blob_name(product_id, document_id, document_name):
    return f"{product_id}/{document_id}/{safe_filename(document_name)}"


def upload_source(storage_client, pdf_bytes, product_id, document_id, document_name, content_type="application/pdf"):
    if not storage_client or not DOCUMENT_BUCKET:
        return None
    blob_name = source_blob_name(product_id, document_id, document_name) + "." + uuid.uuid4().hex
    bucket = storage_client.bucket(DOCUMENT_BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(pdf_bytes, content_type=content_type)
    return f"gs://{DOCUMENT_BUCKET}/{blob_name}"


def delete_source(storage_client, source_uri):
    if not storage_client or not source_uri or not source_uri.startswith("gs://"):
        return
    _, path = source_uri.split("gs://", 1)
    bucket_name, blob_name = path.split("/", 1)
    storage_client.bucket(bucket_name).blob(blob_name).delete(if_generation_match=None)


def document_key(product_id, document_id):
    return f"{product_id}__{document_id}"


def get_document(db, product_id, document_id):
    ref = db.collection(DOCUMENT_COLLECTION).document(document_key(product_id, document_id))
    return ref, ref.get()


def document_response(snapshot, data, status=200):
    return jsonify({
        "decision": "PASS",
        "product_id": data.get("product_id"),
        "document_id": data.get("document_id"),
        "document_name": data.get("document_name"),
        "document_type": data.get("document_type"),
        "metadata": data.get("metadata", {}),
        "processing": data.get("processing", {}),
        "traceability": data.get("traceability", {}),
        "source_uri": data.get("source_uri"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }), status


def delete_chunks(db, product_id, document_id):
    query = (
        db.collection(CHUNK_COLLECTION)
        .where(filter=firestore.FieldFilter("product_id", "==", product_id))
        .where(filter=firestore.FieldFilter("document_id", "==", document_id))
    )
    docs = list(query.stream())
    for start in range(0, len(docs), 450):
        batch = db.batch()
        for snap in docs[start:start + 450]:
            batch.delete(snap.reference)
        batch.commit()
    return len(docs)


def persist_document(db, doc_ref, record, chunks):
    # Replace derived chunk state only after all extraction/embedding work has succeeded.
    delete_chunks(db, record["product_id"], record["document_id"])
    for start in range(0, len(chunks), 400):
        batch = db.batch()
        for chunk in chunks[start:start + 400]:
            ref = db.collection(CHUNK_COLLECTION).document(chunk["chunk_id"])
            batch.set(ref, chunk)
        batch.commit()
    doc_ref.set(record)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "Apollo Vector Database Service",
        "version": "1.2",
        "project": PROJECT_ID,
        "database": DATABASE_ID,
        "document_collection": DOCUMENT_COLLECTION,
        "chunk_collection": CHUNK_COLLECTION,
        "document_bucket": DOCUMENT_BUCKET or None,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "capabilities": ["ingest", "read", "update", "delete", "semantic_search", "keyword_search", "hybrid_search"],
        "write_access": "key_required" if WRITE_KEY else "not_configured",
    })


@app.post("/documents")
def ingest_document():
    access_error = require_write_access()
    if access_error:
        return access_error

    product_id = (request.form.get("product_id") or "").strip()
    document_id = (request.form.get("document_id") or "").strip() or uuid.uuid4().hex
    product_error = validate_identifier(product_id, "product_id")
    document_error = validate_identifier(document_id, "document_id")
    if product_error or document_error:
        return stop_response(
            "INVALID_REQUEST", product_error or document_error,
            "Invalid product/document identifier.", 400, "validation"
        )

    uploaded = request.files.get("document")
    if not uploaded or not uploaded.filename:
        return stop_response(
            "INVALID_REQUEST", "A PDF, JSON, or Markdown document is required.",
            "Missing document.", 400, "validation"
        )
    try:
        document_type = file_type(uploaded.filename)
    except ValueError as exc:
        return stop_response("UNSUPPORTED_DOCUMENT", str(exc), "Unsupported document type.", 415, "validation")

    pdf_bytes = uploaded.read(MAX_PDF_BYTES + 1)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return stop_response(
            "DOCUMENT_TOO_LARGE", f"Document exceeds {MAX_PDF_BYTES} bytes.",
            "Document exceeds configured size limit.", 413, "validation"
        )

    try:
        genai_client, db, storage_client = get_clients()
        doc_ref, existing = get_document(db, product_id, document_id)
        if existing.exists:
            return stop_response(
                "DOCUMENT_ALREADY_EXISTS",
                f"Document '{document_id}' already exists in product '{product_id}'.",
                "Create would overwrite an existing document.", 409, "validation"
            )

        pages = extract_pdf(pdf_bytes) if document_type == "pdf" else extract_text_document(pdf_bytes, document_type)
        chunks = build_chunks(product_id, document_id, uploaded.filename, pages)
        for chunk in chunks:
            chunk["embedding"] = Vector(embed_text(genai_client, chunk["searchable_text"], "RETRIEVAL_DOCUMENT"))
            chunk["embedding_model"] = EMBEDDING_MODEL
            chunk["embedding_dimensions"] = EMBEDDING_DIMENSIONS

        source_uri = upload_source(storage_client, pdf_bytes, product_id, document_id, uploaded.filename, {"pdf":"application/pdf", "json":"application/json", "markdown":"text/markdown"}[document_type])
        sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        now = utc_now_iso()
        metadata = {k: v for k, v in request.form.items() if k not in {"product_id", "document_id"}}
        record = {
            "product_id": product_id,
            "document_id": document_id,
            "document_name": uploaded.filename,
            "document_type": document_type,
            "metadata": metadata,
            "source_uri": source_uri,
            "source_sha256": sha256,
            "processing": {
                "page_count": len(pages),
                "chunk_count": len(chunks),
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
            },
            "traceability": {
                "product_id": product_id,
                "document_id": document_id,
                "document_name": uploaded.filename,
            },
            "created_at": now,
            "updated_at": now,
        }
        persist_document(db, doc_ref, record, chunks)
        snapshot = doc_ref.get()
    except ValueError as exc:
        app.logger.exception("Apollo ingestion validation/processing failure")
        return stop_response("DOCUMENT_PROCESSING_FAILURE", str(exc), "Document processing failed.", 422, "processing")
    except Exception as exc:
        app.logger.exception("Apollo ingestion failure")
        return stop_response("INGEST_FAILURE", str(exc), "Document ingestion failed.", 500, "ingestion")

    return document_response(snapshot, snapshot.to_dict(), 201)


@app.get("/products/<product_id>/documents/<document_id>/content")
def read_document_content(product_id, document_id):
    """Authenticated, integrity-checked exact read of a stored Markdown/JSON source."""
    access_error = require_write_access()
    if access_error:
        return access_error
    error = validate_identifier(product_id, "product_id") or validate_identifier(document_id, "document_id")
    if error:
        return stop_response("INVALID_REQUEST", error, "Invalid document identity.", 400, "validation")
    try:
        _, db, storage_client = get_clients()
        _, snapshot = get_document(db, product_id, document_id)
        if not snapshot.exists:
            return stop_response("DOCUMENT_NOT_FOUND", "Document not found.", "Document unavailable.", 404, "retrieval")
        record = snapshot.to_dict()
        if record.get("product_id") != product_id or record.get("document_id") != document_id:
            return stop_response("DOCUMENT_SCOPE_MISMATCH", "Document scope mismatch.", "Document unavailable.", 409, "retrieval")
        if record.get("document_type") not in ("markdown", "json"):
            return stop_response("UNSUPPORTED_CONTENT_TYPE", "Only Markdown and JSON are supported.", "Unsupported content type.", 415, "retrieval")
        uri = record.get("source_uri")
        prefix = f"gs://{DOCUMENT_BUCKET}/{product_id}/{document_id}/"
        if not DOCUMENT_BUCKET or not storage_client or not isinstance(uri, str) or not uri.startswith(prefix):
            return stop_response("INVALID_SOURCE_URI", "Invalid source reference.", "Document unavailable.", 409, "retrieval")
        blob_name = uri[len(f"gs://{DOCUMENT_BUCKET}/"):]
        raw = storage_client.bucket(DOCUMENT_BUCKET).blob(blob_name).download_as_bytes()
        if len(raw) > MAX_PDF_BYTES:
            return stop_response("CONTENT_TOO_LARGE", "Document exceeds configured limit.", "Content too large.", 413, "retrieval")
        expected = record.get("source_sha256")
        if not isinstance(expected, str) or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
            return stop_response("SOURCE_INTEGRITY_FAILURE", "Source checksum mismatch.", "Document integrity failure.", 409, "retrieval")
        return jsonify({"decision": "PASS", "product_id": product_id, "document_id": document_id, "document_type": record["document_type"], "sha256": expected, "content": raw.decode("utf-8")})
    except UnicodeDecodeError:
        return stop_response("INVALID_ENCODING", "Source is not UTF-8.", "Invalid document encoding.", 422, "retrieval")
    except Exception:
        app.logger.exception("Apollo exact-content retrieval failure")
        return stop_response("RETRIEVAL_FAILURE", "Exact-content read failed.", "Document retrieval failed.", 500, "retrieval")


@app.get("/products/<product_id>/documents/<document_id>")
def read_document(product_id, document_id):
    product_error = validate_identifier(product_id, "product_id")
    document_error = validate_identifier(document_id, "document_id")
    if product_error or document_error:
        return stop_response("INVALID_REQUEST", product_error or document_error, "Invalid request.", 400, "validation")
    try:
        _, db, _ = get_clients()
        _, snapshot = get_document(db, product_id, document_id)
    except Exception as exc:
        app.logger.exception("Apollo document retrieval failure")
        return stop_response("RETRIEVAL_FAILURE", str(exc), "Document retrieval failed.", 500, "retrieval")
    if not snapshot.exists:
        return stop_response(
            "DOCUMENT_NOT_FOUND",
            f"Document '{document_id}' was not found in product '{product_id}'.",
            "Document does not exist in the requested product scope.", 404, "retrieval"
        )
    return document_response(snapshot, snapshot.to_dict(), 200)


@app.put("/products/<product_id>/documents/<document_id>")
def update_document(product_id, document_id):
    access_error = require_write_access()
    if access_error:
        return access_error

    product_error = validate_identifier(product_id, "product_id")
    document_error = validate_identifier(document_id, "document_id")
    if product_error or document_error:
        return stop_response("INVALID_REQUEST", product_error or document_error, "Invalid request.", 400, "validation")

    uploaded = request.files.get("document")
    if not uploaded or not uploaded.filename:
        return stop_response("UNSUPPORTED_DOCUMENT", "A replacement document is required.", "Missing document.", 415, "validation")
    try:
        document_type = file_type(uploaded.filename)
    except ValueError as exc:
        return stop_response("UNSUPPORTED_DOCUMENT", str(exc), "Unsupported document type.", 415, "validation")
    pdf_bytes = uploaded.read(MAX_PDF_BYTES + 1)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return stop_response("DOCUMENT_TOO_LARGE", "Replacement document exceeds configured size limit.", "Document too large.", 413, "validation")

    try:
        genai_client, db, storage_client = get_clients()
        doc_ref, existing = get_document(db, product_id, document_id)
        if not existing.exists:
            return stop_response(
                "DOCUMENT_NOT_FOUND",
                f"Document '{document_id}' was not found in product '{product_id}'.",
                "Update target does not exist.", 404, "validation"
            )
        old = existing.to_dict()
        pages = extract_pdf(pdf_bytes) if document_type == "pdf" else extract_text_document(pdf_bytes, document_type)
        chunks = build_chunks(product_id, document_id, uploaded.filename, pages)
        for chunk in chunks:
            chunk["embedding"] = Vector(embed_text(genai_client, chunk["searchable_text"], "RETRIEVAL_DOCUMENT"))
            chunk["embedding_model"] = EMBEDDING_MODEL
            chunk["embedding_dimensions"] = EMBEDDING_DIMENSIONS

        new_source_uri = upload_source(storage_client, pdf_bytes, product_id, document_id, uploaded.filename, {"pdf":"application/pdf", "json":"application/json", "markdown":"text/markdown"}[document_type])
        metadata = old.get("metadata", {}).copy()
        metadata.update({k: v for k, v in request.form.items() if k not in {"product_id", "document_id"}})
        record = {
            **old,
            "product_id": product_id,
            "document_id": document_id,
            "document_name": uploaded.filename,
            "document_type": document_type,
            "metadata": metadata,
            "source_uri": new_source_uri,
            "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "processing": {
                "page_count": len(pages),
                "chunk_count": len(chunks),
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
            },
            "traceability": {
                "product_id": product_id,
                "document_id": document_id,
                "document_name": uploaded.filename,
            },
            "updated_at": utc_now_iso(),
        }
        persist_document(db, doc_ref, record, chunks)
        if old.get("source_uri") and old.get("source_uri") != new_source_uri:
            try:
                delete_source(storage_client, old.get("source_uri"))
            except Exception:
                app.logger.exception("Old Apollo source cleanup failed after successful update")
        snapshot = doc_ref.get()
    except ValueError as exc:
        return stop_response("DOCUMENT_PROCESSING_FAILURE", str(exc), "Document processing failed.", 422, "processing")
    except Exception as exc:
        app.logger.exception("Apollo update failure")
        return stop_response("UPDATE_FAILURE", str(exc), "Document update failed.", 500, "update")
    return document_response(snapshot, snapshot.to_dict(), 200)


@app.delete("/products/<product_id>/documents/<document_id>")
def delete_document(product_id, document_id):
    access_error = require_write_access()
    if access_error:
        return access_error
    try:
        _, db, storage_client = get_clients()
        doc_ref, existing = get_document(db, product_id, document_id)
        if not existing.exists:
            return stop_response(
                "DOCUMENT_NOT_FOUND",
                f"Document '{document_id}' was not found in product '{product_id}'.",
                "Delete target does not exist.", 404, "validation"
            )
        data = existing.to_dict()
        removed_chunks = delete_chunks(db, product_id, document_id)
        doc_ref.delete()
        try:
            delete_source(storage_client, data.get("source_uri"))
        except Exception:
            app.logger.exception("Apollo source cleanup failure")
            return stop_response(
                "DELETE_PARTIAL_FAILURE",
                "Document metadata/vector state was deleted, but source-file cleanup failed.",
                "Deletion was not fully completed.", 500, "source_cleanup"
            )
    except Exception as exc:
        app.logger.exception("Apollo delete failure")
        return stop_response("DELETE_FAILURE", str(exc), "Document deletion failed.", 500, "delete")
    return jsonify({
        "decision": "PASS",
        "product_id": product_id,
        "document_id": document_id,
        "deleted": True,
        "deleted_chunk_count": removed_chunks,
    }), 200


@app.post("/search")
def semantic_search():
    payload = request.get_json(silent=True) or {}
    product_id = payload.get("product_id")
    user_request = payload.get("user_request")
    product_error = validate_identifier(product_id, "product_id")
    if product_error or not isinstance(user_request, str) or not user_request.strip():
        return stop_response(
            "INVALID_REQUEST",
            product_error or "A non-empty user_request is required.",
            "Invalid semantic-search request.", 400, "validation"
        )

    product_id = product_id.strip()
    user_request = user_request.strip()

    try:
        genai_client, db, _ = get_clients()
        mode = payload.get("mode", "hybrid")
        if mode not in ("hybrid", "keyword", "semantic"):
            return stop_response("INVALID_REQUEST", "mode must be hybrid, keyword, or semantic.", "Invalid search mode.", 400, "validation")
        base_query = db.collection(CHUNK_COLLECTION).where(
            filter=firestore.FieldFilter("product_id", "==", product_id)
        )
        lexical_results = []
        if mode in ("hybrid", "keyword"):
            scoped_chunks = []
            for snap in base_query.limit(MAX_KEYWORD_SCAN + 1).stream():
                scoped_chunks.append(snap.to_dict())
            if len(scoped_chunks) > MAX_KEYWORD_SCAN:
                return stop_response("KEYWORD_SCAN_LIMIT", "Product exceeds configured keyword scan capacity.", "Cannot guarantee complete keyword retrieval.", 422, "retrieval")
            lexical_results = keyword_rank(scoped_chunks, user_request, SEARCH_LIMIT)
        raw_results = []
        if mode in ("hybrid", "semantic"):
            query_embedding = embed_text(genai_client, user_request, "RETRIEVAL_QUERY")
            vector_query = base_query.find_nearest(
                vector_field="embedding",
                query_vector=Vector(query_embedding),
                distance_measure=DistanceMeasure.COSINE,
                limit=SEARCH_LIMIT,
                distance_result_field="vector_distance",
            )
            raw_results = [snap.to_dict() for snap in vector_query.stream()]
        qualifying = fuse(lexical_results, raw_results, SEARCH_LIMIT, MAX_VECTOR_DISTANCE)

    except Exception as exc:
        app.logger.exception("Apollo semantic retrieval failure")
        return stop_response(
            "RETRIEVAL_FAILURE",
            str(exc),
            "Vector search failed.",
            500,
            "retrieval",
        )

    if not qualifying:
        return stop_response("NO_SUPPORTED_MATCH", "No keyword or semantic candidate met the acceptance rules.", "No supported result.", 404, "candidate_evaluation")

    results = []
    for rank, item in enumerate(qualifying, start=1):
        results.append({
            "rank": rank,
            "product_id": product_id,
            "document_id": item.get("document_id"),
            "document_name": item.get("document_name"),
            "chunk_id": item.get("chunk_id"),
            "page_number": item.get("page_number"),
            "content": item.get("content"),
            "vector_distance": item.get("vector_distance"),
            "keyword_score": item.get("keyword_score"),
            "hybrid_score": item.get("hybrid_score"),
            "retrieval_methods": item.get("retrieval_methods"),
            "metadata": {
                "embedding_model": item.get("embedding_model"),
                "embedding_dimensions": item.get("embedding_dimensions"),
            },
            "traceability": {
                "product_id": product_id,
                "document_id": item.get("document_id"),
                "document_name": item.get("document_name"),
                "chunk_id": item.get("chunk_id"),
                "page_number": item.get("page_number"),
            },
        })

    return jsonify({
        "decision": "PASS",
        "product_id": product_id,
        "result_count": len(results),
        "results": results,
        "retrieval": {
            "ranking": "reciprocal_rank_fusion",
            "mode": mode,
            "document_count_agnostic": True,
            "max_results": SEARCH_LIMIT,
            "max_vector_distance": MAX_VECTOR_DISTANCE,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
        },
    }), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
