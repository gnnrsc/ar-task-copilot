# AR Integration Guide
## How to Render the Agent Output in Your XR Engine

The **Universal AR Task Co-Pilot** produces a structured JSON payload that any XR runtime can consume.
This guide covers the output schema, how to call the API, how to track the machine in physical space,
and how to render the step-by-step guidance in Unity, WebXR, or Android XR.

---

## 1. Output Schema

Every successful agent call returns this JSON structure:

```json
{
  "status": "APPROVED",
  "equipment_model": "Server-Rack-S900",
  "task_description": "Replace failed PSU module.",
  "safety_warnings": [
    "High Voltage Risk: ensure lockout/tagout before handling.",
    "Burn Hazard: PSU exhaust > 35 °C — wait 5 min if over limit.",
    "ESD Protection: wear wrist strap and safety glasses."
  ],
  "ar_steps": [
    {
      "step_number": 1,
      "instruction": "Locate failed PSU module indicated by red LED.",
      "ar_element_type": "Highlight",
      "spatial_anchor": "PSU Module Front Panel",
      "ar_guidance_text": "Look for the red LED — this is the failed unit"
    },
    {
      "step_number": 2,
      "instruction": "Press the release latch and slide the PSU outward.",
      "ar_element_type": "3D Arrow",
      "spatial_anchor": "PSU Release Latch",
      "ar_guidance_text": "Press latch -> slide OUT"
    }
  ],
  "summary": "Replace failed PSU on Server-Rack-S900 safely."
}
```

| Field | Type | Description |
|---|---|---|
| `status` | `string` | `"APPROVED"` = safe to render |
| `safety_warnings` | `string[]` | Show as a modal **before** starting |
| `ar_steps` | `object[]` | Ordered steps, one per overlay element |
| `ar_steps[].instruction` | `string` | Full sentence for TTS narration |
| `ar_steps[].ar_element_type` | `string` | Which overlay to spawn (see below) |
| `ar_steps[].spatial_anchor` | `string` | Named component on the machine |
| `ar_steps[].ar_guidance_text` | `string` | Short HUD label in the operator's FOV |

**AR element types:**

| `ar_element_type` | Render as |
|---|---|
| `"3D Arrow"` | Animated directional arrow pointing at the anchor |
| `"Highlight"` | Pulsing bounding-box or outline around the component |
| `"Floating Text Box"` | Billboard text panel floating above the anchor |

---

## 2. Calling the Agent API

Send `equipment_model` + `task_description`; get back the full JSON above.

```http
POST http://localhost:8000/run_sse
Content-Type: application/json

{
  "app_name": "app",
  "user_id": "technician-42",
  "session_id": "session-001",
  "new_message": {
    "role": "user",
    "parts": [{ "text": "{\"equipment_model\": \"Server-Rack-S900\", \"task_description\": \"Replace failed PSU module.\"}" }]
  },
  "streaming": false
}
```

> **session_id**: generate a UUID per task and reuse it for all subsequent calls in the same session
> (mid-task updates, anomaly reports). This gives the agent full conversation context.

**Parsing the response** — the structured payload is in `.output` (or the root object if `.output` is absent):

```javascript
const data = await res.json();
const payload = data.output ?? data;
```

```python
event = json.loads(line[6:])
if isinstance(event.get("output"), dict) and "ar_steps" in event["output"]:
    return event["output"]
```

---

## 3. Machine Identification & Pose Estimation

Before the app can place overlays, the headset must resolve two things:

1. **Which machine is this?** — loads the right anchor registry
2. **Where exactly is it in 3D space?** — anchors overlays to real components

This is handled by a **tracking layer** independent of the agent. Choose one approach:

| Approach | Setup | 3D model? | Offline? | Best for |
|---|---|---|---|---|
| **A — QR / ArUco marker** | Very low | No | Yes | MVPs, development |
| **B — Image target** | Low | No | Yes | Clean environments, no sticker allowed |
| **C — Model target (CAD)** | Medium | Yes (CAD/mesh) | Yes | Production, sub-cm accuracy |
| **D — Cloud spatial anchors** | Low (one-time) | No | No | Multi-user, persistent installations |

### A — QR / ArUco Marker *(recommended starting point)*
Stick a marker on the machine. The headset reads the ID, uses the marker as **world origin**, and looks up all anchor offsets relative to it. No 3D model needed — works on WebXR, Unity ARFoundation, Vuforia, HoloLens.

