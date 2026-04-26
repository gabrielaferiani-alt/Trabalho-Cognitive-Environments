"""
Trabalho Final — Serviços Cognitivos em Cloud (FIAP MBA)
RAG Application: Base de Conhecimento sobre TEA

Endpoints:
  POST /api/ingest  → texto ou PDF base64 → chunking recursivo → embeddings → AI Search
  POST /api/query   → pergunta → busca vetorial híbrida → GPT-4o → resposta + fontes
  GET  /api/health  → healthcheck

Variáveis de ambiente (ver local.settings.json.example):
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY
  AZURE_OPENAI_CHAT_DEPLOYMENT, AZURE_OPENAI_EMBEDDING_DEPLOYMENT
  AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX_NAME
"""

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

app = func.FunctionApp()

# ──────────────────────────────────────────────────────────────────────────────
# Configurações
# ──────────────────────────────────────────────────────────────────────────────

INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX_NAME", "tea-conhecimento")
EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
CHAT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "100"))
TOP_N = int(os.environ.get("RAG_TOP_N", "5"))

# ──────────────────────────────────────────────────────────────────────────────
# Clientes Azure (instanciados por chamada para evitar problemas de cold start)
# ──────────────────────────────────────────────────────────────────────────────

def _openai() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version="2024-02-01",
    )


def _search_index_client() -> SearchIndexClient:
    return SearchIndexClient(
        endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["AZURE_SEARCH_KEY"]),
    )


def _search_client() -> SearchClient:
    return SearchClient(
        endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["AZURE_SEARCH_KEY"]),
        index_name=INDEX_NAME,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Gestão do Índice Vetorial
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_index() -> None:
    """Cria o índice vetorial no AI Search se ainda não existir."""
    idx_client = _search_index_client()
    existing = {idx.name for idx in idx_client.list_indexes()}
    if INDEX_NAME in existing:
        return

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="source", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="timestamp", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=1536,
            vector_search_profile_name="hnsw-profile",
        ),
    ]

    vector_search = VectorSearch(
        profiles=[VectorSearchProfile(
            name="hnsw-profile",
            algorithm_configuration_name="hnsw-config",
        )],
        algorithms=[HnswAlgorithmConfiguration(name="hnsw-config")],
    )

    idx_client.create_index(
        SearchIndex(name=INDEX_NAME, fields=fields, vector_search=vector_search)
    )
    logging.info("✅ Índice '%s' criado no Azure AI Search", INDEX_NAME)


# ──────────────────────────────────────────────────────────────────────────────
# Chunking Recursivo com Overlap
# ──────────────────────────────────────────────────────────────────────────────

def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Estratégia: chunking recursivo por tamanho fixo com overlap.

    Justificativa: documentos de saúde sobre TEA têm seções longas e densas.
    O overlap de 100 caracteres preserva contexto entre chunks adjacentes,
    evitando que conceitos sejam cortados abruptamente entre dois chunks.
    Os separadores são tentados em ordem: parágrafo → sentença → palavra.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = min(start + size, len(text))

        if end < len(text):
            # Tenta quebrar em parágrafo duplo
            split = text.rfind("\n\n", start, end)
            if split <= start:
                # Tenta quebrar em final de sentença
                split = text.rfind(". ", start, end)
            if split <= start:
                # Tenta quebrar em espaço
                split = text.rfind(" ", start, end)
            if split <= start:
                split = end
            else:
                split += 1  # inclui o separador no chunk atual
        else:
            split = end

        chunk = text[start:split].strip()
        if chunk:
            chunks.append(chunk)

        start = max(split - overlap, start + 1)

    return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Embeddings
# ──────────────────────────────────────────────────────────────────────────────

