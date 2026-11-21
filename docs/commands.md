# Commands

One `openfarm` CLI with **41 subcommands**, shared exit codes (`0` ok, `1` refused/ineligible, `2` usage/config error, `3` trouble/infra), and `--json` parity on every command.

### Setup, Auth & Multi-Project

| Command | Purpose |
| --- | --- |
| `openfarm init NAME` | Scaffold a new Rust, Python, TypeScript, or Go repo (`--lang`, `--idea`, `--acp-provider`, `--commit`), inheriting global providers + ZDR headers |
| `openfarm adopt` | Adopt an existing Rust, Python, TypeScript, or Go repository with baseline verification (`--lang`, `--no-suite`) |
| `openfarm setup [--yes] [--project]` | Set everything up step by step: who plans and reviews, the builders' address and key, the model list it offers, and the model and effort for each job. Every answer is also a flag |
| `openfarm models [show\|set\|clear\|catalogue]` | Show or choose the model and effort for the Architect, the reviewer and the builders, for this project or with `--global` for every project; `catalogue` lists what can be chosen |
| `openfarm providers [status\|set\|probe]` | Inspect, save (`0600`), or live-probe global Frontier & Worker settings (`~/.config/openfarm/providers.toml`) |
| `openfarm projects [list\|add\|edit\|remove\|discover]` | Manage global Openfarm project registry (`~/.config/openfarm/projects.toml`); `remove` never touches project files on disk |
| `openfarm acp [status\|login\|configure\|probe]` | Check logins & confinement, run OAuth login, configure `.openfarm/config.toml`, or run a zero-token ACP handshake |
| `openfarm doctor [--fix] [--probe]` | Preflight health check across git, ACP OAuth, Worker key/endpoint, `bwrap`, toolchains, and stale locks |
| `openfarm install-skill` | Deploy bundled skills, slash commands, plugin, and optional `openfarm` bin shim |

### Run, Plan, Web & Daemon

| Command | Purpose |
| --- | --- |
| `openfarm` / `openfarm auto` / `openfarm tui` | Launch the interactive Textual Workbench (`--curses` for stdlib fallback, `--once` for snapshot, `--view` for curses views `overview\|cards\|dossier\|board\|swarm\|harness\|ops` or legacy workbench views `studio\|review\|projects\|findings\|engine\|providers`) |
| `openfarm plan [--draft]` | Display Cards-as-the-DAG topological tiers, parallel batches, and cycle checks (or draft a wave plan) |
| `openfarm card [add\|show\|history\|skip\|migrate]` | Add, read and drop tasks where they live (the cards branch), see who changed one and why, or move an older project's cards and spec off `main` |
| `openfarm try [--goal C1]` | Try it: start the app, screenshot it at desktop and phone size, and check the goal's content can be seen |
| `openfarm check CARD [--baseline]` | Verify card eligibility, configured tooling, and optional green baseline |
| `openfarm run` | Execute Ready cards (`--follow` to carry on into tasks that were waiting, `--cards`, `--card`, `--spend`, `--profile lean\|balanced\|strict`, `--for`, `--until`, `--dry-run`) |
| `openfarm daemon [start\|stop\|status\|logs]` | Run and manage a detached background `openfarm run` session (`start --follow` carries on into tasks that become ready; logs at `<state_dir>/daemon.log`) |
| `openfarm web [--tailscale] [--bind HOST] [--port 8787]` | Serve the stdlib Factory Companion Web UI (loopback by default with `Host`/`Origin` and `X-Openfarm-Token` protection; `--tailscale` exposes HTTPS via `tailscale serve`; `--bind tailscale` binds tailnet IPv4) |
| `openfarm status` | Show farm, card queue, and harness-memory status |
| `openfarm pause` / `resume` / `stop` | Pause/resume claims, clear circuit breaker or kill switch, or stop an active run |

### Review, Verification & Telemetry

| Command | Purpose |
| --- | --- |
| `openfarm triage [CARDS...]` | Score card complexity (`low`/`medium`/`high`/`split`) and recommend supervision profiles |
| `openfarm review` | Interactive (or `--json`) wave review of `in-review` cards, diffs, and dossiers |
| `openfarm land CARD... [--promote-integration]` | Local-only serial merge queue (trial-merge/rebase + full test gate + `git merge --no-ff` + cleanup; zero push). `--accept-blocked --note "what I checked"` lands a blocked card a person has checked, recording who and why |
| `openfarm findings [list\|pick\|dismiss\|fold]` | Manage worker-filed findings inbox across waves |
| `openfarm frontier-review` | Mixture-of-Frontiers review across multiple providers (`--providers claude,codex`) |
| `openfarm dossier CARD` | Print a card's reviewer dossier as Markdown or JSON |
| `openfarm verdict CARD VERDICT` | Record review outcome (`clean`, `minor-fix`, `bounced`, `scrapped`) and review tokens |
| `openfarm done CARD` / `release CARD` / `escape CARD` | Mark a locally merged card done, release a blocked card, or log a post-review escape |
| `openfarm replay [list\|add\|run]` | Manage and run Golden Replay regression tests |
| `openfarm routing` | Show empirical model/effort/profile routing recommendations from historical card runs |
| `openfarm timeline CARD` | Inspect run-by-run attempts, gates, stop codes, and Phase-B resume eligibility |
| `openfarm supervise` | Bounded micromanager triage of blocked cards or queue preparation |
| `openfarm harness` | Inspect or curate project and global Harness² memory (`status`, `index`, `show`, `promote`, `demote`, `reset --yes`, `--global`) |

### Swarm, Board, Drive & Bench

| Command | Purpose |
| --- | --- |
| `openfarm board [read\|post\|answer\|wait]` | Read or interact with a live run's append-only swarm board |
| `openfarm swarm [status\|tiers\|join\|start\|set\|hint\|block\|remove]` | Inspect tiers/roster or act as external coordinator/supervisor |
| `openfarm drive` | Drive external coordinator/supervisor seats headlessly with `claude`, `codex`, or `agy` |
| `openfarm bench [list\|run]` | Run the 12-task efficacy bench (`--dry-run` or live farm vs. solo arms in [`bench/arms/`](bench/arms/)) |

Every command also prints its own help: `openfarm <command> --help`.