### B — Image Target
A reference photo of a distinctive panel acts as a natural marker. The AR SDK (Vuforia, ARCore Image Tracking, ARKit) matches it in the live camera and returns the pose. No sticker needed; sensitive to lighting changes.

### C — Model Target / CAD-based Tracking
Provide a CAD or mesh file. The SDK (Vuforia Model Targets, HoloLens DNN Object Tracker) continuously matches the model's silhouette to the camera to estimate **full 6-DOF pose**. The model is a **recognition template only** — it is never rendered visibly. Best accuracy as the operator moves around.

### D — Cloud Spatial Anchors
A setup technician manually places anchors by tapping real surfaces. Anchors persist in the cloud (Azure Spatial Anchors, ARCore Cloud Anchors) or on-device (ARKit WorldMap). All later operators find the same anchors automatically.

**In all cases the result is the same:** a world-space origin for the machine, which the Anchor Registry (section 4) uses to resolve overlay positions.

```
Tracking layer   ->  world-space origin of the machine
     +
Anchor registry  ->  named offsets per component
     =
World position where each AR overlay is spawned
```

> **Recommended path**: start with Approach A (QR marker) to validate the full pipeline end-to-end, then upgrade to Approach C (Model Target) when CAD files are available.

---

## 4. Spatial Anchor Registry

The `spatial_anchor` strings from the agent (e.g. `"PSU Module Front Panel"`) are **natural-language keys**
your app resolves to real-world transforms.
Store one JSON config file per equipment model and load it at session start, before calling the agent.

```
Equipment: Server-Rack-S900
+-- "PSU Module Front Panel"   -> offset (0.12, 0.45, 0.00) from origin
+-- "PSU Release Latch"        -> offset (0.10, 0.44, 0.05) from origin
+-- "PSU Bay Slot"             -> offset (0.12, 0.44, -0.02) from origin
+-- "Server Power Switch"      -> offset (0.00, 0.90, 0.00) from origin
```

The agent's anchor names always match the equipment's known component names from the SOP database,
so no dynamic resolution is required at runtime.

---

## 5. Rendering Overlays (Unity C#)

The agent returns **all steps at once**. Render one at a time using the step tracker pattern below.
The same class handles first call, step advancement, voice input, and mid-task updates.

```csharp
public class ARSessionManager : MonoBehaviour
{
    private string equipmentModel;
    private string sessionId;
    private int currentStepIndex = 0;
    private List<ArStep> arSteps = new();

    // 1. Start task
    public async void StartTask(string equipment, string taskDesc)
    {
        equipmentModel   = equipment;
        sessionId        = System.Guid.NewGuid().ToString();
        currentStepIndex = 0;

        var payload = await CallAgent(taskDesc);
        arSteps = payload.ar_steps.ToList();

        // Show safety warnings first; advance to step 0 on dismiss
        SafetyWarningPanel.Show(payload.safety_warnings, onDismiss: RenderCurrentStep);
    }

    // 2. Render the active step
    private void RenderCurrentStep()
    {
        ClearAllOverlays();
        var step = arSteps[currentStepIndex];

        Transform anchor = SpatialAnchorRegistry.Find(step.spatial_anchor);
        if (anchor == null) return;

        switch (step.ar_element_type)
        {
            case "3D Arrow":          Instantiate(ArrowPrefab, anchor.position, anchor.rotation); break;
            case "Highlight":         Instantiate(HighlightPrefab, anchor.position, Quaternion.identity); break;
            case "Floating Text Box": SpawnTextBox(anchor.position, step.ar_guidance_text); break;
        }

        TextToSpeech.Speak(step.instruction);
    }

    // 3. Advance step (button / gaze dwell / voice "next")
    public void AdvanceStep()
    {
        currentStepIndex = Mathf.Min(currentStepIndex + 1, arSteps.Count - 1);
        RenderCurrentStep();
    }

    // 4. Handle voice input
    private static readonly string[] AdvanceKeywords = { "next", "done", "avanti", "fatto" };

    public async void OnVoiceInput(string voiceText)
    {
        // Keyword -> advance locally, no agent call
        if (Array.Exists(AdvanceKeywords, kw => voiceText.ToLower().Contains(kw)))
        {
            AdvanceStep();
            return;
        }

        // Anomaly / question -> send to agent with step context
        string msg = $"[Current step: {currentStepIndex + 1} of {arSteps.Count}] {voiceText}";
        var revised = await CallAgent(msg);
        ApplyRevisedSteps(revised.ar_steps);
    }

    // 5. Apply revised steps after anomaly
    private void ApplyRevisedSteps(ArStep[] newSteps)
    {
        arSteps = newSteps.ToList();
        currentStepIndex = Mathf.Min(currentStepIndex, arSteps.Count - 1);
        RenderCurrentStep();
    }

    // Agent call -- always reuses same sessionId
    private async Task<CopilotPayload> CallAgent(string messageText)
    {
        string body = JsonConvert.SerializeObject(new {
            app_name   = "app",
            user_id    = "tech-001",
            session_id = sessionId,
            new_message = new {
                role  = "user",
                parts = new[] { new { text = JsonConvert.SerializeObject(new {
                    equipment_model  = equipmentModel,
                    task_description = messageText
                })}}
            },
            streaming = false
        });

        using var req = new UnityWebRequest("http://localhost:8000/run_sse", "POST");
        req.uploadHandler   = new UploadHandlerRaw(Encoding.UTF8.GetBytes(body));
        req.downloadHandler = new DownloadHandlerBuffer();
        req.SetRequestHeader("Content-Type", "application/json");
        await req.SendWebRequest();

        return JsonConvert.DeserializeObject<CopilotPayload>(req.downloadHandler.text);
    }
}
```