def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Gera embeddings em batch para lista de textos."""
    client = _openai()
    response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=texts)
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


def _embed_single(text: str) -> list[float]:
    return _embed_batch([text])[0]


# ──────────────────────────────────────────────────────────────────────────────
# Extração de texto de PDF (base64)
# ──────────────────────────────────────────────────────────────────────────────

def _extract_pdf_text(pdf_base64: str) -> str:
    """Extrai texto de PDF enviado como base64."""
    try:
        import io
        import pymupdf  # PyMuPDF

        pdf_bytes = base64.b64decode(pdf_base64)
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text())
        return "\n\n".join(pages)
    except Exception as exc:
        logging.error("Erro ao extrair PDF: %s", exc)
        raise ValueError(f"Não foi possível extrair texto do PDF: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint 1: POST /api/ingest
# ──────────────────────────────────────────────────────────────────────────────

@app.route(route="ingest", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    """
    Ingere um documento na base de conhecimento.

    Body JSON (opção A — texto):
        {"source": "guia_tea.txt", "text": "conteúdo do documento..."}

    Body JSON (opção B — PDF em base64):
        {"source": "guia_tea.pdf", "pdf_base64": "<base64>"}

    Retorna:
        {"status": "ok", "source": "...", "total_chunks": 12, "indexed": 12}
    """
    try:
        body = req.get_json()
    except (ValueError, AttributeError):
        return _error("Body JSON inválido.", 400)

    source = (body.get("source") or "documento.txt").strip()
    pdf_b64 = body.get("pdf_base64", "").strip()
    text = body.get("text", "").strip()

    # Extrai texto do PDF se fornecido
    if pdf_b64 and not text:
        try:
            text = _extract_pdf_text(pdf_b64)
        except ValueError as exc:
            return _error(str(exc), 422)

    if not text:
        return _error("Forneça 'text' ou 'pdf_base64'.", 400)

    # Garante que o índice existe
    _ensure_index()

    # Chunking recursivo
    chunks = _chunk_text(text)
    logging.info("📝 %d chunks gerados de '%s'", len(chunks), source)

    # Embeddings em batches de 16
    all_embeddings: list[list[float]] = []
    batch_size = 16
    for i in range(0, len(chunks), batch_size):
        all_embeddings.extend(_embed_batch(chunks[i : i + batch_size]))

    # Monta documentos para indexação
    now = datetime.now(timezone.utc).isoformat()
    documents = []
    for idx, (chunk, embedding) in enumerate(zip(chunks, all_embeddings)):
        doc_id = hashlib.md5(f"{source}_{idx}_{chunk[:40]}".encode()).hexdigest()
        documents.append({
            "id": doc_id,
            "source": source,
            "chunk_index": idx,
            "content": chunk,
            "content_vector": embedding,
            "timestamp": now,
        })

    # Indexa no AI Search
    sc = _search_client()
    results = sc.upload_documents(documents)
    succeeded = sum(1 for r in results if r.succeeded)
    logging.info("✅ %d/%d chunks indexados de '%s'", succeeded, len(documents), source)

    return func.HttpResponse(
        json.dumps({
            "status": "ok",
            "source": source,
            "total_chunks": len(chunks),
            "indexed": succeeded,
        }),
        mimetype="application/json",
        status_code=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint 2: POST /api/query
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """Você é um assistente especializado em Transtorno do Espectro Autista (TEA).

Responda perguntas com base EXCLUSIVAMENTE nos trechos de documentos fornecidos como contexto.
Se a resposta não estiver presente no contexto, diga claramente: "Não encontrei essa informação na base de conhecimento."
Seja objetivo, claro e cite a fonte quando relevante.
Responda sempre em português brasileiro."""


@app.route(route="query", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def query(req: func.HttpRequest) -> func.HttpResponse:
    """
    Consulta RAG: recebe uma pergunta e retorna resposta fundamentada nos documentos.

    Body JSON:
        {"question": "O que é terapia ABA?"}

    Retorna:
        {
          "answer": "A terapia ABA (Applied Behavior Analysis)...",
          "sources": [
            {"chunk": "trecho usado como contexto...", "source": "guia_tea.pdf", "score": 0.92}
          ]
        }
    """
    try:
        body = req.get_json()
        question = (body.get("question") or "").strip()
    except (ValueError, AttributeError):
        return _error('Body inválido. Envie: {"question": "sua pergunta"}', 400)

    if not question:
        return _error("Campo 'question' é obrigatório.", 400)

    logging.info("❓ Pergunta recebida: %s", question)

    # 1. Embedding da pergunta
    q_vector = _embed_single(question)

    # 2. Busca híbrida (vetorial + full-text) no AI Search
    sc = _search_client()
    vector_query = VectorizedQuery(
        vector=q_vector,
        k_nearest_neighbors=TOP_N,
        fields="content_vector",
    )
    results = list(sc.search(
        search_text=question,
        vector_queries=[vector_query],
        select=["id", "content", "source", "chunk_index"],
        top=TOP_N,
    ))

    if not results:
        return func.HttpResponse(
            json.dumps({
                "answer": "Não encontrei informações relevantes na base de conhecimento.",
                "sources": [],
            }, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )

    logging.info("📦 %d chunks recuperados (melhor score: %.4f)", len(results), results[0].get("@search.score", 0))

    # 3. Monta contexto
    context_parts = []
    sources = []
    for r in results:
        context_parts.append(f"[Fonte: {r['source']}]\n{r['content']}")
        sources.append({
            "chunk": r["content"][:400] + ("..." if len(r["content"]) > 400 else ""),
            "source": r["source"],
            "score": round(r.get("@search.score", 0.0), 4),
        })

    context = "\n\n---\n\n".join(context_parts)

    # 4. Gera resposta com GPT-4o
    client = _openai()
    completion = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"## Contexto:\n{context}\n\n## Pergunta:\n{question}"},
        ],
        temperature=0.1,
        max_tokens=800,
    )

    answer = completion.choices[0].message.content
    logging.info("✅ Resposta gerada com %d tokens", completion.usage.total_tokens)

    return func.HttpResponse(
        json.dumps({"answer": answer, "sources": sources}, ensure_ascii=False, indent=2),
        mimetype="application/json",
        status_code=200,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint 3: GET /api/health
# ──────────────────────────────────────────────────────────────────────────────

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Healthcheck da aplicação."""
    return func.HttpResponse(
        json.dumps({
            "status": "ok",
            "service": "rag-tea-conhecimento",
            "index": INDEX_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }),
        mimetype="application/json",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────────────────────────────────────

def _error(message: str, status: int) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": message}),
        mimetype="application/json",
        status_code=status,
    )
