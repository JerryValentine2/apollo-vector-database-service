"""Apollo v1.2 extraction and deterministic lexical retrieval helpers."""
import json
import re
from collections import Counter

SUPPORTED = {'.pdf': 'pdf', '.json': 'json', '.md': 'markdown'}

def file_type(filename):
    name = (filename or '').lower()
    for extension, kind in SUPPORTED.items():
        if name.endswith(extension):
            return kind
    raise ValueError('Supported document types: PDF, JSON, Markdown.')

def extract_text_document(data, kind):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Text document must be UTF-8 encoded.') from exc
    if kind == 'json':
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f'Invalid JSON: {exc.msg}.') from exc
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    if not text.strip():
        raise ValueError('Document contains no searchable text.')
    return [{'page_number': 1, 'text': text}]

def tokenize(text):
    return re.findall(r'[^\W_]+', (text or '').casefold(), re.UNICODE)

def keyword_rank(chunks, query, limit):
    terms = Counter(tokenize(query))
    if not terms:
        return []
    results = []
    for chunk in chunks:
        tokens = Counter(tokenize(chunk.get('searchable_text') or chunk.get('content') or ''))
        hits = sum(min(tokens.get(term, 0), count) for term, count in terms.items())
        if hits:
            results.append((hits / sum(terms.values()), hits, chunk))
    results.sort(key=lambda item: (-item[0], -item[1], str(item[2].get('chunk_id', ''))))
    return [dict(item[2], keyword_score=item[0]) for item in results[:limit]]

def fuse(keyword_results, vector_results, limit, max_distance):
    combined = {}
    for method, candidates in (('keyword', keyword_results), ('semantic', vector_results)):
        for rank, item in enumerate(candidates, 1):
            if method == 'semantic':
                distance = item.get('vector_distance')
                if not isinstance(distance, (int, float)) or distance > max_distance:
                    continue
            key = (item.get('document_id'), item.get('chunk_id'))
            if key not in combined:
                combined[key] = dict(item, retrieval_methods=[], hybrid_score=0.0)
            entry = combined[key]
            entry['hybrid_score'] += 1.0 / (60 + rank)
            entry['retrieval_methods'].append(method)
            for field in ('vector_distance', 'keyword_score'):
                if field in item:
                    entry[field] = item[field]
    return sorted(combined.values(), key=lambda item: (-item['hybrid_score'], str(item.get('chunk_id', ''))))[:limit]