---

## 6. Rendering Overlays (WebXR / JavaScript)

```javascript
const session = { equipmentModel: "", sessionId: null, currentStepIndex: 0, arSteps: [] };

// 1. Start task
async function startTask(equipmentModel, taskDescription) {
  session.equipmentModel   = equipmentModel;
  session.sessionId        = crypto.randomUUID();
  session.currentStepIndex = 0;

  const data = await callAgent(taskDescription);
  session.arSteps = data.ar_steps;
  showWarningModal(data.safety_warnings, renderCurrentStep);
}

// 2. Render active step
function renderCurrentStep() {
  clearAROverlays();
  const step   = session.arSteps[session.currentStepIndex];
  const anchor = document.querySelector(`[data-anchor="${step.spatial_anchor}"]`);
  if (!anchor) return;

  switch (step.ar_element_type) {
    case "3D Arrow":          spawnArrow(anchor); break;
    case "Highlight":         spawnHighlight(anchor); break;
    case "Floating Text Box": spawnTextBox(anchor, step.ar_guidance_text); break;
  }
  speakText(step.instruction);
}

// 3. Advance step
function advanceStep() {
  session.currentStepIndex = Math.min(session.currentStepIndex + 1, session.arSteps.length - 1);
  renderCurrentStep();
}

// 4. Voice input
const ADVANCE_KEYWORDS = ["next", "done", "avanti", "fatto"];

function startVoiceInput() {
  const recognition = new webkitSpeechRecognition();
  recognition.lang = "it-IT";
  recognition.continuous = false;

  recognition.onresult = async (event) => {
    const text = event.results[0][0].transcript.toLowerCase();

    if (ADVANCE_KEYWORDS.some(kw => text.includes(kw))) {
      advanceStep();  // local -- no agent call
      return;
    }

    const msg = `[Current step: ${session.currentStepIndex + 1} of ${session.arSteps.length}] ${text}`;
    const revised = await callAgent(msg);
    applyRevisedSteps(revised.ar_steps);
  };
  recognition.start();
}

// 5. Apply revised steps after anomaly
function applyRevisedSteps(newSteps) {
  session.arSteps = newSteps;
  session.currentStepIndex = Math.min(session.currentStepIndex, newSteps.length - 1);
  renderCurrentStep();
}

// Agent call -- always same sessionId
async function callAgent(messageText) {
  const res = await fetch("http://localhost:8000/run_sse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      app_name: "app", user_id: "tech-001", session_id: session.sessionId,
      new_message: { role: "user", parts: [{ text: JSON.stringify({
        equipment_model: session.equipmentModel, task_description: messageText
      })}] },
      streaming: false
    })
  });
  const data = await res.json();
  return data.output ?? data;
}
```

---

## 7. Rendering Overlays (Android XR / Smart Glasses)

Android XR is Google's platform for spatial computing on Android devices, including
smart glasses such as **Samsung Galaxy Glass** (Project Moohan) and other Android XR
certified headsets. It uses the **Jetpack XR SDK** (Kotlin) on top of ARCore — a
completely different stack from Unity or WebXR.

