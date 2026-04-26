# RAG TEA Conhecimento

**Trabalho Final — Serviços Cognitivos em Cloud | FIAP MBA Data Science & IA**

Aplicação RAG (Retrieval-Augmented Generation) sobre uma base de conhecimento
de documentos sobre Transtorno do Espectro Autista (TEA), construída com
Azure Functions, Azure OpenAI e Azure AI Search.

---

## Visão Geral

**Problema:** Pais e profissionais de saúde que acompanham pessoas com TEA
precisam de acesso rápido e confiável a informações sobre terapias, legislação,
diagnóstico e estratégias de suporte — informações dispersas em múltiplos
documentos técnicos.

**Solução:** Um assistente RAG que indexa documentos (guias clínicos, FAQs,
legislação) e responde perguntas em linguagem natural, sempre citando a fonte.
A resposta é gerada pelo GPT-4o com base apenas nos trechos recuperados —
sem alucinação.

**Domínio escolhido:** Saúde digital — TEA (documentos de terapia, legislação
e orientação familiar).

---

## Arquitetura

```
┌──────────────┐   POST /api/query    ┌──────────────────────────────────┐
│  Cliente     │ ──────────────────▶  │  Azure Function App (Python 3.11)│
│  (curl /     │                      │                                  │
│  Postman)    │ ◀──────────────────  │  /api/ingest   /api/query        │
└──────────────┘   resposta + fontes  └──────────┬───────────────────────┘
                                                  │
                         ┌────────────────────────┼────────────────────┐
                         ▼                        ▼                    ▼
                  ┌─────────────┐        ┌──────────────┐    ┌──────────────┐
                  │ Azure OpenAI│        │  Azure AI    │    │  Chunking    │
                  │ GPT-4o      │        │  Search      │    │  Recursivo   │
                  │ text-emb-3s │        │  (vetorial + │    │  (800 chars, │
                  └─────────────┘        │  full-text)  │    │  overlap 100)│
                                         └──────────────┘    └──────────────┘
```

**Fluxo de ingestão** (`POST /api/ingest`):
1. Recebe documento (texto ou PDF base64) + nome do arquivo
2. Aplica chunking recursivo com overlap
3. Gera embeddings via `text-embedding-3-small` (batches de 16)
4. Indexa chunks + vetores no Azure AI Search

**Fluxo de consulta** (`POST /api/query`):
1. Recebe pergunta do usuário
2. Gera embedding da pergunta
3. Executa busca híbrida (vetorial + full-text) → top 5 chunks
4. Monta contexto e chama GPT-4o com prompt RAG
5. Retorna resposta gerada + fontes utilizadas

---

## Pré-requisitos

- Python 3.11+
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Azure CLI (`az`)
- Conta Azure com acesso a:
  - Azure OpenAI (com modelos `gpt-4o` e `text-embedding-3-small` deployados)
  - Azure AI Search
  - Azure Storage Account

---

## Como Executar Localmente

### 1. Clonar e configurar ambiente

```bash
git clone <url-do-repositorio>
cd trabalho_final

python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configurar variáveis de ambiente

```bash
cp local.settings.json.example local.settings.json
```

Edite `local.settings.json` com as credenciais dos recursos Azure:

| Variável | Onde encontrar |
|---|---|
| `AzureWebJobsStorage` | Storage Account → Access keys → Connection string |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI → Keys and Endpoint |
| `AZURE_OPENAI_KEY` | Azure OpenAI → Keys and Endpoint → Key 1 |
| `AZURE_SEARCH_ENDPOINT` | AI Search → Overview → URL |
| `AZURE_SEARCH_KEY` | AI Search → Keys → Primary admin key |

### 3. Iniciar localmente

```bash
func start
```

Saída esperada:
```
Functions:
    ingest:  [POST] http://localhost:7071/api/ingest
    query:   [POST] http://localhost:7071/api/query
    health:  [GET]  http://localhost:7071/api/health
```

---

## Como Fazer o Deploy

### Opção A — Via Azure Functions Core Tools

```bash
# 1. Criar o Function App no Azure (se não usou o Bicep)
az functionapp create \
  --resource-group <rg-name> \
  --consumption-plan-location eastus2 \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name <function-app-name> \
  --storage-account <storage-name> \
  --os-type Linux

# 2. Publicar o código
func azure functionapp publish <function-app-name>
```

### Opção B — Via Infraestrutura como Código (Bicep)

```bash
# Provisiona todos os recursos + publica o código
az deployment sub create \
  --location eastus2 \
  --template-file infra/main.bicep \
  --parameters infra/parameters.json

# Após o deploy, publique o código
func azure functionapp publish tearag-func-<sufixo>
```

---

## Exemplos de Uso

### Ingerir um documento

```bash
curl -X POST http://localhost:7071/api/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "source": "guia_tea_terapias.txt",
    "text": "'"$(cat docs/guia_tea_terapias.txt)"'"
  }'
