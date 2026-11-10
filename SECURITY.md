# Security Policy

## Threat model

Ouroboros runs **on your machine, with your permissions**, next to a coding agent. It has two jobs that touch
security:

1. It executes **verification commands** (`shell=True`, in your workspace, 900 s timeout).
2. It can **gate the agent's shell commands** through an optional classifier ([laya-guard](https://github.com/chetas1208/laya-guard)).

What protects you:

| Layer | What it does | What it does *not* do |
|---|---|---|
| Model output is a **hint** | A command proposed by the planner model is stored as `hint` and **never executed**. Only a command the working agent adopts with `ouro set-verify` runs. | It does not make an adopted command safe. |
| Denylist | Refuses `git push/commit/reset --hard/clean -f/rebase/merge`, `rm -rf`, `sudo`, `curl/wget/nc/ssh/scp/rsync`, `kubectl/helm apply\|delete\|rollout\|upgrade`, `docker push\|rm\|system prune`, `npm/yarn/pnpm publish`, `terraform/pulumi apply\|destroy`, writes to `/etc /usr /System /Library`, and anything containing `deploy`. Checked in `set-verify` **and** again before every run. | It is a regex list, **not a sandbox**. A determined or sufficiently creative command can get around it. |
| Plan policy filter | Nodes mentioning deploy/publish/release/push/commit/merge/production are dropped from model plans in code. | It filters titles, not intent. |
| laya-guard (optional) | Deterministic rules plus an open-weight classifier on every shell command and on the goal text. Block -> deny, review -> ask. | Without it installed, only the denylist applies. |
| Fail-open hooks | A broken `ouro` never wedges your editor. | This also means a missing guard silently degrades to denylist-only. Run `ouro doctor`. |

## Hardening advice

- Treat `ouro set-verify` like writing a CI step: use test/build/grep commands only.
- Do not put secrets in goals. Goals are stored in `.ouro/state.json` and sent only to the Ollama endpoint you configure (default `127.0.0.1`); Ouroboros makes no other network calls.
- `.ouro/` is added to `.git/info/exclude` automatically; keep it out of commits.
- Run untrusted repositories in a container or VM. Ouroboros is not an isolation boundary.

## Legal and compliance notes

Not legal advice.

- **Your prompts stay local.** Ouroboros sends goals only to the Ollama endpoint you configure (default `127.0.0.1`)
  and writes them to `.ouro/` in your workspace. It has no telemetry. If you point `OURO_OLLAMA_URL` at a remote
  server, goal text leaves your machine: do not put personal data or secrets in goals.
- **Model licenses are yours to follow.** Ouroboros bundles no model. The default `qwen2.5:3b` is under the Qwen
  Research License (non-commercial); check any model's license before commercial use (see `THIRD_PARTY_NOTICES.md`).
- **Generated code.** The coding agent's output may be subject to your employer's policies, your model provider's terms
  and, for third-party code it reproduces, the original license. Review before shipping.
- **Employer IP.** If you use this at work, check your employment and IP-assignment terms before publishing anything
  derived from work projects.
- **Guard is best-effort.** The denylist and optional classifier reduce risk; they do not guarantee compliance with any law.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (**Security -> Report a vulnerability**) on this repository.
Do not open a public issue for security problems. I aim to acknowledge within 7 days.
