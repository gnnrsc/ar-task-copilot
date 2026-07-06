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
from mcp.server.fastmcp import FastMCP

# Create the FastMCP server
mcp = FastMCP("AR Task Co-Pilot Server")

# Mock safety threshold database
SAFETY_THRESHOLDS = {
    "Generator-XYZ-100": {
        "max_temperature_c": 70,
        "max_torque_nm": 15,
        "required_ppe": ["heavy-duty thermal gloves", "safety glasses"],
        "voltage_risk": "low"
    },
    "Pump-Max-500": {
        "max_torque_nm": 22,
        "required_ppe": ["cut-resistant gloves", "electrical boots", "safety glasses"],
        "voltage_risk": "high"
    },
    "Server-Rack-S900": {
        "max_temperature_c": 35,
        "required_ppe": ["esd wrist strap", "safety glasses"],
        "voltage_risk": "high"
    }
}

# Mock XR UI configurations
XR_UI_TEMPLATES = {
    "3D Arrow": {
        "color": "neon_green",
        "blink_rate_hz": 1.5,
        "scale": [1.0, 1.0, 1.0],
        "animation": "bounce",
        "rendering_layer": "overlay"
    },
    "Highlight": {
        "color": "bright_orange",
        "pulse_rate_hz": 2.0,
        "intensity": 2.5,
        "rendering_layer": "world"
    },
    "Floating Text Box": {
        "font_size": 14,
        "text_color": "white",
        "background_color": "semi_transparent_blue",
        "billboard_mode": "camera_facing"
    }
}

# Mock SOP database (similar to agent.py)
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
        )
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
        )
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
        )
    }
}

@mcp.tool()
def get_safety_thresholds(equipment_model: str) -> str:
    """Returns the safety limits and PPE requirements for a given equipment model.

    Args:
        equipment_model: Name/model of the equipment (e.g. 'Generator-XYZ-100', 'Pump-Max-500').
    """
    model_key = None
    for key in SAFETY_THRESHOLDS:
        if key.lower() in equipment_model.lower() or equipment_model.lower() in key.lower():
            model_key = key
            break

    if model_key:
        return json.dumps(SAFETY_THRESHOLDS[model_key])
    else:
        return json.dumps({
            "warning": f"No custom thresholds found for model '{equipment_model}'. Standard safety practices apply.",
            "max_torque_nm": 10,
            "required_ppe": ["standard work gloves", "safety glasses"]
        })

@mcp.tool()
def get_xr_ui_templates() -> str:
    """Returns the supported XR UI rendering components and visual templates for the AR engine."""
    return json.dumps(XR_UI_TEMPLATES)

@mcp.tool()
def search_sop_database(equipment_model: str) -> str:
    """Searches the database of Standard Operating Procedures (SOPs) for the specified equipment.

    Args:
        equipment_model: Name/model of the equipment (e.g. 'Generator-XYZ-100', 'Pump-Max-500').
    """
    model_key = None
    for key in MOCK_SOP_DATABASE:
        if key.lower() in equipment_model.lower() or equipment_model.lower() in key.lower():
            model_key = key
            break

    if model_key:
        return json.dumps(MOCK_SOP_DATABASE[model_key])
    else:
        return json.dumps({
            "error": f"No SOP found for model '{equipment_model}' in the database."
        })

if __name__ == "__main__":
    mcp.run()