```

Resposta esperada:
```json
{
  "status": "ok",
  "source": "guia_tea_terapias.txt",
  "total_chunks": 18,
  "indexed": 18
}
```

---

### Exemplo 1 — Pergunta sobre terapia ABA

```bash
curl -X POST http://localhost:7071/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "O que é terapia ABA e quais suas principais técnicas?"}'
```

Resposta esperada:
```json
{
  "answer": "A Análise do Comportamento Aplicada (ABA) é a abordagem com maior evidência científica para o TEA. Baseia-se nos princípios do behaviorismo e utiliza técnicas como o reforço positivo — comportamentos desejados são incentivados por consequências agradáveis — e o ensino por tentativas discretas (DTT), em que habilidades complexas são divididas em passos pequenos. O modelo naturalístico (NET) promove o aprendizado em contextos cotidianos. Intervenções intensivas (25–40 horas semanais) iniciadas precocemente levam a ganhos em linguagem e habilidades sociais.",
  "sources": [
    {
      "chunk": "A Análise do Comportamento Aplicada (ABA, do inglês Applied Behavior Analysis) é a abordagem com maior evidência científica...",
      "source": "guia_tea_terapias.txt",
      "score": 0.9312
    }
  ]
}
```

---

### Exemplo 2 — Pergunta sobre legislação

```bash
curl -X POST http://localhost:7071/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "O plano de saúde é obrigado a cobrir terapias para TEA?"}'
```

Resposta esperada:
```json
{
  "answer": "Sim. A Resolução Normativa ANS nº 539/2022 estabelece que os planos de saúde são obrigados a cobrir todas as sessões de terapias prescritas por médico para pessoas com TEA — incluindo ABA, fonoaudiologia, terapia ocupacional e psicologia — sem limitação de número de sessões. Em caso de negativa do plano, o usuário pode registrar reclamação no site da ANS ou buscar orientação no PROCON.",
  "sources": [
    {
      "chunk": "A Resolução Normativa nº 539/2022 da ANS estabelece que os planos de saúde são obrigados a cobrir...",
      "source": "guia_tea_terapias.txt",
      "score": 0.9187
    }
  ]
}
```

---

### Exemplo 3 — Pergunta sobre comunicação alternativa

```bash
curl -X POST http://localhost:7071/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "Como funciona o sistema PECS de comunicação por figuras?"}'
```

Resposta esperada:
```json
{
  "answer": "O PECS (Picture Exchange Communication System) é um sistema de comunicação por troca de figuras. A criança aprende a entregar uma figura para solicitar um item desejado, progredindo por 6 fases: (I) troca física da figura por um item, (II) persistência e distância, (III) discriminação entre figuras, (IV) estrutura de sentença 'Eu quero ___', (V) resposta à pergunta 'O que você quer?' e (VI) comentários espontâneos.",
  "sources": [
    {
      "chunk": "PECS (Picture Exchange Communication System): O PECS é um sistema de comunicação por troca de figuras...",
      "source": "guia_tea_terapias.txt",
      "score": 0.9421
    }
  ]
}
```

---

### Healthcheck

```bash
curl http://localhost:7071/api/health
```

```json
{"status": "ok", "service": "rag-tea-conhecimento", "index": "tea-conhecimento"}
```

---

## Decisões Técnicas

### Domínio: Saúde Digital — TEA
Documentos sobre TEA são densos, técnicos e frequentemente consultados por pais
e profissionais que precisam de respostas rápidas e confiáveis. O RAG garante que
as respostas sejam fundamentadas nos documentos indexados, sem risco de
alucinações sobre recomendações médicas.

### Estratégia de Chunking: Recursivo com Overlap
- **Tamanho**: 800 caracteres (≈ 150 palavras) — equilibra contexto suficiente
  com precisão na recuperação
- **Overlap**: 100 caracteres — preserva contexto entre chunks adjacentes,
  evitando corte de conceitos no meio
- **Separadores**: parágrafo duplo → sentença → palavra (em ordem de preferência)

**Por que não semântico via LLM?** Para documentos estruturados em seções longas
(como guias clínicos), o chunking por tamanho fixo com overlap tem custo zero e
performance comparável. O chunking semântico via LLM seria preferido para
documentos muito heterogêneos ou sem estrutura clara.

### Banco Vetorial: Azure AI Search
- Usado em aula → equipe já familiarizada
- Busca híbrida nativa (vetorial + BM25 full-text) sem configuração extra
- Escalável e gerenciado no mesmo ecossistema Azure

**Por que não ChromaDB/Qdrant?** Para um produto em produção no Azure, AI Search
elimina a necessidade de gerenciar infraestrutura adicional.

### Modelo de Embedding: text-embedding-3-small (1536 dims)
- Melhor custo-benefício da família OpenAI
- Ótima performance em português (MTEB benchmark)

### Modelo de Chat: GPT-4o
- Melhor qualidade de resposta para texto em português
- Temperatura 0.1 para máxima fidelidade ao contexto fornecido

---

## Estrutura do Projeto

```
trabalho_final/
├── function_app.py              # Código principal (ingest + query + health)
├── requirements.txt
├── host.json
├── .funcignore
├── .gitignore
├── local.settings.json.example  # Template de variáveis (não commitar o real)
├── prompts/
│   ├── chunking_system.md       # Prompt para chunking semântico (referência)
│   └── rag_system.md            # Prompt do assistente RAG (referência)
├── docs/
│   └── guia_tea_terapias.txt    # Documento de exemplo para ingestão
└── infra/
    ├── main.bicep               # IaC: provisiona todos os recursos Azure
    └── parameters.json
```

---

## Vídeo de Demonstração

[Link para o vídeo no YouTube/Drive — https://www.youtube.com/watch?v=Y9rPhiKhpmI ]

O vídeo demonstra:
1. Deploy em produção com URL em nuvem visível
2. Ingestão do documento `guia_tea_terapias.txt`
3. Pergunta 1: "O que é terapia ABA?"
4. Pergunta 2: "O plano de saúde é obrigado a cobrir terapias para TEA?"
5. Pergunta 3: "Como funciona o sistema PECS?"

---

*Trabalho elaborado para a disciplina Cloud & Cognitive Environments — FIAP MBA Data Science & IA*
