# Openfarm Workspace Rules

When operating inside an Openfarm-managed repository:

1. Never weaken or modify `[policy] never_touch`, `.openfarm/config.toml` verification gates, or `IMMUTABLE_GATE_KEYS`.
2. Commit baseline code, `.openfarm/`, and `cards/*.md` on the base branch (`main`) before invoking `openfarm check` or `openfarm run`.
3. Keep automated coding-model build/retry logs out of frontier agent context by delegating card waves to `openfarm run` and `openfarm supervise`.
4. Frontier supervision defaults to native Claude ACP via OAuth (`openfarm acp status` / `openfarm acp login` / `openfarm acp configure`); Workers remain configured via API keys (`[model] key_file`).
5. Never push farm branches automatically; review `openfarm dossier <CARD>` and `git diff`, record `openfarm verdict`, and merge approved branches locally.
