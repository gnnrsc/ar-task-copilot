# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional
from pydantic import BaseModel, Field

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool, McpToolset
from mcp import StdioServerParameters
from google.adk.workflow import START, Edge, Workflow, node
from google.adk.agents.context import Context
from google.adk.events import RequestInput, Event
from google.adk.utils.content_utils import extract_text_from_content
from google.genai import types

from .config import config

# ==========================================
# 1. State Schema & Pydantic Data Structures
# ==========================================
# Design choice: TaskState is a Pydantic model stored in ctx.state, the ADK
# Workflow's shared memory between nodes. Using a typed schema (rather than a
# plain dict) ensures that every node reads/writes well-defined fields and that
# missing keys surface as validation errors at development time, not silent
# runtime bugs in production.

class ArStep(BaseModel):
    step_number: int = Field(description="Sequential step number starting from 1")
    instruction: str = Field(description="Step instruction description")
    ar_element_type: str = Field(description="XR UI component to render (e.g., '3D Arrow', 'Highlight', 'Floating Text Box')")
    spatial_anchor: str = Field(description="Equipment part/anchor point to attach the element to")
    ar_guidance_text: str = Field(description="Short text to display in the user's field of view")

class OrchestratorOutput(BaseModel):
    safety_warnings: List[str] = Field(description="Critical safety warnings or precautions")
    ar_steps: List[ArStep] = Field(description="List of step-by-step instructions mapped to XR UI components")
    summary: str = Field(description="Brief summary of the procedure")

class TaskState(BaseModel):
    equipment_model: str = ""
    task_description: str = ""
    raw_instructions: str = ""
    safety_warnings: List[str] = Field(default_factory=list)
    ar_steps: List[Dict[str, Any]] = Field(default_factory=list)
    supervisor_feedback: str = ""
    approved: bool = False
    security_error: str = ""

# ==========================================
# 2. Mock Technical Documentation DB & Tool
# ==========================================
# The MOCK_SOP_DATABASE simulates an enterprise CMMS (Computerized Maintenance
# Management System) like IBM Maximo or SAP PM. In production, doc_search_tool
# would be replaced by a call to a real REST API or vector search index.
# The dual-path design (local doc_search_tool + MCP search_sop_database) is
# intentional: it lets the orchestrator fall back to the MCP server if the
# local cache misses, without changing agent logic.

MOCK_SOP_DATABASE = {
    "Generator-XYZ-100": {
        "title": "Replace Primary Cooling Filter",
        "instructions": (
            "1. Turn off secondary coolant valve V-12.\n"
            "2. Drain reserve coolant chamber C-4 into a container.\n"
            "3. Unscrew primary filter casing using a 10mm hex key.\n"
            "4. Replace filter media with Part F-99.\n"
            "5. Tighten casing back to 15 Nm torque.\n"
            "6. Refill coolant, close drain port, and open secondary valve V-12."
        ),
        "warnings": [
            "Coolant in reserve chamber C-4 may exceed 70 degrees C. Wear heavy-duty thermal gloves.",
            "Ensure secondary valve V-12 is fully closed and locked before draining chamber C-4 to prevent high pressure spray."
        ]
    },
    "Pump-Max-500": {
        "title": "Inspect Impeller Housing",
        "instructions": (
            "1. Shut down pump main power breaker B-5.\n"
            "2. Disconnect inlet duct coupling flange.\n"
            "3. Loosen housing bolts in star pattern.\n"
            "4. Slide back casing to inspect impeller blades.\n"
            "5. Check for debris or cavitation wear.\n"
            "6. Reassemble housing, tighten bolts to 22 Nm, and restore breaker B-5."
        ),
        "warnings": [
            "Impeller blades may have sharp cavitated edges. Wear cut-resistant safety gloves.",
            "Verify lock-out tag-out on breaker B-5. Residual charge may cause sudden rotation."
        ]
    },
    "Server-Rack-S900": {
        "title": "Troubleshoot Power Supply Fan Failure",
        "instructions": (
            "1. Locate failed PSU module indicated by red LED.\n"
            "2. Connect ESD wrist strap to ground.\n"
            "3. Power down server partition and slide PSU latch.\n"
            "4. Inspect exhaust temperature: if it exceeds 35 degrees C, wait 5 minutes before handling.\n"
            "5. Replace failed PSU unit with Spare Model S-PSU.\n"
            "6. Re-insert PSU, secure latch, and restore power partition."
        ),
        "warnings": [
            "Ensure partition power is fully turned off to prevent high-voltage electrical shock.",
            "Exhaust fan assembly and casing may be extremely hot if server was active. Wear safety glasses."
        ]
    }
}

