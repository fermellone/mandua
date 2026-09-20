# Mandu'a — TypeSafe AI Jev Adapter Harness

This directory provides an adapter harness integrating **Mandu'a** (`mandua-memory`) with **Jev**, the System One model developed by [TypeSafe AI](https://typesafe.ai/).

---

## 1. End-to-End System Architecture

```mermaid
flowchart TB
    subgraph AgentLoop["Agent Loop / Caller"]
        Intent["Natural Language Intent / Task Description\n(e.g., 'Why was the irrigation threshold changed to 35%?')"]
    end

    subgraph JevEngine["TypeSafe Jev: System 1 Decision Engine"]
        direction TB
        StatePrep["State Preparation (JSON payload)"]
        JevAPI["Typed Inferences (70–500 ms)\n• ChoiceQuestion: operation selection\n• NoulQuestion: is_mutation probability\n• NoulQuestion: requires_human_approval\n• ScoreQuestion: operational risk (0–10)"]
        StatePrep --> JevAPI
    end

    subgraph PolicyGate["3-Tier Policy Gate & Guardrails"]
        direction TB
        AutoExec["Tier 1: AUTO_EXECUTE\n(Read ops with confidence >= 0.80)"]
        ReqConfirm["Tier 2: REQUIRE_CONFIRMATION\n(Mutations or moderate confidence)"]
        Escalate["Tier 3: ESCALATE_TO_HUMAN\n(Risk score >= 7.0 or confidence < 0.50)"]
    end

    subgraph ManduaCore["Mandu'a Core (Deterministic Git Evidence)"]
        direction TB
        MemService["MemoryService.open()"]
        GitOps["14 Bounded Operations:\nwhy, timeline, status, compare,\ndecision, origin, checkpoint, etc."]
        GitRepo[("Immutable Git Objects,\nDAG, Trailers & Notes")]
        MemService --> GitOps --> GitRepo
    end

    subgraph OutputContract["Contract & Deliverable"]
        Result["HarnessResult (JSON)\n• Jev typed decision & confidence\n• Git provenance & verifiable evidence\n• Operation audit log"]
    end

    Intent --> StatePrep
    JevAPI --> PolicyGate
    AutoExec -->|"Direct execution"| MemService
    ReqConfirm -->|"Dry-run preview (requires --apply)"| MemService
    Escalate -->|"Execution halted"| Result
    GitRepo --> Result
```

---

## 2. Decision Tree & Policy Enforcement

Jev does not generate arbitrary prose tokens; it evaluates typed questions with calibrated probability distributions and confidence values (`confidence`: 0.0 to 1.0):

```mermaid
flowchart TD
    Start["Input: Agent intent or task state"] --> Route["Jev Choice: Select 1 of 14 Mandu'a operations"]
    Route --> EvalRisk["Jev Noul & Score: Evaluate mutation & risk metrics"]
    
    EvalRisk --> CheckRisk{"Risk score >= 7.0 OR\nconfidence < 0.50?"}
    CheckRisk -- Yes --> Tier3["🔴 ESCALATE_TO_HUMAN\n• Execution halted by safety policy\n• Escalates to human operator"]
    
    CheckRisk -- No --> CheckMut{"Is this operation\na repository mutation?"}
    CheckMut -- Yes --> CheckConf{"Confidence >= 0.80?"}
    CheckConf -- No --> Tier2["🟡 REQUIRE_CONFIRMATION\n• Generates preview only (dry-run)\n• Requires explicit --apply to write"]
    CheckConf -- Yes --> CheckApply{"Was --apply provided?"}
    CheckApply -- Yes --> ExecMut["🟢 Apply mutation to Git"]
    CheckApply -- No --> Tier2
    
    CheckMut -- No --> Tier1["🟢 AUTO_EXECUTE\n• Bounded read query\n• Returns verifiable MemoryResult immediately"]
```

---

## 3. Sequence Flow: Provenance Query (`why`)

How an automated agent reconstructs recorded Git provenance through Jev routing:

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Agent / User
    participant Harness as ManduaJevHarness
    participant Jev as TypeSafe Jev (System 1)
    participant Mandua as Mandu'a (MemoryService)
    participant Git as Git Repository

    Agent->>Harness: evaluate_and_run("Why was line 5 of rules.md changed?")
    Harness->>Jev: POST /v1/systemone (state, questions: Choice, Noul, Score)
    Note over Jev: Non-autoregressive decision (~100 ms)
    Jev-->>Harness: {operation: 'why', confidence: 0.95, is_mutation: 0.05, risk: 1.0}
    
    Note over Harness: Policy Gate: AUTO_EXECUTE approved
    Harness->>Mandua: service.why(path="knowledge/rules.md", line=5)
    Mandua->>Git: git blame + inspect commit trailers + refs/notes/review
    Git-->>Mandua: Commit OID, Decision-ID: DEC-IRR-001, Author, Date
    Mandua-->>Harness: MemoryResult(observed, evidence, confidence='high')
    Harness-->>Agent: HarnessResult (Typed decision + Immutable Git evidence)