The agent's JSON output is identical; only the rendering layer changes.

> [!IMPORTANT]
> Android XR and the Jetpack XR SDK are in **active development** (SDK versions may
> change rapidly). Always check the [official Jetpack XR release notes](https://developer.android.com/jetpack/androidx/releases/xr)
> before starting an integration.

---

### Stack overview

```
Agent API  -->  Retrofit / OkHttp (Kotlin coroutines)
                      |
                      v
            ar_steps[] JSON payload
                      |
                      v
         Jetpack XR Session + SpatialEnvironment
                      |
              +-------+-------+
              |               |
       AnchorEntity     Compose in 3D (SpatialPanel)
       (3D Arrow /      (Floating Text Box /
        Highlight)       HUD guidance text)
                      |
                      v
              ARCore tracking  <--  camera feed
              (QR / Image / Model target)
```

---

### Step 1 — Call the agent (Kotlin + Coroutines)

```kotlin
// build.gradle: implementation("com.squareup.retrofit2:retrofit:2.x")
data class AgentRequest(
    val app_name: String,
    val user_id: String,
    val session_id: String,
    val new_message: Message,
    val streaming: Boolean = false
)
data class Message(val role: String, val parts: List<Part>)
data class Part(val text: String)

suspend fun fetchARGuidance(
    equipmentModel: String,
    taskDescription: String,
    sessionId: String
): JsonObject {
    val body = AgentRequest(
        app_name = "app",
        user_id = "tech-001",
        session_id = sessionId,
        new_message = Message(
            role = "user",
            parts = listOf(Part(Gson().toJson(mapOf(
                "equipment_model" to equipmentModel,
                "task_description" to taskDescription
            ))))
        )
    )
    val response = agentApiService.runAgent(body)  // Retrofit call
    return response.body()?.get("output")?.asJsonObject ?: response.body()!!
}
```

---

### Step 2 — Resolve anchors with ARCore

ARCore is the tracking backbone on Android XR. Use it with a QR/ArUco marker
or an Image Target to obtain the machine's world-space pose, then compute
anchor positions as offsets.

```kotlin
// After ARCore detects the QR/image marker:
val session = Session(context)
val frame = session.update()

frame.getUpdatedTrackables(AugmentedImage::class.java).forEach { image ->
    if (image.trackingState == TrackingState.TRACKING) {
        val machinePose = image.centerPose          // world-space origin
        val anchorPose  = machinePose.compose(
            Pose.makeTranslation(0.12f, 0.45f, 0.0f)  // offset from registry
        )
        val anchor = session.createAnchor(anchorPose)
        spawnOverlay(anchor, step)                  // see step 3
    }
}
```

---

### Step 3 — Spawn overlays with Jetpack XR

Jetpack XR renders 3D content via `SpatialEnvironment` and UI panels via
`SpatialPanel` (Jetpack Compose extended to 3D space).

```kotlin
// Inside an XrActivity or XrFragment
val xrSession = XrSession(this)
val env       = xrSession.spatialEnvironment

fun spawnOverlay(anchor: Anchor, step: ArStep) {
    val entity = AnchorEntity(xrSession, anchor)

    when (step.ar_element_type) {
        "3D Arrow" -> {
            // Load a glTF arrow model and attach it to the entity
            val arrowModel = GltfModel.load(xrSession, Uri.parse("file:///android_asset/arrow.glb"))
            entity.addComponent(ModelComponent(arrowModel))
        }
        "Highlight" -> {
            // Attach a pulsing bounding-box shader component
            entity.addComponent(HighlightComponent(color = Color.Green, pulseHz = 2f))
        }
        "Floating Text Box" -> {
            // Compose UI panel anchored in world space
            val panel = SpatialPanel(xrSession, widthDp = 300, heightDp = 120)
            panel.setContent { Text(step.ar_guidance_text) }
            panel.setPose(entity.pose.compose(Pose.makeTranslation(0f, 0.3f, 0f)))
            env.addSpatialPanel(panel)
        }
    }

    env.addEntity(entity)
    TextToSpeech(this) { it.speak(step.instruction, QUEUE_FLUSH, null, null) }
}
```

> **Note**: `HighlightComponent` and exact `SpatialPanel` APIs are illustrative —
> check the current Jetpack XR alpha/beta for the actual class names, as the SDK
> is evolving rapidly.

---

### Step 4 — Voice input on Android XR

Use Android's built-in `SpeechRecognizer` (no dependency needed):

```kotlin
val recognizer = SpeechRecognizer.createSpeechRecognizer(context)
val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
    putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
    putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")  // or "it-IT"
}

recognizer.setRecognitionListener(object : RecognitionListener {
    override fun onResults(results: Bundle) {
        val text = results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: return
        if (ADVANCE_KEYWORDS.any { text.lowercase().contains(it) }) {
            advanceStep()
        } else {
            // Anomaly report -- send to agent with step context
            val msg = "[Current step: ${currentStepIndex + 1} of ${arSteps.size}] $text"
            lifecycleScope.launch { sendMidTaskUpdate(msg) }
        }
    }
    // ... other required overrides
})
recognizer.startListening(intent)
```

**Offline alternative**: bundle OpenAI Whisper via ONNX Runtime for Android
(`com.microsoft.onnxruntime:onnxruntime-android`) — no internet required.

---

### Android XR & Smart Glasses — Known Limitations

> [!WARNING]
> These limitations are significant. Evaluate each carefully before committing to
> an Android XR integration.

- **SDK immaturity**: The Jetpack XR SDK is currently in alpha/beta. APIs are
  unstable and may change between releases. Production deployment is risky until
  the SDK reaches stable.

- **Device fragmentation**: Android XR runs on multiple hardware form factors
  (full headsets, slim smart glasses, companion-phone setups). FOV, compute
  power, and available sensors vary significantly between devices — your overlay
  layout must adapt dynamically.

- **Limited FOV on slim glasses**: Smart glasses typically have a 35–50° FOV
  (vs 90°+ on HoloLens / Quest). Overlays placed outside this cone are
  invisible. Keep all AR elements close to the operator's centre of gaze.

- **Agent latency on constrained hardware**: The 3–8 s agent response time is
  even more painful on glasses with limited battery. A spinning indicator
  occupying the small FOV is disruptive — consider pre-fetching guidance while
  the operator is still navigating to the machine.

- **No persistent world anchors without internet**: ARCore Cloud Anchors (Approach D
  in §3) require connectivity. On factory floors with poor Wi-Fi, anchors may
  fail to resolve. Use local QR markers (Approach A) as the reliable fallback.

- **`SpeechRecognizer` requires network by default**: Android's built-in STT
  sends audio to Google's cloud. In offline environments use Whisper via
  ONNX Runtime (larger APK, ~150 MB model) or a local Whisper server on the
  companion phone.

- **glTF asset pipeline**: 3D arrow and highlight assets must be packaged as
  `.glb` files inside the APK's `assets/` folder. There is no equivalent of
  Unity's prefab system — all models must be pre-authored and bundled at build
  time.

- **Gaze dwell as step-advance input**: on slim glasses with no physical
  buttons, gaze dwell is the primary hands-free alternative to voice. Android
  XR Eye Tracking API provides gaze data, but accuracy on low-cost glasses may
  be insufficient for precise dwell targets.

---

## 8. Step Advancement Strategies

The agent returns all steps at once. Your app decides when to move from one to the next.
The agent is **not re-called** for normal step progression — only for anomalies (section 9).

| Strategy | How | Best for |
|---|---|---|
| **Button / gaze dwell** | Operator taps a button or fixes gaze for N seconds | Any platform — recommended default |
| **Voice keyword** | STT detects "next" / "avanti" / "done" -> `advanceStep()` locally | Hands-free environments |
| **Auto after TTS** | `utterance.onend` / `WaitUntil(!IsSpeaking)` -> `advanceStep()` | Guided, pace-controlled walkthroughs |
| **Sensor triggered** | RFID / GPIO / CV model -> WebSocket event -> `advanceStep()` | Fully automated QA lines |

> **Recommended**: Button/gaze as baseline + voice keyword layered on top.

---

## 9. Mid-Task Updates (Anomalies)

If something unexpected happens, send a new message to the **same session**.
The agent reads the full context and returns a revised `ar_steps[]` array.

```python
body = {
    "app_name": "app",
    "user_id": "tech-001",
    "session_id": "s-001",       # SAME session as the original call
    "new_message": {
        "role": "user",
        "parts": [{"text": "[Current step: 3 of 6] WARNING: temperature just jumped to 52 C"}]
    },
    "streaming": False
}
```

The agent will:
1. Re-read the session context (equipment model, original task, current step)
2. Re-check the safety threshold via MCP (`get_safety_thresholds`)
3. Return a **revised `ar_steps[]`** with a corrective action inserted

Your app replaces the current overlay queue via `applyRevisedSteps()` (shown in sections 5 and 6).

---

## 10. Voice Input — STT Options

The agent accepts plain text regardless of source. You need an STT layer on the client side.

| STT option | Platform | Offline | Setup |
|---|---|---|---|
| Web Speech API | Browser / WebXR | No | Zero — built-in Chrome/Edge |
| Windows DictationRecognizer | HoloLens 2 | Yes | Built into Unity |
| Meta Voice SDK (Wit.ai) | Meta Quest | No | Free Wit.ai account |
| OpenAI Whisper | Any (Python backend) | Yes | `pip install openai-whisper` |
| Azure Cognitive Services | Any | No | Azure subscription |

- **HoloLens 2**: use `UnityEngine.Windows.Speech.DictationRecognizer`
- **Meta Quest**: use Meta Voice SDK and wire `OnVoiceResponse` to your `OnVoiceInput` handler
- **Offline factory floors**: run Whisper on a companion PC and relay transcriptions to the headset

---

## 11. Known Limitations

Understanding these constraints is essential before integrating into a production XR experience.

### Agent & API

- **Latency (3–8 s)**: the agent pipeline (security check -> orchestrator -> sub-agents -> MCP tools) is not real-time. Show a loading indicator while waiting; do not call the agent per-frame.
- **No cross-session memory**: each new `session_id` starts from zero. If the operator closes and re-opens the app, the agent has no memory of the previous task. The XR app must save and restore session state locally.
- **LLM non-determinism**: the same prompt may occasionally produce slightly different `spatial_anchor` names. Always validate that returned anchor names exist in the local registry before rendering; log and skip missing ones.
- **Network dependency**: the agent requires a live connection to the deployment endpoint and to the MCP safety-threshold service. Plan a cached-steps fallback or graceful degradation for offline scenarios.

### Spatial Anchoring & Tracking

- **Anchor registry is manually built**: the agent generates anchor names from the SOP database, but someone must physically measure and record the offsets for every equipment model before first use.
- **Tracking loss**: QR/Image/Model trackers lose pose if the machine is occluded or lighting changes. Build a "tracking lost" UX state that pauses guidance until tracking recovers.
- **Model Target accuracy degrades at distance**: Vuforia Model Targets work best within 1–3 m. At larger distances, pose estimation becomes unreliable.
- **Cloud Spatial Anchors require internet**: Approach D (section 3) cannot be used in offline environments.

### Step Advancement

- **No ground-truth completion verification**: the app has no way to confirm the operator actually completed an action. Strategies A–C are intent-based. Only Strategy D (sensor) provides physical verification.
- **Voice keyword collisions**: advance keywords ("done", "ok", "next") may conflict with legitimate anomaly reports. Tune the keyword list carefully for the operating language and vocabulary.

### Platform-Specific

- **WebXR CORS**: calling `localhost:8000` from a WebXR session requires proper CORS headers on the agent server, and HTTPS if served from a real domain.
- **HoloLens DictationRecognizer** requires internet (Windows cloud STT). Use Whisper on a companion PC for offline support.
- **Android XR / Smart Glasses**: Jetpack XR SDK is alpha/beta — APIs are unstable. Limited FOV, constrained compute, and fragmented device hardware add significant integration risk. See §7 for a full breakdown of limitations.

---

## 12. Summary

```
[Optional] Operator speaks  ->  STT layer  ->  plain text
                                                    |
                                       POST /run_sse (same session_id)
                                                    |
                                            Agent Output JSON
                                                    |
                           +------------------------+
                           |                        |
                  safety_warnings[]           ar_steps[]
                  Show modal BEFORE           One step at a time
                  starting                   +-- ar_element_type  -> spawn overlay
                                             +-- spatial_anchor   -> world position (via registry)
                                             +-- ar_guidance_text -> HUD label
                                             +-- instruction      -> TTS narration
                                                    |
                                         Operator advances step
                                         (button / voice / TTS / sensor)
                                                    |
                                         Anomaly? -> re-call agent
                                                  -> applyRevisedSteps()
```

The agent is **engine-agnostic** and **input-agnostic** by design.
It produces structured data from any text source; your XR runtime renders it.
The spatial tracking layer and step advancement logic live entirely on the client side.