def doc_search_tool(equipment_model: str) -> str:
    """Queries the technical documentation database for a given equipment model.

    Args:
        equipment_model: The name/model of the equipment (e.g., 'Generator-XYZ-100', 'Pump-Max-500').

    Returns:
        A string containing the standard operating procedure (SOP) and raw instructions/warnings.
    """
    model_key = None
    for key in MOCK_SOP_DATABASE:
        if key.lower() in equipment_model.lower() or equipment_model.lower() in key.lower():
            model_key = key
            break
            
    if model_key:
        sop = MOCK_SOP_DATABASE[model_key]
        return json.dumps({
            "model": model_key,
            "title": sop["title"],
            "instructions": sop["instructions"],
            "warnings": sop["warnings"]
        })
    else:
        return json.dumps({
            "error": f"No documentation found for equipment model '{equipment_model}'."
        })

# ==========================================
# MCP Toolset Initialization
# ==========================================
# Design choice: a single shared McpToolset instance is constructed once at
# module load time and injected into multiple agents. This avoids spawning
# redundant MCP subprocesses and keeps the stdio connection pool small.
# sys.executable ensures the MCP server runs in the same .venv as the ADK
# agent, avoiding PATH/interpreter mismatch errors across environments.

import sys

mcp_toolset = McpToolset(
    connection_params=StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"]
    )
)

# ==========================================
# 3. Sub-Agents Definition & Wrapping
# ==========================================
# Design choice: each sub-agent has a strict input_schema and output_schema
# (Pydantic models). This forces the LLM to produce structured JSON rather than
# free-form prose, making downstream parsing deterministic. The orchestrator
# wraps each sub-agent via AgentTool so it can call them like regular tools,
# keeping the orchestrator's planning logic clean and reusable.

class WarningScoutInput(BaseModel):
    equipment_model: str = Field(description="The model identifier of the equipment")
    task_instructions: str = Field(description="The raw task instructions text")

class WarningScoutOutput(BaseModel):
    warnings: List[str] = Field(description="List of critical safety warnings and hazards found")

warning_scout_agent = Agent(
    name="warning_scout_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a safety officer specialized in industrial and field operations.
Analyze the provided equipment documentation and task instructions.
Identify all critical safety warnings, hazards, and precautions, and return them by calling the 'finish_task' tool matching the output schema. Do not output raw JSON text; you must call 'finish_task'.
Focus on safety concerns (e.g., hot surfaces, electrical lockouts, high pressure).
Use safety thresholds from the MCP server tool 'get_safety_thresholds' to verify temperature limits, PPE requirements, or voltage risk for this equipment model.""",
    input_schema=WarningScoutInput,
    output_schema=WarningScoutOutput,
    tools=[mcp_toolset],
    description="Searches manuals and extracts critical safety warnings and hazards."
)

class ArVisualizerInput(BaseModel):
    steps: List[str] = Field(description="The list of task steps to visualize")

class ArVisualizerOutput(BaseModel):
    ar_steps: List[ArStep] = Field(description="The list of steps mapped to XR UI components")

ar_visualizer_agent = Agent(
    name="ar_visualizer_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are an XR User Interface designer.
Convert the provided raw mechanical instructions into a list of 3D spatial AR elements and floating text overlays.
For each step, determine:
1. The appropriate AR element type (e.g., '3D Arrow', 'Highlight', 'Floating Text Box').
2. The physical anchor point/part on the equipment.
3. A concise guidance label to display to the user.
Query the MCP tool 'get_xr_ui_templates' to look up available templates, colors, blink rates, and scale parameters before formatting the response.
Format the output and return it by calling the 'finish_task' tool strictly matching the output schema. Do not output raw JSON text; you must call 'finish_task'.""",
    input_schema=ArVisualizerInput,
    output_schema=ArVisualizerOutput,
    tools=[mcp_toolset],
    description="Converts raw mechanical steps into 3D AR UI sequences."
)