```

---

## 4. Sequence Flow: Safe Mutation (`checkpoint`)

Enforcing Mandu'a's two-phase preview and apply write guarantee:

```mermaid
sequenceDiagram
    autonumber
    actor Agent as Agent / User
    participant Harness as ManduaJevHarness
    participant Jev as TypeSafe Jev (System 1)
    participant Mandua as Mandu'a (MemoryService)

    Agent->>Harness: evaluate_and_run("Record verified irrigation threshold", apply=False)
    Harness->>Jev: Evaluate routing and risk questions
    Jev-->>Harness: {operation: 'checkpoint', is_mutation: 0.95, confidence: 0.78}
    
    Note over Harness: Policy Gate: REQUIRE_CONFIRMATION (Preview only)
    Harness->>Mandua: service.checkpoint(request, apply=False)
    Mandua-->>Harness: MemoryResult(applied=False, changes=[PlannedChange(...)])
    Harness-->>Agent: Returns preview; prompts for explicit apply
    
    Agent->>Harness: evaluate_and_run("Confirm", apply=True)
    Harness->>Mandua: service.checkpoint(request, apply=True)
    Mandua-->>Harness: MemoryResult(applied=True, new_oid="d3b073...")
    Harness-->>Agent: Semantic commit recorded cleanly
```

---

## 5. Hypothesis Comparison & Scoring (`compare`)

Comparing two divergent Git branches and using Jev as a quantitative evaluator:

```mermaid
flowchart LR
    subgraph GitBranches["Git Branches"]
        B1["hypothesis/sensor-threshold"]
        B2["hypothesis/fixed-schedule"]
    end

    subgraph ManduaCompare["mandua.compare()"]
        MergeBase["Compute Merge Base"]
        ExclusiveCommits["Exclusive Commits"]
        DiffState["State Diff & Patch Correspondence"]
    end

    subgraph JevScore["TypeSafe Jev Evaluator"]
        ChoiceQ["Winner (Choice)"]
        ScoreL["Score Left Branch (0–10)"]
        ScoreR["Score Right Branch (0–10)"]
    end

    subgraph Outcome["Outcome"]
        Pick["Selected: hypothesis/sensor-threshold\nConfidence: 0.88\nScores: 9.2 vs 6.1"]
    end

    B1 & B2 --> ManduaCompare
    ManduaCompare --> JevScore
    JevScore --> Outcome
```

---

## 6. Question Schema & Typed Outputs

| Question ID | Question Type | Input Mapping | Expected Typed Output |
| :--- | :--- | :--- | :--- |
| `operation` | `Choice` | Natural-language intent / program state | Exact Mandu'a operation name (`why`, `status`, `timeline`, etc.) |
| `is_mutation` | `Noul` | Write/commit indicators in intent | Mutation probability ($0.0 \dots 1.0$) |
| `requires_human_approval` | `Noul` | Risk patterns (`delete`, `force`, `rewrite`) | Critical risk probability ($0.0 \dots 1.0$) |
| `risk_score` | `Score` | Potential destructiveness of action | Quantitative scale ($0.0 \dots 10.0$) |

---

## 7. Python API Usage

```python
from pathlib import Path
from adapters.jev import ManduaJevHarness, TypeSafeJevClient

# Initialize client (uses TYPESAFE_API_KEY environment variable or offline mock)
client = TypeSafeJevClient()
harness = ManduaJevHarness(Path("/path/to/git-repo"), client=client)

# 1. Route intent and execute
result = harness.evaluate_and_run("Why was the irrigation threshold changed to 35%?")
print(result.route.operation)  # "why"
print(result.route.confidence)  # e.g. 0.95
print(result.memory_result.answer)  # Git provenance evidence from Mandu'a

# 2. Compare hypotheses and score candidates
evaluation = harness.compare_hypotheses_with_scoring(
    left="hypothesis/sensor-threshold",
    right="hypothesis/fixed-schedule",
    criteria="Water conservation with low sensor failure risk",
)
print(f"Winner: {evaluation['preferred_hypothesis']}")
print(f"Confidence: {evaluation['confidence']}")
```

---

## 8. CLI Usage

```console
# Query repository status or history through Jev routing
python3 -m adapters.jev --repo . "Show me the recent commit timeline for knowledge/rules.md" --path knowledge/rules.md

# Output in machine-readable JSON format
python3 -m adapters.jev --repo . "Check working tree status" --format json
```
