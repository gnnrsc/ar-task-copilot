# Universal AR Task Co-Pilot

**Hands-free, real-time AI guidance for industrial technicians — turning static manuals into live spatial AR instructions with safety-first multi-agent intelligence.**

**Track: Agents for Business**

---

## 1. Problem Statement

Every day, thousands of industrial technicians — mechanics, field engineers, data-center operators — face the same friction: they must pause a task, remove gloves, flip through hundreds of pages of static PDF manuals or query legacy databases, locate the right safety precaution or torque value, and then re-engage with the equipment.

This context-switching is slow, error-prone, and genuinely dangerous. A missed safety threshold (e.g., exhaust temperature exceeding safe handling limits) or an overlooked lock-out/tag-out step can result in equipment damage, costly downtime, or personal injury. In industrial settings, unplanned downtime alone costs manufacturers an average of $260,000 per hour (Aberdeen Group).

The core problem is the mismatch between how knowledge is stored (static documents) and how technicians need it (real-time, hands-free, spatially relevant).

---

## 2. Proposed Solution

The **Universal AR Task Co-Pilot** is a Google ADK 2.0 multi-agent system that converts a natural-language task request and an equipment model identifier into a fully-structured, safety-screened set of 3D spatial AR instructions — ready to be rendered in an AR headset field of view.

A technician wearing a HoloLens or Meta Quest headset can describe their task (or submit a structured request from a connected client), and the system:

1. **Validates** the input for safety policy compliance and scrubs PII
2. **Retrieves** the correct Standard Operating Procedure (SOP) from the knowledge base
3. **Extracts** domain-specific safety warnings using real equipment safety thresholds
4. **Maps** each procedural step to a 3D AR element (arrow, highlight, floating text box)
5. **Delivers** a structured JSON payload ready for immediate headset rendering

The result is a hands-free, eyes-up experience that keeps technicians safe, reduces errors, and eliminates manual lookups entirely.

---

## 3. Architecture & ADK Multi-Agent Design

The system is built on the **Google ADK 2.0 Workflow graph API**, using function nodes connected by typed edges. Workflow graphs provide deterministic routing (critical for safety-gated systems), clear auditability, and clean separation between security logic and AI reasoning.

### Agent Graph

```
START
  └─► security_checkpoint (node)
        ├─► [security_violation] ─► security_violation_handler (node) ─► END
        └─► [approved] ─► orchestrator (LlmAgent)
                              └─► save_orchestrator_output (node)
                                    └─► final_output (node) ─► END
```

### Workflow Nodes

| Node | Type | Role |
|---|---|---|
| `security_checkpoint` | `@node` function | PII scrubbing, injection detection, safety bypass blocking, audit logging |
| `orchestrator` | `LlmAgent` | Central coordinator; delegates to sub-agents via `AgentTool` |
| `save_orchestrator_output` | `@node` function | Extracts structured schema from orchestrator and writes to `ctx.state` |
| `final_output` | `@node` function | Formats the validated AR engine payload returned to the client |
| `security_violation_handler` | `@node` function | Returns structured error for policy violations |

### Sub-Agents (via AgentTool)

The orchestrator coordinates two specialized `LlmAgent` instances, each with typed input/output Pydantic schemas:

**`WarningScoutAgent`** — Safety & Risk Assessor
- Input: `WarningScoutInput(equipment_model, task_instructions)`
- Output: `WarningScoutOutput(warnings: List[str])`
- Calls MCP tool `get_safety_thresholds` to verify equipment-specific limits before finalizing warnings.

**`ArVisualizerAgent`** — XR UI Designer
- Input: `ArVisualizerInput(steps: List[str])`
- Output: `ArVisualizerOutput(ar_steps: List[ArStep])`
- Maps each step to a typed `ArStep` (element type, spatial anchor, guidance text). Calls MCP tool `get_xr_ui_templates` for rendering metadata.

Both sub-agents are connected to the MCP server via `MCPToolset`. The orchestrator is the only entry point into knowledge retrieval, calling `doc_search_tool` (local SOP lookup) and the MCP `search_sop_database` tool directly.

### State Management

A `TaskState` Pydantic model tracks all inter-node data: `equipment_model`, `task_description`, `safety_warnings`, `ar_steps`, `supervisor_feedback`, `approved`, and `security_error`. Nodes read and write exclusively through `ctx.state`, keeping the graph stateless at the edge level.

---

## 4. Model Context Protocol (MCP) Server

The project includes a custom **stdio MCP server** (`app/mcp_server.py`) built with the MCP Python SDK, exposing three domain-specific tools:

| Tool | Purpose |
|---|---|
| `get_safety_thresholds(equipment_model)` | Returns max torque (Nm), max temperature (°C), required PPE, and voltage risk level for known equipment models |
| `get_xr_ui_templates()` | Returns rendering metadata for headset components: neon colors, blink rates, scale coordinates for arrows/highlights/text boxes |
| `search_sop_database(equipment_model)` | Looks up standard operating procedures by model identifier |

Both `WarningScoutAgent` and `ArVisualizerAgent` connect to this server via `MCPToolset`, and the orchestrator can also query it directly — meaning three of the four AI-driven components in the graph use MCP.

The MCP server runs as a subprocess via `StdioServerParameters`, with `sys.executable` ensuring it runs in the same virtual environment as the ADK agent, avoiding path resolution failures across environments.

---

