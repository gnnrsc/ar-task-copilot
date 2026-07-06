# Adding New Equipment & Instructions

This guide explains how to extend the AR Task Co-Pilot agent to support new machinery.

The agent currently recognises **three hardcoded equipment models**:
- `Generator-XYZ-100`
- `Pump-Max-500`
- `Server-Rack-S900`

If you send any other `equipment_model`, the agent returns an error or may hallucinate steps.
Below are three approaches to fix this, from simplest to most scalable.

---

## Where the data lives today

Two files contain hardcoded dictionaries that must both be updated when adding a new machine:

| File | What it contains |
|---|---|
| [`app/agent.py`](app/agent.py) — `MOCK_SOP_DATABASE` (line 64) | Step-by-step instructions + warnings per model |
| [`app/mcp_server.py`](app/mcp_server.py) — `MOCK_SOP_DATABASE` (line 65) | Duplicate of the above, exposed as an MCP tool |
| [`app/mcp_server.py`](app/mcp_server.py) — `SAFETY_THRESHOLDS` (line 22) | Numeric safety limits + PPE per model |

> **Note**: the SOP database is currently duplicated in both files. Any of the approaches below
> should unify these into a single data source.

---

## Approach 1 — Edit the hardcoded dictionaries (current, quickest)

Add your machine directly to the Python dictionaries. No infrastructure needed.

### Step 1 — Add to `MOCK_SOP_DATABASE` in `agent.py`

```python
"Turbine-Z500": {
    "title": "Replace Main Bearing",
    "instructions": (
        "1. Shut down main power supply at panel B-3.\n"
        "2. Wait 10 minutes for full cooldown.\n"
        "3. Remove side cover using 8mm hex key.\n"
        "4. Extract worn bearing with dedicated puller tool.\n"
        "5. Insert replacement bearing Part CB-44 and torque to 20 Nm.\n"
        "6. Refit cover and restore power supply."
    ),
    "warnings": [
        "Rotating surfaces may remain hot up to 80°C after shutdown. Wear thermal gloves.",
        "Verify lock-out on panel B-3 before opening the cover."
    ]
},
```

### Step 2 — Add to `MOCK_SOP_DATABASE` in `mcp_server.py`

Add the same entry (instructions only — warnings and thresholds are managed separately in `SAFETY_THRESHOLDS`).

### Step 3 — Add to `SAFETY_THRESHOLDS` in `mcp_server.py`

```python
"Turbine-Z500": {
    "max_temperature_c": 80,
    "max_torque_nm": 20,
    "required_ppe": ["thermal gloves", "safety glasses", "hearing protection"],
    "voltage_risk": "medium"
},
```

**When to use**: during development or for a small, fixed set of machines (< 20 models).  
**Limitation**: requires a code change and redeploy for every new machine.

---

## Approach 2 — One JSON file per equipment model (no code change needed)

Create a folder `sop_data/` at the project root. Each machine gets its own JSON file.
The `doc_search_tool` and MCP tools scan the folder at runtime instead of reading a dict.

### Folder structure

```
ar-task-copilot/
+-- sop_data/
|   +-- Generator-XYZ-100.json
|   +-- Pump-Max-500.json
|   +-- Server-Rack-S900.json
|   +-- Turbine-Z500.json          <- add a file, done
+-- app/
+-- ...
```

### JSON file format

```json
{
  "model": "Turbine-Z500",
  "title": "Replace Main Bearing",
  "instructions": [
    "Shut down main power supply at panel B-3.",
    "Wait 10 minutes for full cooldown.",
    "Remove side cover using 8mm hex key.",
    "Extract worn bearing with dedicated puller tool.",
    "Insert replacement bearing Part CB-44 and torque to 20 Nm.",
    "Refit cover and restore power supply."
  ],
  "warnings": [
    "Rotating surfaces may remain hot up to 80°C after shutdown. Wear thermal gloves.",
    "Verify lock-out on panel B-3 before opening the cover."
  ],
  "safety_thresholds": {
    "max_temperature_c": 80,
    "max_torque_nm": 20,
    "required_ppe": ["thermal gloves", "safety glasses", "hearing protection"],
    "voltage_risk": "medium"
  }
}
```

### Code change required (one-time, in `agent.py` and `mcp_server.py`)

Replace the dictionary lookups with a file-system scan:

```python
import glob, os, json

SOP_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "sop_data")

def doc_search_tool(equipment_model: str) -> str:
    # Find the best-matching JSON file by name similarity
    files = glob.glob(os.path.join(SOP_DATA_DIR, "*.json"))
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0].lower()
        if name in equipment_model.lower() or equipment_model.lower() in name:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()   # return the raw JSON string

    return json.dumps({"error": f"No SOP found for '{equipment_model}'."})
```

