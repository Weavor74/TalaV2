# File: `settings-summary-workflow.json`

## Overview

A declarative workflow definition detailing the sequential steps for summarizing user settings.

## File Contents

```json
{
  "flow_id": "settings-summary-workflow",
  "name": "Settings Concise Summary Workflow",
  "description": "Automatically triggered when the user asks about their settings, configuration, or system preferences. Acquires all active settings nodes from the HTML Neural Net and produces a short, concise summary of the current agent configuration including inference providers, guardrails, workflows, git, storage, database, runtime, auth, firewall, and search settings.",
  "tags": ["settings", "configuration", "preferences", "system", "inference", "agent", "guardrails", "workflow", "summary"],
  "trigger_keywords": ["settings", "config", "configuration", "preferences", "tell me about my settings", "what are my settings", "show settings", "system settings", "agent settings", "inference settings"],
  "nodes": [
    {
      "id": "start-trigger",
      "type": "trigger",
      "label": "Query Settings Trigger",
      "config": {
        "description": "Triggered when user requests settings information"
      },
      "connections": {
        "onSuccess": "acquire-settings"
      }
    },
    {
      "id": "acquire-settings",
      "type": "custom_script",
      "label": "Acquire Settings Info",
      "config": {
        "code": "result = 1 + 1",
        "description": "Acquire system and node settings from HTML Neural Net"
      },
      "connections": {
        "onSuccess": "summarize-settings"
      }
    },
    {
      "id": "summarize-settings",
      "type": "custom_script",
      "label": "Summarize Settings Concise",
      "config": {
        "code": "result = 2 + 2",
        "description": "Formulate a short, concise summary of the active settings nodes"
      },
      "connections": {
        "onSuccess": "output-summary"
      }
    },
    {
      "id": "output-summary",
      "type": "output",
      "label": "Concise Settings Output",
      "config": {
        "description": "Output the final concise summary of system settings"
      },
      "connections": {}
    }
  ]
}
```
