# Openfarm Workspace Rules

When operating inside an Openfarm-managed repository:

1. Never weaken or modify `[policy] never_touch`, `.openfarm/config.toml` verification gates, or `IMMUTABLE_GATE_KEYS`.
2. Commit baseline code, `.openfarm/`, and `cards/*.md` on the base branch (`main`) before invoking `openfarm check` or `openfarm run`.
3. Keep automated coding-model build/retry logs out of frontier agent context by delegating card waves to `openfarm run` and `openfarm supervise`.
4. Frontier supervision works in ACP mode across Claude (`claude-acp`), Antigravity (`antigravity-acp` / `agy-acp`), and Codex (`codex-acp`) via OAuth (`openfarm acp status|login|configure --provider claude|antigravity|codex|all`), or in automated external Driver mode (`openfarm drive --agent claude|antigravity|codex`); Workers remain configured via API keys (`[model] key_file`).
5. Never push farm branches automatically; review `openfarm dossier <CARD>` and `git diff` (or run `openfarm drive --wave --record-verdict`), record `openfarm verdict`, and merge approved branches locally.