**To add a new machine**: drop a new `.json` file in `sop_data/` — no code change, no redeploy.  
**Limitation**: matching is still name-based; a typo in the model name will miss the file.

---

## Approach 3 — Vector Store with semantic search (production)

Index all SOP documents as text embeddings. The agent can then find the right procedure
even with partial names, typos, or descriptions in a different language.

### How it works

```
PDF / Word manuals
      |
      | (one-time indexing script)
      v
Text chunks  -->  Embedding model (e.g. text-embedding-004)
      |
      v
Vector DB  (ChromaDB locally, or Pinecone / pgvector in cloud)
      |
      | similarity search at query time
      v
doc_search_tool(equipment_model + task_description)
      |
      v
Top-K relevant chunks  -->  Orchestrator LLM as context
```

### Recommended stack

| Component | Local / free option | Cloud option |
|---|---|---|
| Vector DB | ChromaDB (pip install chromadb) | Pinecone, Weaviate |
| Embedding model | `text-embedding-004` via Gemini API | Same |
| Document parsing | `pypdf` for PDF, `python-docx` for Word | Same |

### Indexing script (run once per new document)

```python
import chromadb
from google import genai

client = chromadb.PersistentClient(path="./chroma_sop_db")
collection = client.get_or_create_collection("sop_procedures")

genai_client = genai.Client()

def index_sop(equipment_model: str, text: str):
    """Splits the SOP text into chunks and stores their embeddings in ChromaDB."""
    chunks = [text[i:i+500] for i in range(0, len(text), 500)]
    for i, chunk in enumerate(chunks):
        result = genai_client.models.embed_content(
            model="text-embedding-004", contents=chunk
        )
        collection.add(
            ids=[f"{equipment_model}_chunk_{i}"],
            embeddings=[result.embeddings[0].values],
            documents=[chunk],
            metadatas=[{"equipment_model": equipment_model}]
        )
```

### Updated `doc_search_tool` (semantic)

```python
def doc_search_tool(equipment_model: str, task_description: str = "") -> str:
    query = f"{equipment_model} {task_description}"
    result = genai_client.models.embed_content(
        model="text-embedding-004", contents=query
    )
    hits = collection.query(
        query_embeddings=[result.embeddings[0].values],
        n_results=5,
        where={"equipment_model": {"$eq": equipment_model}}  # optional filter
    )
    return "\n\n".join(hits["documents"][0])
```

**To add a new machine**: run `index_sop("Turbina-Z500", full_manual_text)` — no code change.  
**Advantage**: works with partial names, synonyms, multilingual queries, and full PDF manuals.  
**Limitation**: requires Gemini API key for embeddings; ChromaDB adds a local dependency.

---

## Choosing the right approach

| | Approach 1 (dict) | Approach 2 (JSON files) | Approach 3 (vector store) |
|---|---|---|---|
| Add new machine | Code change + redeploy | Drop a JSON file | Run indexing script |
| Name matching | Exact (case-insensitive) | Exact (case-insensitive) | Semantic / fuzzy |
| Works offline | Yes | Yes | Embeddings need API |
| Setup time | None | 30 min (one-time) | 2–4 hours |
| Scales to | ~20 models | ~500 models | Unlimited |
| Best for | Demo / prototype | Small-medium fleet | Production |

> **Recommended migration path**:
> 1. Start with Approach 2 (JSON files) to decouple data from code immediately.
> 2. Upgrade to Approach 3 (vector store) when you have real PDF manuals
>    or need to support more than a few dozen machines.

---

## Spatial Anchor Registry (per new machines)

Adding a machine to the SOP database is only half the work.
You also need to create the **anchor offset registry** so the XR app knows
where to place the overlays on the physical machine.

Create one JSON config per model in `anchor_registries/`:

```json
{
  "model": "Turbine-Z500",
  "anchors": [
    { "name": "Power Panel B-3",  "offset": [0.05, 1.20, 0.00] },
    { "name": "Side Cover",        "offset": [0.30, 0.60, 0.10] },
    { "name": "Bearing Housing",   "offset": [0.25, 0.55, 0.15] }
  ]
}
```

The anchor `name` values must match the `spatial_anchor` strings that the
`ar_visualizer_agent` will generate — which in turn come from the component
names in your SOP instructions. Using consistent terminology in the SOP text
(e.g. always "Side Cover", never "lateral panel" or "cover plate")
ensures reliable matching.

See [AR_INTEGRATION_GUIDE.md](AR_INTEGRATION_GUIDE.md) §4 for how the XR app loads and uses this registry.
