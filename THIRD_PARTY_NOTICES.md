# Third-party notices

Ouroboros has **no runtime dependencies**: it is standard-library Python only, and nothing below is bundled or
redistributed in this repository. These are things you may install alongside it, each under its own license.

| Component | How it is used | License |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) | Local model server that Ouroboros calls over HTTP (`127.0.0.1`) | MIT |
| `qwen2.5:3b` (default planner model) | Pulled by you via Ollama; used only to draft plans | **Qwen Research License** (non-commercial research); other Qwen2.5 sizes are Apache-2.0. Confirm on the model page before commercial use |
| [laya-guard](https://github.com/chetas1208/laya-guard) | Optional command/prompt classifier, called as a separate executable | MIT |
| [Cursor](https://cursor.com) | The editor whose hooks, rules and skills this integrates with | Proprietary; governed by Cursor's own terms |

Product and company names are trademarks of their owners and are used only to describe compatibility.