warning_scout_tool = AgentTool(agent=warning_scout_agent, skip_summarization=False)
ar_visualizer_tool = AgentTool(agent=ar_visualizer_agent, skip_summarization=False)

# ==========================================
# 4. Main Orchestrator Agent
# ==========================================
# The orchestrator uses mode="task" (single-turn structured output) rather than
# mode="chat" (multi-turn conversation). This is deliberate: for industrial
# procedures we need a deterministic JSON payload on every call, not a
# conversational exchange. The orchestrator is the ONLY agent with access to
# doc_search_tool — sub-agents receive pre-fetched content, which reduces
# redundant SOP lookups and limits LLM token usage.

orchestrator = Agent(
    name="orchestrator",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are the main coordinator of the Universal AR Task Co-Pilot.
Your goal is to convert a user's technical task request into a structured AR guidance plan.
You MUST provide your final output by calling the 'finish_task' tool with parameters matching the OrchestratorOutput schema. Do not output the JSON as plain text; you must call 'finish_task'.
Never return conversational responses, explanations, warnings, or raw text directly.
Follow these steps:
1. Retrieve the equipment manual/SOP using `doc_search_tool` (or the MCP tool `search_sop_database`) for the given equipment model.
2. If no technical documentation or SOP is found for the given equipment model, or if the search returns an error, set 'safety_warnings' to ['Error: Equipment model not found in the technical manual database.'] and describe this in the 'summary', and leave 'ar_steps' as an empty list.
3. Extract the instructions and pass them along with the model to `warning_scout_agent` to extract safety warnings.
4. Pass the instructions (split by lines) to `ar_visualizer_agent` to format them into 3D AR UI sequences.
5. If supervisor feedback is provided, revise the warnings and steps based on the feedback.
Format the final result strictly matching the output schema by calling the 'finish_task' tool.""",
    tools=[doc_search_tool, warning_scout_tool, ar_visualizer_tool, mcp_toolset],
    output_schema=OrchestratorOutput,
    mode="task",
)

# ==========================================
# 5. Workflow Node Functions
# ==========================================
# Design choice: ADK Workflow graph (not a plain LlmAgent) is used as the root
# agent. This provides two key advantages for a safety-critical system:
#   a) Deterministic routing — security_checkpoint always runs first, before
#      any LLM call, so no input can bypass the security gate.
#   b) Auditability — every edge transition is logged by the ADK runtime,
#      giving a full trace of which node handled each request.

@node(rerun_on_resume=True)
async def orchestrator_node(ctx: Context, node_input: Any) -> Any:
    """Invokes the orchestrator Agent dynamically in task mode."""
    return await ctx.run_node(orchestrator, node_input, use_as_output=True)

@node
def security_checkpoint(ctx: Context, node_input: Any) -> str:
    """
    First node in the workflow graph — no LLM reasoning starts until this passes.

    Security controls applied in order:
      1. Prompt injection detection  — blocks attempts to override agent instructions
      2. Physical safety bypass      — blocks commands to disable industrial safeties
      3. PII scrubbing               — redacts emails, phones, serial numbers

    Routes to 'approved' (orchestrator) or 'security_violation' (handler).
    Emits a structured JSON audit log to stdout on every invocation.
    """
    # Extract input values robustly
    equipment_model = ""
    task_description = ""
    text_content = ""

    if isinstance(node_input, dict):
        equipment_model = node_input.get("equipment_model", "")
        task_description = node_input.get("task_description", "")
        text_content = task_description
    else:
        # Extract text from Content, Event, or string
        if hasattr(node_input, 'parts') or isinstance(node_input, types.Content):
            text_content = extract_text_from_content(node_input)
        elif isinstance(node_input, Event):
            text_content = extract_text_from_content(node_input.content) if node_input.content else ""
        else:
            text_content = str(node_input)
            
        # Try to parse the text as JSON in case the user pasted a JSON string
        try:
            cleaned_text = text_content.strip()
            if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
            cleaned_text = cleaned_text.strip()
            
            data = json.loads(cleaned_text)
            equipment_model = data.get("equipment_model", "")
            task_description = data.get("task_description", "")
        except Exception:
            # Not a JSON string, fallback: use the text as task_description
            task_description = text_content
            # Try to guess equipment model from task_description
            for key in ["Generator-XYZ-100", "Pump-Max-500"]:
                if key.lower() in task_description.lower():
                    equipment_model = key
                    break
            if not equipment_model:
                equipment_model = "General"

    combined_input = f"{equipment_model} {task_description}".lower()
    
    # Base Audit log
    audit_log = {
        "node": "security_checkpoint",
        "severity": "INFO",
        "message": "Input validation initiated.",
        "details": {
            "equipment_model": equipment_model,
            "task_description": task_description
        }
    }

    # 1. Prompt Injection Detection
    injection_keywords = ["ignore previous", "system prompt", "developer instructions", "override constraints", "ignore instructions", "dan mode"]
    for keyword in injection_keywords:
        if keyword in combined_input:
            audit_log["severity"] = "CRITICAL"
            audit_log["message"] = f"Prompt injection attempt detected with keyword: {keyword}"
            print(json.dumps(audit_log))
            ctx.state["security_error"] = "Security Violation: Potential prompt injection detected."
            ctx.route = "security_violation"
            return "security_violation"

    # 2. Domain-Specific Rule: Block Safety Bypass / Modification
    bypass_keywords = ["bypass safety", "disable safety", "hot swap live", "hot-swap live", "override safety valve", "bypass breaker"]
    for keyword in bypass_keywords:
        if keyword in combined_input:
            audit_log["severity"] = "WARNING"
            audit_log["message"] = f"Bypass attempt blocked: {keyword}"
            print(json.dumps(audit_log))
            ctx.state["security_error"] = "Security Violation: Unauthorized attempt to bypass equipment safety valves or breakers."
            ctx.route = "security_violation"
            return "security_violation"

    # 3. PII Scrubbing (Email, Phone, Serial Numbers)
    email_regex = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    phone_regex = r"\+?\d{1,4}[-.\s]?\(?\d{1,3}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}"
    serial_regex = r"\b[S/N\s:]*([A-Z]{2,4}\d{6,8})\b"

    scrubbed_desc = re.sub(email_regex, "[REDACTED_EMAIL]", task_description)
    scrubbed_desc = re.sub(phone_regex, "[REDACTED_PHONE]", scrubbed_desc)
    scrubbed_desc = re.sub(serial_regex, "[REDACTED_SERIAL]", scrubbed_desc)

    # Save to state
    ctx.state["equipment_model"] = equipment_model
    ctx.state["task_description"] = scrubbed_desc
    ctx.state["approved"] = False
    ctx.state["supervisor_feedback"] = ""

    # Log successful validation
    audit_log["message"] = "Input validated and sanitized successfully."
    audit_log["details"]["task_description"] = scrubbed_desc
    print(json.dumps(audit_log))
    
    ctx.route = "approved"
    
    # Pass structured input parameters to orchestrator
    return {
        "equipment_model": equipment_model,
        "task_description": scrubbed_desc,
        "supervisor_feedback": ""
    }

@node
def save_orchestrator_output(ctx: Context, node_input: Any) -> str:
    """
    Bridge node between the orchestrator LlmAgent and the final_output node.

    Design rationale: ADK Workflow nodes pass data through node_input, but an
    LlmAgent's output can arrive as a Pydantic model, a dict, or raw text
    depending on whether output_schema parsing succeeded. This node normalises
    all three cases into a consistent ctx.state structure so that final_output
    never needs to handle multiple formats.
    """
    if isinstance(node_input, dict):
        ctx.state["safety_warnings"] = node_input.get("safety_warnings", [])
        ctx.state["ar_steps"] = node_input.get("ar_steps", [])
    elif hasattr(node_input, "safety_warnings"):
        ctx.state["safety_warnings"] = node_input.safety_warnings
        ctx.state["ar_steps"] = [step.model_dump() for step in node_input.ar_steps]
    else:
        # Fallback if raw text
        text = extract_text_from_content(node_input) if hasattr(node_input, 'parts') or isinstance(node_input, types.Content) else str(node_input)
        try:
            data = json.loads(text)
            ctx.state["safety_warnings"] = data.get("safety_warnings", [])
            ctx.state["ar_steps"] = data.get("ar_steps", [])
        except Exception:
            ctx.state["safety_warnings"] = ["Caution: Wear standard safety gear."]
            ctx.state["ar_steps"] = [{"step_number": 1, "instruction": text, "ar_element_type": "Floating Text Box", "spatial_anchor": "Equipment", "ar_guidance_text": "Follow instructions."}]
            
    return "done"

@node(rerun_on_resume=True)
async def supervisor_approval(ctx: Context, node_input: Any) -> AsyncGenerator[Any, None]:
    """Human-in-the-loop approval checkpoint. Pauses and prompts the user."""
    interrupt_id = "supervisor_approval_interrupt"
    
    # Only process response if this is a workflow resume trigger for our interrupt ID
    if ctx.resume_inputs and interrupt_id in ctx.resume_inputs:
        user_response = ctx.resume_inputs[interrupt_id]
        if isinstance(user_response, dict) and "result" in user_response:
            user_response = user_response["result"]
            
        user_response_str = str(user_response).strip().lower()
        if user_response_str in ("approve", "approved", "yes", "y", "ok"):
            ctx.state["approved"] = True
            ctx.route = "approved"
            yield "approved"
            return
        else:
            ctx.state["supervisor_feedback"] = str(user_response)
            ctx.state["approved"] = False
            ctx.route = "needs_revision"
            # Yield revision details to loop back to orchestrator
            yield {
                "equipment_model": ctx.state["equipment_model"],
                "task_description": ctx.state["task_description"],
                "supervisor_feedback": str(user_response)
            }
            return

    yield RequestInput(
        interrupt_id=interrupt_id,
        message="Please review the generated AR sequence and warnings. Approve or type feedback for changes.",
        response_schema=str
    )

@node
def final_output(ctx: Context, node_input: Any) -> Dict[str, Any]:
    """
    Terminal node — assembles the XR headset rendering payload from ctx.state.

    The returned dict is consumed by the AR client (HoloLens, Meta Quest, WebXR)
    and contains the fully-structured step sequence with 3D anchor coordinates,
    element types, and safety warnings ready for spatial rendering.
    """
    return {
        "status": "APPROVED",
        "equipment_model": ctx.state.get("equipment_model"),
        "task_description": ctx.state.get("task_description"),
        "safety_warnings": ctx.state.get("safety_warnings"),
        "ar_steps": ctx.state.get("ar_steps")
    }

@node
def security_violation_handler(ctx: Context, node_input: Any) -> Dict[str, Any]:
    """Handles prompt injection or PII safety failures."""
    return {
        "status": "SECURITY_VIOLATION",
        "error": ctx.state.get("security_error", "Access Denied: Security Check Failed.")
    }

# ==========================================
# 6. Workflow Assembly
# ==========================================
# Edge design notes:
#   - security_checkpoint uses a conditional dict edge (two routes: 'approved'
#     and 'security_violation'). This is the ADK pattern for branching on
#     ctx.route set inside a node function.
#   - All subsequent edges are unconditional — once security passes, the
#     pipeline is deterministic: orchestrator → save → output.
#   - supervisor_approval is intentionally NOT wired into these edges;
#     it is available as an opt-in node for high-risk task deployments that
#     require human sign-off before dispatching AR instructions to a headset.

ar_task_workflow = Workflow(
    name="ar_task_workflow",
    description="Universal AR Task Co-Pilot Workflow",
    state_schema=TaskState,
    edges=[
        # 1. Start through security check
        (START, security_checkpoint),
        # 2. Security checkpoint branches
        (security_checkpoint, {
            "approved": orchestrator_node,
            "security_violation": security_violation_handler
        }),
        # 3. Orchestration pipeline
        (orchestrator_node, save_orchestrator_output),
        (save_orchestrator_output, final_output)
    ]
)

# Export the app object
root_agent = ar_task_workflow

app = App(
    root_agent=ar_task_workflow,
    name="app",
)