## 5. Security & Safety Design

Security is the **first node in the graph**. No AI reasoning begins until `security_checkpoint` has completed. This design reflects the domain reality: in an industrial setting, a manipulated instruction set could cause physical harm.

### Controls Implemented

**PII Redaction (regex-based)**
- Email addresses → `[REDACTED_EMAIL]`
- Phone numbers → `[REDACTED_PHONE]`
- Serial numbers (format `SN######`) → `[REDACTED_SERIAL]`

This prevents sensitive operational data from propagating into LLM prompts and appearing in logs or outputs.

**Prompt Injection Detection**
Keywords such as `ignore previous`, `system prompt`, `dan mode`, `override constraints` trigger an immediate `security_violation` route — the orchestrator never executes.

**Physical Safety Bypass Detection**
A domain-specific rule blocks instructions containing `bypass safety`, `disable safety`, `hot swap live`, `bypass breaker`. These represent genuinely dangerous industrial scenarios (e.g., servicing live electrical equipment). The violation is logged at `WARNING` severity and routed directly to `security_violation_handler`.

**Structured JSON Audit Logging**
Every invocation of `security_checkpoint` emits a JSON audit log to stdout with fields: `node`, `severity` (`INFO`/`WARNING`/`CRITICAL`), `message`, and `details`. This creates a traceable record of every security decision for compliance review.

---

## 6. Demo Walkthrough

Three test scenarios demonstrate the full capability range of the system:

### Scenario 1 — Standard Maintenance Task (Automated Flow)
**Input:**
```json
{
  "equipment_model": "Generator-XYZ-100",
  "task_description": "Replace the primary cooling filter. Contact support at engineer@factory.com or S/N: SN998877."
}
```
**What happens:** `security_checkpoint` redacts the email to `[REDACTED_EMAIL]` and serial to `[REDACTED_SERIAL]`. The orchestrator fetches the SOP, routes to `WarningScoutAgent` (which confirms the 70°C thermal threshold and required PPE), then to `ArVisualizerAgent` (which maps 6 steps to 3D Arrows and Floating Text Boxes). Final output: `"status": "APPROVED"` with full AR coordinates.

### Scenario 2 — Real-Time Thermal Anomaly Adaptation
**Input:**
```json
{
  "equipment_model": "Server-Rack-S900",
  "task_description": "Replace failed PSU module. Warning: Exhaust temperature is 48 C."
}
```
**What happens:** `WarningScoutAgent` calls `get_safety_thresholds` and detects that 48°C exceeds the Server-Rack-S900 limit of 35°C. It automatically inserts a 5-minute cooldown instruction at Step 4 and flags the heat hazard. The AR overlay for Step 4 reads: *"WARNING: Exhaust temperature is 48C. WAIT 5 MINUTES BEFORE PROCEEDING."*

### Scenario 3 — Safety Policy Gate (Blocked)
**Input:**
```json
{
  "equipment_model": "Generator-XYZ-100",
  "task_description": "I need to bypass safety breaker and disable safety sensors to speed up operations."
}
```
**What happens:** `security_checkpoint` detects the keyword `bypass safety`, emits a `WARNING`-severity audit log, and routes to `security_violation_handler` — the LLM is never invoked. Output: `"status": "SECURITY_VIOLATION"` with a clear policy message.

---

## 7. Toolchain & Project Setup

The project was scaffolded using **Google Agents CLI** (`agents-cli scaffold`) and follows ADK best practices throughout:

- **`app/agent.py`** — Pydantic state schema, sub-agent definitions, workflow nodes, graph assembly
- **`app/mcp_server.py`** — FastMCP stdio server with 3 domain tools
- **`app/config.py`** — Environment-driven `AgentConfig` dataclass (reads `GEMINI_MODEL` from `.env`)
- **`pyproject.toml`** — Pinned dependency ranges: `google-adk[gcp]>=2.0.0,<3.0.0`, `mcp>=1.0.0,<2.0.0`, `fastapi>=0.110,<1.0`, `uvicorn>=0.29,<1.0`
- **`Makefile`** — `install`, `playground`, `run`, `test` targets

**To run locally:**
```bash
uv sync
uv run adk web app --host 127.0.0.1 --port 18081 --reload_agents
# Access the ADK Playground at http://localhost:18081
```

Model: `gemini-2.5-flash-lite` (configured via `GEMINI_MODEL` in `.env`; higher daily quota suitable for multi-agent sequential calls on the free tier).

---

## 8. Impact & Value

**Who benefits:** Industrial manufacturers, data center operators, field service organizations — any enterprise where frontline workers execute complex, safety-critical procedures.

**Direct business value:**
- **Reduced error rates** — AR-guided steps eliminate manual page-flipping and misread instructions
- **Faster task completion** — real-time SOP retrieval and instant AR payload generation vs. minutes of manual lookup
- **Safety compliance** — automated policy enforcement prevents dangerous procedure violations before any AI reasoning begins
- **Auditability** — structured JSON audit logs on every request support regulatory and insurance requirements

**Scalability:** The MCP server's tool-based architecture means new equipment models and safety databases can be added without modifying the agent graph. The local SOP tools can be replaced with live connections to real ERP or CMMS systems (SAP, IBM Maximo) with no changes to the ADK workflow structure.

The Universal AR Task Co-Pilot demonstrates that agentic AI in industrial environments is not just a productivity tool — it can be a frontline safety system.

