"""Openfarm v0.10.0 Textual 3-View + Findings Inbox Workbench."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rich.markup import escape as rich_escape
from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    Sparkline,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from openfarm_core.acp_setup import inspect_acp_status
from openfarm_core.adopt import adopt_project
from openfarm_core.automode import (
    DEFAULT_GRILL_QUESTIONS,
    architect_grill_turn,
    compute_pipeline_stages,
    detect_workspace_state,
    draft_spec_and_chunks,
    draft_wave_plan,
    ensure_baseline_committed,
    load_project_chunks,
    open_architect_backend,
    set_chunk_status,
    split_card_into_two,
    update_card_with_eligibility,
)
from openfarm_core.bootstrap import initialize_project
import openfarm_core
from openfarm_core.config import CONFIG_PATH, load_config
from openfarm_core.daemon import daemon_status, start_daemon, stop_daemon
from openfarm_core.doctor import compute_touches_conflict_matrix, run_doctor
from openfarm_core.routing import compute_routing_recommendations
from openfarm_core.errors import AdapterError, ConfigError, Refused
from openfarm_core.findings import (
    bulk_decide_findings,
    decide_finding,
    findings_for_card,
    is_finding_id_or_header,
    list_findings,
)
from openfarm_core.cards import waiting_on, waiting_text
from openfarm_core.gates.eligibility import check_eligibility
from openfarm_core.git import Git
from openfarm_core.projects import (
    _is_ephemeral_tmp_path,
    default_projects_path,
    discover_workspace_projects,
    edit_project,
    get_project,
    list_projects,
    register_project,
    remove_project,
)
from openfarm_core.providers import (
    WORKER_PRESETS,
    load_global_providers,
    probe_worker_endpoint,
    save_global_providers,
)
from openfarm_core.state import StateDir
from openfarm_core.triage import triage_card_heuristic
from openfarm_core.tui import _load_tui_context, _read_cards_readonly

from .markup import esc
from .modals import (
    BounceNoteModal,
    CardEditorModal,
    DeleteProjectModal,
    DismissFindingModal,
    EditProjectModal,
    FoldFindingModal,
    FullFindingModal,
    HintModal,
    NewProjectModal,
    SplitCardModal,
    WorkspaceDiscoveryModal,
)


ARCHITECT_ON_WORDS = "on: the Architect writes the questions, the spec and the cards"
ARCHITECT_OFF_WORDS = "off: the plan comes from a basic template, the Architect is not asked"
STEP_BUTTONS = {
    "grill": "#btn-grill-ask",
    "spec": "#btn-draft-spec",
    "split": "#btn-chunk-to-cards",
    "plan": "#btn-draft-plan",
}


class OpenfarmPaletteProvider(Provider):
    """Fuzzy Command Palette wired to all guided & power actions."""

    ENTRIES = (
        ("Projects: Switch, Add, Edit or Remove Projects", "projects", "Manage all Openfarm projects across directories"),
        ("Studio: Grill & Draft Next Wave", "grill", "Open multi-line Grill & Frontier Architect"),
        ("Studio: Launch Wave (non-blocking run)", "run", "Commit baseline & run ready cards"),
        ("Review: Inspect Diffs & Local Land Inbox", "review", "Switch to Split-Pane Review & Land"),
        ("Findings: Open Worker Findings Inbox", "findings", "Pick, dismiss, or fold worker findings"),
        ("Providers: Global Frontier & Worker Setup", "providers", "Configure presets, keys & live probe"),
        ("Power: Mixture-of-Frontiers Review", "frontier-review", "Run Claude + Codex review on selected card"),
        ("Power: Land Approved Cards Locally", "land", "Serial local merge onto base branch (zero push)"),
        ("Engine: Pause Queue", "pause", "Stop new card claims"),
        ("Engine: Resume Queue", "resume", "Clear pause flag"),
        ("Engine: Stop Run (Kill Switch)", "stop", "Request active run stop"),
        ("Engine: Board / Swarm / Harness Diagnostics", "engine", "Inspect live board, swarm & harness"),
    )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app: OpenfarmApp = self.app  # type: ignore[assignment]
        for label, action_id, help_text in self.ENTRIES:
            score = matcher.match(f"{label} {action_id}")
            if score > 0 or not query.strip():
                yield Hit(
                    score or 1.0,
                    matcher.highlight(label) if query.strip() else Text(label),
                    lambda a=action_id: app.dispatch_palette_action(a),
                    help=f"{action_id}  ·  {help_text}",
                )


class OpenfarmApp(App):
    """High-contrast Textual workbench for Openfarm v0.10.0."""

    TITLE = "openfarm"
    COMMANDS = {OpenfarmPaletteProvider}

    BINDINGS = [
        Binding("0", "switch_view('projects')", "0 Projects", show=True),
        Binding("1", "switch_view('studio')", "1 Studio", show=True),
        Binding("2", "switch_view('review')", "2 Review", show=True),
        Binding("3", "switch_view('findings')", "3 Findings", show=True),
        Binding("4", "switch_view('engine')", "4 Engine", show=True),
        Binding("ctrl+k", "command_palette", "Palette", show=True),
        Binding("r", "refresh_all", "Refresh", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    CSS = """
    Screen {
        background: #0b0f17;
        color: #eef4fc;
        layout: vertical;
    }
    #topbar {
        height: 3;
        background: #131a26;
        border-bottom: solid #26344a;
        padding: 0 2;
        align: left middle;
    }
    #brand {
        width: auto;
        background: #38bdf8;
        color: #08101a;
        text-style: bold;
        padding: 0 2;
        margin-right: 2;
    }
    #repo-title {
        width: auto;
        color: #f8fafc;
        text-style: bold;
        margin-right: 2;
    }
    .spacer { width: 1fr; }
    #token-summary {
        width: auto;
        color: #cbd5e1;
        margin-right: 2;
    }
    .status-pill {
        width: auto;
        padding: 0 2;
        text-style: bold;
    }
    .status-running { background: #423310; color: #fbbf24; }
    .status-ready   { background: #103824; color: #4ade80; }
    .status-setup   { background: #123447; color: #38bdf8; }

    #navstrip {
        height: 3;
        background: #0f1520;
        border-bottom: solid #1f2b3d;
        padding: 0 2;
        align: left middle;
    }
    .nav-btn {
        width: auto;
        min-width: 10;
        height: 1;
        border: none;
        background: #182232;
        color: #cbd5e1;
        margin-right: 1;
        padding: 0 2;
    }
    .nav-btn.active {
        background: #12384d;
        color: #38bdf8;
        text-style: bold;
    }
    .nav-sep { width: auto; color: #3b4d6b; margin: 0 1; }
    .step-done { width: auto; background: #103623; color: #4ade80; text-style: bold; padding: 0 1; margin-right: 1; }
    .step-cur  { width: auto; background: #12384d; color: #38bdf8; text-style: bold; padding: 0 1; margin-right: 1; }
    .step-pend { width: auto; background: #161f2e; color: #94a3b8; padding: 0 1; margin-right: 1; }
    .step-arrow { width: auto; color: #475569; margin-right: 1; }

    #main-switcher { height: 1fr; padding: 1 2; }
    #studio-switcher { height: 1fr; }

    .card {
        background: #111824;
        border: round #2b3b54;
        border-title-color: #e2e8f0;
        border-title-style: bold;
        padding: 1 2;
    }
    .card-focused {
        background: #111824;
        border: round #38bdf8;
        border-title-color: #38bdf8;
        border-title-style: bold;
        padding: 1 2;
    }
    .section-h { width: auto; color: #38bdf8; text-style: bold; margin-top: 1; }
    .section-h-first { width: auto; color: #38bdf8; text-style: bold; }
    .subtext { color: #cbd5e1; }
    .inline-label { width: auto; color: #cbd5e1; }

    .pill-green  { width: auto; background: #103623; color: #4ade80; text-style: bold; padding: 0 1; margin-right: 1; }
    .pill-cyan   { width: auto; background: #12384d; color: #38bdf8; text-style: bold; padding: 0 1; margin-right: 1; }
    .pill-purple { width: auto; background: #2f1b47; color: #c084fc; text-style: bold; padding: 0 1; margin-right: 1; }
    .pill-yellow { width: auto; background: #423310; color: #fbbf24; text-style: bold; padding: 0 1; margin-right: 1; }

    .action-row { height: 1; align: left middle; margin-top: 1; }
    .action-row-tall { height: 3; align: left middle; margin-top: 1; }
    Button.primary-cta {
        width: auto; min-width: 10; height: 1; background: #38bdf8; color: #08101a;
        text-style: bold; border: none; padding: 0 2; margin-right: 1;
    }
    Button.approve-btn {
        width: auto; min-width: 10; height: 1; background: #22c55e; color: #06180d;
        text-style: bold; border: none; padding: 0 2; margin-right: 1;
    }
    Button.soft-btn {
        width: auto; min-width: 8; height: 1; background: #1b2638; color: #e2e8f0;
        border: none; padding: 0 2; margin-right: 1;
    }

    /* Findings "Needs You" Banner in Studio */
    #studio-findings-banner {
        height: auto;
        background: #2b230f;
        border: round #fbbf24;
        padding: 0 2;
        margin-bottom: 1;
    }
    .hidden { display: none; }
    #architect-switch-row { height: 3; align: left middle; }

    /* Providers View */
    #prov-card { width: 100%; max-width: 118; height: 1fr; }
    #frontier-row {
        height: 3; background: #162030; border: round #22c55e;
        padding: 0 2; align: left middle; margin-top: 1; margin-bottom: 1;
    }
    RadioSet { background: #0d131d; border: round #26344a; padding: 0 1; height: auto; margin: 1 0; }
    .field-row { height: 4; margin-bottom: 1; }
    .field-col { width: 1fr; margin-right: 2; }
    Input { background: #0b1019; border: round #334663; color: #f8fafc; height: 3; }
    Input:focus { border: round #38bdf8; }
    #probe-banner {
        height: 3; background: #0f2e1f; border: round #4ade80;
        padding: 0 2; align: left middle;
    }

    /* Studio Grill */
    #stack-bar { height: 1; align: left middle; margin-bottom: 1; }
    .stack-chip {
        width: auto; min-width: 8; height: 1; margin-right: 1;
        background: #172131; color: #cbd5e1; border: none; padding: 0 2;
    }
    .stack-chip.selected { background: #12384d; color: #38bdf8; text-style: bold; }
    TextArea { background: #0b1019; border: round #334663; color: #f8fafc; margin-top: 1; }
    TextArea:focus { border: round #38bdf8; }
    #ta-idea { height: 5; }
    #grill-chat-box {
        height: auto;
        background: #0d131d;
        border: round #26344a;
        padding: 1 2;
        margin-top: 1;
    }
    #grill-chat-box.hidden { display: none; }
    #grill-options-row { height: auto; margin-top: 1; }
    #grill-options-row.hidden { display: none; }
    .grill-opt-btn {
        width: auto; min-width: 8; height: 1; margin-right: 1;
        background: #12384d; color: #e0f2fe; border: none; padding: 0 2;
    }
    .grill-opt-btn.hidden { display: none; }
    #grill-answer-row { height: 3; align: left middle; margin-top: 1; }
    #inp-grill-answer { width: 1fr; margin-right: 1; }
    #grill-status {
        height: auto;
        background: #12283d;
        border: round #38bdf8;
        color: #e0f2fe;
        padding: 0 2;
        margin-top: 1;
    }
    #grill-status.hidden { display: none; }
    #architect-panel {
        height: auto;
        max-height: 11;
        background: #0e1c2b;
        border: round #38bdf8;
        padding: 0 2;
        margin-bottom: 1;
    }
    #architect-panel.hidden { display: none; }
    #architect-panel-head { height: 1; align: left middle; }
    #architect-banner { width: 1fr; color: #e0f2fe; }
    #architect-log { height: 6; background: #09111b; border: round #1e334a; padding: 0 1; margin-top: 1; }

    /* Split Panes */
    .split-left  { width: 46; margin-right: 2; height: 1fr; }
    .split-right { width: 1fr; height: 1fr; }
    .q-box { background: #0b1019; border: round #26344a; padding: 0 1; margin-bottom: 1; }

    /* Live Run */
    #econ-box { background: #0d131d; border: round #26344a; padding: 0 1; margin-top: 1; height: auto; }
    #activity-log { height: 1fr; background: #0b1019; border: round #26344a; padding: 1 2; margin-bottom: 1; }
    Sparkline { height: 2; }
    Sparkline > .sparkline--max-color { color: #4ade80; }
    Sparkline > .sparkline--min-color { color: #38bdf8; }

    /* Review & Findings */
    #diff-scroll { height: 1fr; background: #0b1019; border: round #26344a; padding: 0 1; margin: 1 0; }
    #review-card-findings {
        height: auto; max-height: 8; background: #171d29; border: round #fbbf24;
        padding: 0 2; margin-bottom: 1;
    }
    #findings-header {
        height: 1; color: #fbbf24; text-style: bold; margin-bottom: 1;
    }
    #findings-filter { height: 3; margin-bottom: 1; }
    #projects-table, #findings-table, #review-table, #plan-table, #board-table, #chunks-table {
        height: 1fr; background: #0b1019; border: round #26344a;
    }
    #ta-spec-editor { height: 1fr; }
    #project-detail-scroll, #finding-detail-scroll {
        height: 1fr; background: #0b1019; border: round #26344a; padding: 1 2; margin-bottom: 1;
    }
    Footer { background: #131a26; color: #cbd5e1; }
    """

    def __init__(
        self,
        repo: Path | str = ".",
        config_path: Path | str | None = None,
        *,
        run_id: str | None = None,
        initial_view: str = "studio",
    ) -> None:
        super().__init__()
        self.repo = Path(repo).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.pinned_run_id = run_id
        self._initial_view = initial_view
        self._selected_stack = "typescript"
        self._stack_user_override = False
        self._grill_history: list[dict] = []
        self._grill_pending_question = ""
        self._grill_options: list[str] = []
        self._chunks_items: list[dict] = []
        self._selected_chunk_id = ""
        self._running_wave = False
        self._drafting_plan = False
        self._draft_started_at = 0.0
        self._draft_phase_msg = ""
        self._draft_last_heartbeat = 0
        self._active_architect_backend: object | None = None
        self._architect_cancelled = False
        self._architect_failure: dict | None = None  # the step to offer Retry for
        self._busy_button = ""  # only the running step's button shows a spinner
        self._draft_asked = True  # False while a basic template stands in for the Architect
        self._draft_doing = ""
        self._grill_from_template = False
        self._last_baseline_ok: bool | None = None
        self._last_baseline_error = ""
        self._worker_tokens = 0
        self._supervisor_tokens = 0
        self._tokens_fresh = 0
        self._tokens_cached = 0
        self._spark_points: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0]
        self._plan_cards: list[dict] = []
        self._selected_plan_id: str = ""
        self._in_review_count = 0
        self._startable_count = 0
        self._review_cards: list = []
        self._selected_review_id: str = ""
        self._findings_items: list[dict] = []
        self._findings_counts: dict = {
            "waiting": 0, "new_since_last_run": 0, "picked": 0,
            "dismissed": 0, "folded": 0, "promoted": 0, "total": 0,
        }
        self._selected_finding_id: str = ""
        self._findings_query: str = ""
        self._approved_to_land: list[str] = []
        self._card_phases: dict[str, str] = {}
        self._cards_finished: set[str] = set()
        self._run_start_note = ""  # what the run said when it picked its cards
        self._busy_labels: dict[str, str] = {}  # buttons of slow actions still running
        self._projects_items: list[dict] = []
        self._selected_project_id: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("openfarm", id="brand")
            yield Static(esc(self.repo.name), id="repo-title")
            yield Static("", classes="spacer")
            yield Static("W —% · F —% (idle)", id="token-summary")
            yield Static("○ READY", id="status-badge", classes="status-pill status-ready")

        with Horizontal(id="navstrip"):
            yield Button("0 Projects", id="nav-projects", classes="nav-btn")
            yield Button("1 Studio", id="nav-studio", classes="nav-btn active")
            yield Button("2 Review", id="nav-review", classes="nav-btn")
            yield Button("3 Findings", id="nav-findings", classes="nav-btn")
            yield Button("4 Engine", id="nav-engine", classes="nav-btn")
            yield Static("│", classes="nav-sep")
            yield Static("○ Providers", id="st-prov", classes="step-pend")
            yield Static("→", classes="step-arrow")
            yield Static("○ Plan", id="st-plan", classes="step-pend")
            yield Static("→", classes="step-arrow")
            yield Static("○ Run", id="st-run", classes="step-pend")
            yield Static("→", classes="step-arrow")
            yield Static("○ Review & Land", id="st-rev", classes="step-pend")

        with ContentSwitcher(initial="studio", id="main-switcher"):
            # ── VIEW P: PROJECTS MANAGER (SWITCH / ADD / EDIT / NON-DESTRUCTIVE REMOVE) ──
            with Horizontal(id="projects"):
                left_pr = Vertical(classes="card-focused split-left")
                left_pr.border_title = "Openfarm Projects"
                with left_pr:
                    yield DataTable(id="projects-table")
                right_pr = Vertical(classes="card split-right")
                right_pr.border_title = "Project Detail"
                with right_pr:
                    with VerticalScroll(id="project-detail-scroll"):
                        yield Static("No projects registered yet.", id="project-detail")
                    with Horizontal(classes="action-row"):
                        yield Button("Switch Project \\[Enter]", classes="primary-cta", id="btn-proj-switch")
                        yield Button("Add / New \\[n]", classes="approve-btn", id="btn-proj-new")
                        yield Button("Scan ~/projects \\[S]", classes="soft-btn", id="btn-proj-scan")
                        yield Button("Edit \\[e]", classes="soft-btn", id="btn-proj-edit")
                        yield Button("Remove From Openfarm \\[d]", classes="soft-btn", id="btn-proj-delete")

            # ── VIEW 0: GLOBAL PROVIDER SETUP ────────────────────────────────
            with Container(id="providers"):
                prov = VerticalScroll(id="prov-card", classes="card-focused")
                prov.border_title = "Global Provider Profile  (~/.config/openfarm/providers.toml)"
                with prov:
                    yield Static("FRONTIER   (Architect · Supervisor · Mixture-of-Frontiers Reviewer)", classes="section-h-first")
                    with Horizontal(id="frontier-row"):
                        yield Static("● Claude ACP (OAuth)", id="prov-frontier-status", classes="inline-label")
                        yield Static("", classes="spacer")
                        yield Button("Login OAuth \\[l]", id="btn-acp-login", classes="soft-btn")

                    yield Static("WORKER     (Lesser Coding Model · ZDR Header x-cmd-zdr: 1)", classes="section-h")
                    with RadioSet(id="worker-presets"):
                        for idx, preset in enumerate(WORKER_PRESETS):
                            ep_txt = preset["endpoint"] or "Enter custom http(s)://.../v1 URL"
                            yield RadioButton(
                                f"{preset['label']:<20} {ep_txt:<42} ({preset['note']})",
                                value=(idx == 0),
                                id=f"preset-{preset['id']}",
                            )

                    with Horizontal(classes="field-row"):
                        with Vertical(classes="field-col"):
                            yield Label("Worker Endpoint URL", classes="subtext")
                            yield Input(value="https://openrouter.ai/api/v1", id="inp-endpoint")
                        with Vertical(classes="field-col"):
                            yield Label("Worker Coding Model ID", classes="subtext")
                            yield Input(value="qwen/qwen3-coder", id="inp-model")

                    with Horizontal(classes="field-row"):
                        with Vertical(classes="field-col"):
                            yield Label("Worker API Key  (masked · saved atomic 0600)", classes="subtext")
                            yield Input(placeholder="Paste API key (leave blank to keep existing)", password=True, id="inp-key")

                    with Horizontal(id="probe-banner"):
                        yield Static("Ready to probe Worker endpoint", id="probe-status", classes="inline-label")

                    with Horizontal(classes="action-row-tall"):
                        yield Button("Save & Continue", classes="primary-cta", id="btn-save-prov")
                        yield Button("Test Probe", classes="soft-btn", id="btn-probe")
                        yield Static("  ZDR Strict: ", classes="inline-label")
                        yield Switch(value=True, id="sw-zdr")

            # ── VIEW 1: STUDIO (GRILL / SPEC & CHUNKS / PLAN / LIVE RUN) ────
            with Vertical(id="studio"):
                yield Static("", id="studio-findings-banner", classes="hidden")
                with Vertical(id="architect-panel", classes="hidden"):
                    with Horizontal(id="architect-panel-head"):
                        yield Static("", id="architect-banner")
                        yield Button("Retry", classes="primary-cta hidden", id="btn-arch-retry")
                        yield Button("Use a basic template instead", classes="soft-btn hidden", id="btn-arch-template")
                        yield Button("Cancel", classes="soft-btn hidden", id="btn-arch-cancel")
                        yield Button("Hide Log", classes="soft-btn", id="btn-arch-hide")
                    yield RichLog(id="architect-log", markup=True, wrap=True, min_width=0)
                with ContentSwitcher(initial="studio-grill", id="studio-switcher"):
                    # 1A: Single-Prompt Studio & Conversational Frontier Architect
                    with VerticalScroll(id="studio-grill", classes="card-focused") as sg:
                        sg.border_title = "Project Studio  ·  Frontier Architect"
                        with Horizontal(id="stack-bar"):
                            yield Static("Stack: ", classes="inline-label")
                            yield Button("● TypeScript / Web", id="stk-typescript", classes="stack-chip selected")
                            yield Button("○ Python", id="stk-python", classes="stack-chip")
                            yield Button("○ Rust", id="stk-rust", classes="stack-chip")
                            yield Button("○ Go", id="stk-go", classes="stack-chip")
                            yield Button("○ Auto-detect", id="stk-auto", classes="stack-chip")
                        yield Static("What do you want to build?", classes="section-h-first")
                        yield Static(
                            "Describe a vague app idea, a full spec, or a feature in an existing repo. "
                            "Frontier Architect will clarify key choices with you, then design the real runnable app, invariants & Wave 1 cards.",
                            classes="subtext",
                        )
                        yield TextArea("", id="ta-idea", show_line_numbers=False)
                        with Vertical(id="grill-chat-box", classes="hidden"):
                            yield Static("", id="grill-transcript")
                            with Horizontal(id="grill-options-row", classes="hidden"):
                                yield Button("", id="btn-grill-opt-0", classes="grill-opt-btn hidden")
                                yield Button("", id="btn-grill-opt-1", classes="grill-opt-btn hidden")
                                yield Button("", id="btn-grill-opt-2", classes="grill-opt-btn hidden")
                            with Horizontal(id="grill-answer-row"):
                                yield Input(placeholder="Reply to Architect (or click an option above)...", id="inp-grill-answer")
                                yield Button("Reply", classes="primary-cta", id="btn-grill-send")
                                yield Button("Reset Chat", classes="soft-btn", id="btn-grill-reset")
                        with Horizontal(classes="action-row-tall"):
                            yield Button("Ask Architect to Grill Me", classes="primary-cta", id="btn-grill-ask")
                            yield Button("Flesh Out Spec & Chunks", classes="approve-btn", id="btn-draft-spec")
                            yield Button("Quick: Draft Cards Now", classes="soft-btn", id="btn-draft-plan")
                            yield Button("Configure Providers", classes="soft-btn", id="btn-open-providers")
                        with Horizontal(id="architect-switch-row"):
                            yield Static("Architect: ", classes="inline-label")
                            yield Switch(value=True, id="sw-use-architect")
                            yield Static(ARCHITECT_ON_WORDS, id="sw-architect-words", classes="inline-label")
                        yield Static("", id="grill-status", classes="hidden")

                    # 1A-2: Living Spec & Ordered Chunks (Idea -> Grill -> Spec -> Chunks -> Cards)
                    with Horizontal(id="studio-spec"):
                        left_sp = Vertical(classes="card-focused split-left")
                        left_sp.border_title = "Spec Chunks (Milestones)"
                        with left_sp:
                            yield DataTable(id="chunks-table")
                            yield Static("Select a chunk to inspect its goal and split it into cards.", id="chunk-goal-detail", classes="subtext")
                        right_sp = Vertical(classes="card split-right")
                        right_sp.border_title = "Living Spec (.openfarm/PROJECT_SPEC.md)"
                        with right_sp:
                            yield TextArea("", id="ta-spec-editor")
                            yield Static("", id="spec-status", classes="subtext")
                            with Horizontal(classes="action-row"):
                                yield Button("Split Chunk into Cards", classes="primary-cta", id="btn-chunk-to-cards")
                                yield Button("Mark Chunk Done", classes="approve-btn", id="btn-chunk-done")
                                yield Button("Save Spec", classes="soft-btn", id="btn-spec-save")
                                yield Button("Back to Grill / Iterate", classes="soft-btn", id="btn-spec-back")

                    # 1B: Pre-Verified Wave Plan Approval
                    with Horizontal(id="studio-plan"):
                        left_p = Vertical(classes="card-focused split-left")
                        left_p.border_title = "Wave Plan"
                        with left_p:
                            yield DataTable(id="plan-table")
                            yield Static("", id="plan-preflight", classes="subtext")
                        right_p = VerticalScroll(classes="card split-right")
                        right_p.border_title = "Card Detail"
                        with right_p:
                            yield Static("Select a card to inspect its scope and named tests.", id="plan-detail")
                            with Horizontal(classes="action-row"):
                                yield Button("Commit Baseline & Launch Wave", classes="primary-cta", id="btn-launch-wave")
                                yield Button("Edit Card \\[e]", classes="soft-btn", id="btn-plan-edit")
                                yield Button("Split Card \\[x]", classes="soft-btn", id="btn-plan-split")
                                yield Button("Spec & Chunks", classes="soft-btn", id="btn-open-spec")
                                yield Button("Release Blocked", classes="soft-btn", id="btn-plan-release")
                                yield Button("Iterate / New Idea", classes="soft-btn", id="btn-regrill")

                    # 1C: Live Non-Blocking Factory Floor
                    with Horizontal(id="studio-run"):
                        left_r = Vertical(classes="card split-left")
                        left_r.border_title = "Wave Queue (non-blocking)"
                        with left_r:
                            yield Static("No active wave yet.", id="run-queue-view")
                            with Vertical(id="econ-box"):
                                yield Static("TOKEN ECONOMICS", id="econ-title", classes="subtext")
                                yield ProgressBar(total=100, show_eta=False, show_percentage=True, id="worker-bar")
                                yield Static("Worker 0% · Frontier 0% · fresh 0 · cached 0", id="econ-stats", classes="subtext")
                                yield Sparkline(self._spark_points, id="econ-spark")
                        right_r = Vertical(classes="card-focused split-right")
                        right_r.border_title = "Live Activity Stream"
                        with right_r:
                            yield RichLog(id="activity-log", markup=True, wrap=True, min_width=0)
                            with Horizontal(classes="action-row", id="run-controls"):
                                yield Button("Back to Plan", classes="primary-cta", id="btn-run-back")
                                yield Button("Pause \\[p]", classes="soft-btn hidden", id="btn-pause")
                                yield Button("Stop \\[s]", classes="soft-btn hidden", id="btn-stop")
                                yield Button("Hint \\[h]", classes="soft-btn hidden", id="btn-hint")
                                yield Button("Resume", classes="soft-btn hidden", id="btn-resume")
                                yield Button("Start Detached", classes="soft-btn", id="btn-daemon-start")
                                yield Button("Stop Daemon", classes="soft-btn hidden", id="btn-daemon-stop")
                                yield Button("Open Review [2]", classes="approve-btn", id="btn-jump-review")

            # ── VIEW 2: SPLIT-PANE REVIEW & LAND INBOX ───────────────────────
            with Horizontal(id="review"):
                left_rev = Vertical(classes="card split-left")
                left_rev.border_title = "Review & Land Inbox"
                with left_rev:
                    yield DataTable(id="review-table")
                    yield Static("Approved queue: (none)", id="approved-queue-label", classes="subtext")
                right_rev = Vertical(classes="card-focused split-right")
                right_rev.border_title = "Card Dossier"
                with right_rev:
                    with TabbedContent(id="review-tabs"):
                        with TabPane("Summary & Diff", id="rev-tab-summary"):
                            yield Static("Select a card from the queue.", id="review-gates")
                            yield Static("No findings filed by this card.", id="review-card-findings")
                            yield Static("", id="review-status", classes="subtext")
                            with VerticalScroll(id="diff-scroll"):
                                yield Static("(no diff preview)", id="review-diff")
                            with Horizontal(classes="action-row"):
                                yield Button("Approve \\[a]", classes="approve-btn", id="btn-approve")
                                yield Button("Land Locally \\[L]", classes="primary-cta", id="btn-land")
                                yield Button("Merge Integration → Main", classes="soft-btn", id="btn-promote-int")
                                yield Button("Bounce \\[b]", classes="soft-btn", id="btn-bounce")
                                yield Button("Accept Blocked...", classes="soft-btn hidden", id="btn-accept-blocked")
                                yield Button("Frontier Review \\[m]", classes="soft-btn", id="btn-freview")
                                yield Button("Next Chunk / Iterate", classes="soft-btn", id="btn-next-iter")
                        with TabPane("Tests & Spec", id="rev-tab-tests"):
                            yield VerticalScroll(Static("(select a card)", id="review-tests"))
                        with TabPane("Frontier Review", id="rev-tab-frontier"):
                            yield VerticalScroll(Static("Press \\[m] to run Mixture-of-Frontiers review.", id="review-frontier-out"))

            # ── VIEW 3: FINDINGS INBOX ───────────────────────────────────────
            with Vertical(id="findings"):
                yield Static("Findings: 0 waiting · 0 new since last run · 0 picked for next wave", id="findings-header")
                yield Input(placeholder="Filter findings (press / to focus, Esc to clear)...", id="findings-filter")
                with Horizontal():
                    left_f = Vertical(classes="card-focused split-left")
                    left_f.border_title = "Waiting Findings (newest first)"
                    with left_f:
                        yield DataTable(id="findings-table")
                    right_f = Vertical(classes="card split-right")
                    right_f.border_title = "Finding Detail"
                    with right_f:
                        with VerticalScroll(id="finding-detail-scroll"):
                            yield Static("No waiting findings.", id="finding-detail")
                        with Horizontal(classes="action-row"):
                            yield Button("Pick Next Wave \\[w]", classes="primary-cta", id="btn-find-pick")
                            yield Button("Dismiss \\[x]", classes="soft-btn", id="btn-find-dismiss")
                            yield Button("Bulk Dismiss Card \\[X]", classes="soft-btn", id="btn-find-bulk-dismiss")
                            yield Button("Fold Into Card \\[c]", classes="soft-btn", id="btn-find-fold")
                            yield Button("Synthesize Wave \\[W]", classes="approve-btn", id="btn-find-synth")
                            yield Button("Open Full \\[Enter]", classes="soft-btn", id="btn-find-full")

            # ── VIEW 4: ENGINE DIAGNOSTICS ───────────────────────────────────
            with Container(id="engine"):
                eng = Vertical(classes="card-focused")
                eng.border_title = "Engine Diagnostics (Board · Swarm · Harness · Providers)"
                with eng:
                    with TabbedContent():
                        with TabPane("Board"):
                            yield DataTable(id="board-table")
                        with TabPane("Swarm"):
                            yield Static("(no active swarm)", id="engine-swarm")
                        with TabPane("Harness"):
                            yield Static("(harness status loading...)", id="engine-harness")
                        with TabPane("Providers & Config"):
                            yield Static("", id="engine-providers")
                            with Horizontal(classes="action-row"):
                                yield Button("Open Global Provider Setup", classes="primary-cta", id="btn-eng-providers")
                        with TabPane("Doctor"):
                            yield Static("Click Run Doctor to inspect repo, OAuth, Worker, bwrap, toolchains & locks.", id="engine-doctor")
                            with Horizontal(classes="action-row"):
                                yield Button("Run Doctor", classes="primary-cta", id="btn-doc-run")
                                yield Button("Run Safe Fix", classes="approve-btn", id="btn-doc-fix")
                        with TabPane("Spec"):
                            yield VerticalScroll(Static("(no .openfarm/PROJECT_SPEC.md yet)", id="engine-spec"))
                        with TabPane("Routing"):
                            yield VerticalScroll(Static("(no card history yet)", id="engine-routing"))

        yield Footer()

    # ── Lifecycle & Refresh ──────────────────────────────────────────────────

    def on_mount(self) -> None:
        # Configure tables
        prt = self.query_one("#projects-table", DataTable)
        prt.cursor_type = "row"
        prt.add_columns("", "Name", "Stack", "Status", "Path")

        pt = self.query_one("#plan-table", DataTable)
        pt.cursor_type = "row"
        pt.add_columns("ID", "Title", "State", "Needs", "Prof")

        cht = self.query_one("#chunks-table", DataTable)
        cht.cursor_type = "row"
        cht.add_columns("ID", "St", "Chunk")

        rt = self.query_one("#review-table", DataTable)
        rt.cursor_type = "row"
        rt.add_columns("ID", "State", "Title")

        ft = self.query_one("#findings-table", DataTable)
        ft.cursor_type = "row"
        ft.add_columns("ID", "From", "Title")

        bt = self.query_one("#board-table", DataTable)
        bt.cursor_type = "row"
        bt.add_columns("Seq", "Author", "Card", "Kind", "Summary")

        # Register the current directory automatically once if it is already configured
        if (self.repo / CONFIG_PATH).is_file():
            try:
                register_project(self.repo, auto=True)
            except (OSError, Refused) as exc:
                self.log.warning(f"could not register current repo: {exc}")

        self._load_providers_into_form()
        self.refresh_all_views()
        self.set_interval(1.0, self._tick_draft_timer)

        # Choose initial screen cleanly
        gp = load_global_providers()
        ws = detect_workspace_state(self.repo, self.config_path)
        target_view = self._initial_view
        if target_view in ("studio", "auto") and not gp["configured"] and ws["stage"] == "empty":
            target_view = "providers"
        elif target_view == "auto":
            # No tab was asked for: open where the project's next step is.
            target_view = "studio"
            if self._wave_active():
                self.query_one("#studio-switcher", ContentSwitcher).current = "studio-run"
            elif self._in_review_count and not self._startable_count:
                target_view = "review"
        self._load_idea_draft()
        self.action_switch_view(target_view)

    def _idea_draft_path(self) -> Path:
        """The unsent idea is kept beside the projects list, since a new folder has nowhere to put it."""
        key = hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
        return default_projects_path().parent / "idea-drafts" / f"{key}.txt"

    def _load_idea_draft(self) -> None:
        try:
            text = self._idea_draft_path().read_text(encoding="utf-8")
        except OSError:
            text = ""
        self.query_one("#ta-idea", TextArea).load_text(text)

    @on(TextArea.Changed, "#ta-idea")
    def _save_idea_draft(self, event: TextArea.Changed) -> None:
        path = self._idea_draft_path()
        try:
            if event.text_area.text.strip():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(event.text_area.text, encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning(f"could not save the idea draft: {exc}")

    def _typing_target(self):
        """The text field a typed letter belongs to on the idea screen, or None."""
        if len(self.screen_stack) > 1:
            return None
        if self.query_one("#main-switcher", ContentSwitcher).current != "studio":
            return None
        if self.query_one("#studio-switcher", ContentSwitcher).current != "studio-grill":
            return None
        if not self.query_one("#grill-chat-box").has_class("hidden"):
            return self.query_one("#inp-grill-answer", Input)
        return self.query_one("#ta-idea", TextArea)

    def _focus_typing_target(self) -> None:
        target = self._typing_target()
        if target is not None and not isinstance(self.focused, (Input, TextArea)):
            target.focus()

    def notify(self, message, *, title="", severity="information", timeout=None, markup=False):
        """Toasts carry card text, errors and key names like [L], so they are plain text."""
        super().notify(str(message), title=title, severity=severity, timeout=timeout, markup=markup)

    def _safe_cfg(self):
        cfg_p = self.config_path or (self.repo / CONFIG_PATH)
        if not cfg_p.is_file():
            return None
        try:
            return load_config(self.repo, cfg_p)
        except (ConfigError, OSError):
            return None

    def _load_providers_into_form(self) -> None:
        gp = load_global_providers()
        wk = gp["worker"]
        fr = gp["frontier"]["provider"]
        try:
            acp = inspect_acp_status(self.repo, provider=fr)
            signed = bool(acp.get("oauth_ready") or acp.get("ok"))
        except Exception:
            signed = False
        badge = "[bold #4ade80]✓ signed in[/]" if signed else "[bold #fbbf24]○ login required[/]"
        self.query_one("#prov-frontier-status", Static).update(
            f"[bold #f8fafc]● {esc(fr.title())} ACP (OAuth)[/]   {badge}"
        )
        if wk.get("endpoint"):
            self.query_one("#inp-endpoint", Input).value = wk["endpoint"]
        if wk.get("model"):
            self.query_one("#inp-model", Input).value = wk["model"]
        self.query_one("#sw-zdr", Switch).value = bool(wk.get("zdr", True))

    def refresh_all_views(self) -> None:
        cfg = self._safe_cfg()
        ws = detect_workspace_state(self.repo, self.config_path)
        gp = load_global_providers()

        branch = ""
        if (self.repo / ".git").exists():
            try:
                branch = Git(self.repo).current_branch()
            except Exception:
                branch = ""
        self.query_one("#repo-title", Static).update(
            esc(f"{self.repo.name}  ({branch})" if branch else self.repo.name)
        )

        # Load cards & findings
        cards: list = []
        if cfg is not None:
            tctx = _load_tui_context(self.repo, self.config_path)
            cards, _mode = _read_cards_readonly(tctx)
            fres = list_findings(self.repo, cfg, state_filter="waiting", query=self._findings_query, deduplicate=True)
            self._findings_items = fres["findings"]
            self._findings_counts = fres["counts"]
        else:
            self._findings_items = []
            self._findings_counts = {
                "waiting": 0, "new_since_last_run": 0, "picked": 0,
                "dismissed": 0, "folded": 0, "promoted": 0, "total": 0,
            }

        non_findings = [c for c in cards if not is_finding_id_or_header(c.id, getattr(c, "fields", None))]
        in_review = [c for c in non_findings if str(c.state).lower().replace("-", "_") == "in_review"]
        ready_cards = [c for c in non_findings if str(c.state).lower() == "ready"]
        blocked_cards = [c for c in non_findings if str(c.state).lower() == "blocked"]
        startable = [c for c in ready_cards if c.id not in waiting_on(non_findings)]
        self._in_review_count, self._startable_count = len(in_review), len(startable)

        # Update daemon pill when not actively drafting/running in-process
        if not self._running_wave and not self._drafting_plan:
            d_st = daemon_status(self.repo, config_path=self.config_path)
            pill = self.query_one("#status-badge", Static)
            if d_st.get("running"):
                pill.remove_class("status-ready")
                pill.add_class("status-running")
                pill.update(f"● DAEMON ({d_st['pid']})")
            else:
                pill.remove_class("status-running")
                pill.add_class("status-ready")
                pill.update("○ READY")

        # Update nav badges
        self.query_one("#nav-review", Button).label = f"2 Review ({len(in_review)})" if in_review else "2 Review"
        w_cnt = self._findings_counts["waiting"]
        self.query_one("#nav-findings", Button).label = f"3 Findings ({w_cnt})" if w_cnt else "3 Findings"

        # Update phase stepper
        prov_ok = bool(gp["configured"] or (cfg is not None and getattr(cfg, "model_adapter", "") == "scripted"))
        plan_ok = bool(non_findings)
        self._set_step("#st-prov", "Providers", "done" if prov_ok else "current")
        self._set_step("#st-plan", "Plan", "done" if plan_ok else ("current" if prov_ok else "pending"))
        self._set_step(
            "#st-run",
            f"Run ({len(startable)})" if startable else "Run",
            "current" if self._running_wave else ("done" if in_review else ("current" if plan_ok else "pending")),
        )
        self._set_step(
            "#st-rev",
            f"Review ({len(in_review)})" if in_review else "Review & Land",
            "current" if (in_review and not self._running_wave) else "pending",
        )

        # Update Studio "Needs You" findings banner
        banner = self.query_one("#studio-findings-banner", Static)
        if w_cnt > 0:
            new_cnt = self._findings_counts["new_since_last_run"]
            sample_titles = "  ·  ".join(
                f"{item['id']}: {esc(item['title'][:40])}" for item in self._findings_items[:3]
            )
            banner.remove_class("hidden")
            banner.update(
                f"[bold #fbbf24]Needs you: {new_cnt} new findings from this run · {w_cnt} waiting in total[/]  "
                f"[bold #38bdf8][3] Open Findings Inbox[/]\n[#cbd5e1]{sample_titles}[/]"
            )
        else:
            banner.add_class("hidden")

        # Sync Studio stack selector chip to the active project's configured language (unless user picked one)
        if not self._stack_user_override and cfg is not None and getattr(cfg, "languages", None):
            cfg_lang = str(cfg.languages[0]).strip().lower()
            if cfg_lang in ("typescript", "python", "rust", "go") and cfg_lang != self._selected_stack:
                self._apply_stack_chip(cfg_lang)

        # Update Studio sub-mode (when not actively running)
        studio_sw = self.query_one("#studio-switcher", ContentSwitcher)
        if not self._running_wave and studio_sw.current not in ("studio-run", "studio-spec"):
            if not non_findings:
                studio_sw.current = "studio-grill"
            elif studio_sw.current == "studio-grill":
                studio_sw.current = "studio-plan"

        # Populate Spec & Chunks view
        self._chunks_items = load_project_chunks(self.repo)
        cht = self.query_one("#chunks-table", DataTable)
        cht.clear()
        for ch in self._chunks_items:
            st_code = {"active": "act", "done": "✓", "pending": "pnd"}.get(ch["status"], ch["status"][:3])
            cht.add_row(Text(ch["id"]), st_code, Text(ch["title"][:28]), key=ch["id"])
        if self._chunks_items:
            if self._selected_chunk_id not in {c["id"] for c in self._chunks_items}:
                act = next((c for c in self._chunks_items if c["status"] == "active"), self._chunks_items[0])
                self._selected_chunk_id = act["id"]
            sel_ch = next(c for c in self._chunks_items if c["id"] == self._selected_chunk_id)
            self.query_one("#chunk-goal-detail", Static).update(
                f"[bold #38bdf8]{esc(sel_ch['id'])}[/] ({esc(sel_ch['status'])})\n"
                f"{esc(sel_ch['title'])}\n[#cbd5e1]{esc(sel_ch.get('goal') or '')}[/]"
            )
        else:
            self._selected_chunk_id = ""
            self.query_one("#chunk-goal-detail", Static).update("No chunks yet — click Flesh Out Spec & Chunks from Studio.")
        spec_ed = self.query_one("#ta-spec-editor", TextArea)
        if self.focused is not spec_ed:
            sp = self.repo / ".openfarm" / "PROJECT_SPEC.md"
            spec_ed.text = sp.read_text(encoding="utf-8", errors="replace") if sp.is_file() else ""

        # Populate Plan table
        pt = self.query_one("#plan-table", DataTable)
        pt.clear()
        self._plan_cards = []
        # A ready card behind an unfinished one is waiting, and one that fails the
        # eligibility check can't be built: the plan shows both instead of "ready".
        waiting = waiting_on(non_findings)
        states = {str(c.id): str(c.state).lower() for c in non_findings}
        for c in ready_cards + blocked_cards:
            tri = triage_card_heuristic(c)
            failures: list[str] = []
            if str(c.state).lower() == "blocked":
                st_short = "blocked"
            elif c.id in waiting:
                st_short = "waits"
            else:
                st_short = "ready"
                if cfg is not None:
                    try:
                        failures = [str(f.get("detail") or f.get("rule"))
                                    for f in check_eligibility(c, cfg, worktree=self.repo).failures]
                    except Exception as exc:  # noqa: BLE001 - one odd card must not blank the plan
                        failures = [f"could not be checked: {exc}"]
                if failures:
                    st_short = "fix"
            entry = {
                "id": c.id,
                "title": c.title,
                "state": str(c.state).lower(),
                "category": c.category,
                "touches": list(c.touches),
                "depends": list(c.deps),
                "tests": [t.as_dict() for t in c.tests],
                "done_text": c.done_text,
                "profile": str(tri.get("recommended_profile") or "balanced"),
                "eligible": not failures,
                "failures": failures,
                "plan_state": st_short,
                "waits_for": waiting.get(c.id, []),
                "depends_states": [(d, states.get(d, "missing")) for d in c.deps],
            }
            self._plan_cards.append(entry)
            pt.add_row(Text(c.id), Text(c.title[:18]), st_short, Text(", ".join(c.deps) or "-"),
                       Text(entry["profile"][:4]), key=c.id)
        if self._plan_cards:
            if self._selected_plan_id not in {e["id"] for e in self._plan_cards}:
                self._selected_plan_id = self._plan_cards[0]["id"]
            sel_plan = next(e for e in self._plan_cards if e["id"] == self._selected_plan_id)
            self._render_plan_detail(sel_plan)
        else:
            self._selected_plan_id = ""
        matrix = compute_touches_conflict_matrix(ready_cards)
        if matrix["conflict_count"]:
            c_pairs = ", ".join(f"{c['a']}↔{c['b']}" for c in matrix["conflicts"][:3])
            conflict_line = f"\n[bold #fbbf24]![/] {matrix['conflict_count']} conflict(s) ({esc(c_pairs)}) · {matrix['max_parallelism']} parallel lane(s)"
        else:
            conflict_line = f"\n[bold #4ade80]✓[/] 0 touches conflicts · {matrix['max_parallelism']} parallel lane(s)"
        base_line = ""
        if self._last_baseline_ok is True:
            base_line = "\n[bold #4ade80]✓[/] Baseline tests passed"
        elif self._last_baseline_ok is False:
            base_line = f"\n[bold #fb7185]✗ Baseline failed:[/] {esc(self._last_baseline_error[:120])}"
        counts = {k: sum(1 for e in self._plan_cards if e["plan_state"] == k)
                  for k in ("ready", "waits", "blocked", "fix")}
        tally = f"{counts['ready']} can start now"
        if counts["waits"]:
            tally += f"  ·  {counts['waits']} waiting"
        if counts["blocked"]:
            tally += f"  ·  {counts['blocked']} blocked"
        if counts["fix"]:
            tally += f"  ·  {counts['fix']} can't be built yet"
        waits_line = f"\n[bold #fbbf24]⏳[/] {esc(waiting_text(waiting))}" if waiting else ""
        self.query_one("#plan-preflight", Static).update(
            f"[bold {'#4ade80' if counts['ready'] else '#fbbf24'}]{'✓' if counts['ready'] else '○'}[/] {tally}  ·  "
            f"{self._findings_counts['picked']} picked finding(s){waits_line}\n"
            f"[bold #38bdf8]✓[/] Worker: {esc(gp['worker'].get('model') or '(repo config)')}"
            f"{conflict_line}{base_line}"
        )

        # Populate Review table
        rt = self.query_one("#review-table", DataTable)
        rt.clear()
        self._review_cards = in_review + blocked_cards + ready_cards
        for c in self._review_cards:
            st = str(c.state).lower().replace("_", "-")
            rt.add_row(Text(c.id), st, Text(c.title[:28]), key=c.id)
        if self._review_cards:
            if self._selected_review_id not in {c.id for c in self._review_cards}:
                self._selected_review_id = self._review_cards[0].id
            sel_card = next(c for c in self._review_cards if c.id == self._selected_review_id)
            self._render_review_card(sel_card, cfg)
        else:
            self.query_one("#review-gates", Static).update("No cards yet — plan a wave in [1 Studio].")
            self.query_one("#review-card-findings", Static).update("No findings filed yet.")

        # Populate Findings Inbox table
        self._refresh_findings_widgets()

        # Populate Projects Manager table
        self._refresh_projects_widgets()

        # Populate Engine diagnostics
        self._refresh_engine_widgets(cfg, gp)
        self._update_token_widgets(ws.get("economics") if isinstance(ws, dict) else None)
        self._refresh_run_controls()

    def _set_step(self, selector: str, label: str, state: str) -> None:
        w = self.query_one(selector, Static)
        w.remove_class("step-done", "step-cur", "step-pend")
        if state == "done":
            w.add_class("step-done")
            w.update(f"✓ {label}")
        elif state == "current":
            w.add_class("step-cur")
            w.update(f"▶ {label}")
        else:
            w.add_class("step-pend")
            w.update(f"○ {label}")

    def _update_token_widgets(self, econ: dict | None = None) -> None:
        wt = self._worker_tokens
        st = self._supervisor_tokens
        fr = self._tokens_fresh
        ca = self._tokens_cached
        if wt == 0 and st == 0 and isinstance(econ, dict):
            wt = int(econ.get("worker_tokens", 0) or 0)
            st = int(econ.get("supervisor_tokens", 0) or 0)
            fr = int(econ.get("tokens_in_fresh", 0) or 0)
            ca = int(econ.get("tokens_in_cached", 0) or 0)
        tot = wt + st
        w_pct = int(round((wt / tot) * 100)) if tot > 0 else 0
        f_pct = (100 - w_pct) if tot > 0 else 0
        c_pct = int(round((ca / (fr + ca)) * 100)) if (fr + ca) > 0 else 0
        if tot > 0:
            self.query_one("#token-summary", Static).update(
                f"Tokens: {tot:,}  ·  W {w_pct}% / F {f_pct}%  ·  {c_pct}% cached"
            )
        else:
            self.query_one("#token-summary", Static).update("W —% · F —% (idle)")
        pb = self.query_one("#worker-bar", ProgressBar)
        pb.update(total=100, progress=w_pct)
        self.query_one("#econ-stats", Static).update(
            f"[bold #4ade80]■ Worker {w_pct}% ({wt:,})[/]  [bold #c084fc]■ Frontier {f_pct}% ({st:,})[/]\n"
            f"[#94a3b8]Input: {fr:,} fresh · {ca:,} cached ({c_pct}%)[/]"
        )
        self.query_one("#econ-spark", Sparkline).data = list(self._spark_points[-20:])

    def _render_plan_detail(self, entry: dict) -> None:
        touches = ", ".join(entry.get("touches") or []) or "(none)"
        tests_lines = "\n".join(
            f"  [bold #4ade80]• {esc(t.get('name', ''))}[/] ({esc(t.get('file', ''))})\n"
            f"    [#94a3b8]{esc(t.get('asserts', ''))}[/]"
            for t in (entry.get("tests") or [])
        ) or "  (none)"
        plan_state = entry.get("plan_state", "ready")
        if plan_state == "waits":
            waits = ", ".join(f"{d} ({st.replace('_', ' ')})" for d, st in entry.get("waits_for") or [])
            status = f"[bold #fbbf24 on #3a2c0a] ⏳ waits for {esc(waits)} [/]"
        elif plan_state == "blocked":
            status = "[bold #fb7185 on #3b1219] ✗ blocked: edit it, then Release Blocked to retry [/]"
        elif plan_state == "fix":
            status = "[bold #fb7185 on #3b1219] ✗ can't be built yet [/]\n" + "\n".join(
                f"  [#fb7185]• {esc(why)}[/]" for why in entry.get("failures") or [])
        else:
            status = "[bold #4ade80 on #103623] ✓ can be built [/]"
        dep_states = entry.get("depends_states") or []
        depends = ("Depends on: " + esc(", ".join(f"{d} ({st.replace('_', ' ')})" for d, st in dep_states)) + "\n"
                   if dep_states else "Depends on: nothing\n")
        self.query_one("#plan-detail", Static).update(
            f"[bold #f8fafc]{esc(entry['id'])}  ·  {esc(entry['title'])}[/]\n"
            f"[bold #c084fc on #2f1b47] {esc(entry['category'])} [/]  "
            f"[bold #4ade80 on #103623] profile: {esc(entry['profile'])} [/]  "
            f"{status}\n{depends}\n"
            f"[bold #38bdf8]SCOPE[/]\nTouches: {esc(touches)}\n\n"
            f"[bold #38bdf8]DONE SPEC[/]\n{esc(entry.get('done_text', ''))}\n\n"
            f"[bold #38bdf8]TESTS[/]\n{tests_lines}"
        )

    def _frontier_review_path(self, cfg, card_id: str) -> Path:
        return Path(cfg.state_dir) / "cards" / card_id / "frontier-review.txt"

    def _render_review_card(self, card, cfg) -> None:
        self._selected_review_id = card.id
        st = str(card.state).lower().replace("_", "-")
        self.query_one("#btn-accept-blocked", Button).set_class(st != "blocked", "hidden")
        saved = self._frontier_review_path(cfg, card.id) if cfg is not None else None
        self.query_one("#review-frontier-out", Static).update(
            esc(saved.read_text(encoding="utf-8", errors="replace")) if saved is not None and saved.is_file()
            else esc(f"No Frontier Review of {card.id} yet. Press [m] to run one."))
        self.query_one("#review-gates", Static).update(
            f"[bold #f8fafc]{esc(card.id)} · {esc(card.title)}[/]   "
            f"[bold #38bdf8 on #12384d] {esc(st)} [/]   "
            f"[bold #c084fc on #2f1b47] {esc(card.category)} [/]"
        )
        # Findings filed by this card (id, title, start of body)
        card_findings = findings_for_card(self.repo, cfg, card.id) if cfg is not None else []
        if card_findings:
            flines = [f"[bold #fbbf24]Findings filed by {esc(card.id)} ({len(card_findings)}):[/]"]
            for f in card_findings[:5]:
                flines.append(
                    f"  [bold #38bdf8]{esc(f['id'])}[/]  [bold #f8fafc]{esc(f['title'])}[/]  "
                    f"[#94a3b8]{esc(f['body_preview'][:90])}[/]"
                )
            self.query_one("#review-card-findings", Static).update("\n".join(flines))
        else:
            self.query_one("#review-card-findings", Static).update(
                f"[#94a3b8]No findings filed by {esc(card.id)}.[/]"
            )

        # Diff preview from card branch if present
        diff_text = ""
        if cfg is not None and (self.repo / ".git").exists():
            git = Git(self.repo)
            branch_matches = git.branches(f"openfarm/{card.id}-*")
            if branch_matches:
                res = git._run(["diff", f"{cfg.base_branch}...{branch_matches[0]}"], check=False)
                if res.returncode == 0:
                    diff_text = res.stdout[:12000]
        if diff_text.strip():
            self.query_one("#review-diff", Static).update(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=True, background_color="#0b1019")
            )
        else:
            self.query_one("#review-diff", Static).update(
                f"[#94a3b8](No branch diff yet for {esc(card.id)}; touches: {esc(', '.join(card.touches))})[/]"
            )

        tests_txt = "\n".join(
            f"• {t.name} ({t.file}) — {t.asserts}" for t in card.tests
        ) or "(no named tests)"
        self.query_one("#review-tests", Static).update(
            esc(f"Touches: {', '.join(card.touches)}\n\nTests:\n{tests_txt}\n\n{card.done_text}")
        )

    def _refresh_findings_widgets(self) -> None:
        c = self._findings_counts
        self.query_one("#findings-header", Static).update(
            f"Findings Inbox  ·  {c['waiting']} waiting  ·  "
            f"{c['new_since_last_run']} new since last run  ·  "
            f"{c['picked']} picked for next wave   "
            f"[#94a3b8](w pick · x dismiss · c fold · Enter full · / filter)[/]"
        )
        ft = self.query_one("#findings-table", DataTable)
        ft.clear()
        for item in self._findings_items:
            tag = " [new]" if item["is_new"] else ""
            dup = f" (×{item['duplicate_count']})" if int(item.get("duplicate_count") or 1) > 1 else ""
            ft.add_row(Text(item["id"]), Text(item["from_card"]), Text(f"{item['title'][:32]}{dup}{tag}"), key=item["id"])
        if self._findings_items:
            if self._selected_finding_id not in {f["id"] for f in self._findings_items}:
                self._selected_finding_id = self._findings_items[0]["id"]
            sel = next(f for f in self._findings_items if f["id"] == self._selected_finding_id)
            self._render_finding_detail(sel)
        else:
            self._selected_finding_id = ""
            self.query_one("#finding-detail", Static).update("No waiting findings — inbox clear.")

    def _render_finding_detail(self, item: dict) -> None:
        self._selected_finding_id = item["id"]
        new_badge = "  [bold #fbbf24 on #423310] NEW [/]" if item["is_new"] else ""
        self.query_one("#finding-detail", Static).update(
            f"[bold #f8fafc]{esc(item['id'])}  ·  {esc(item['title'])}[/]{new_badge}\n"
            f"[#94a3b8]From card:[/] [bold #38bdf8]{esc(item['from_card'])}[/] "
            f"({esc(item['from_card_title'])})\n"
            f"[#94a3b8]Created:[/]   {esc(item.get('created_at') or '—')}   "
            f"[#94a3b8]Decision:[/] {esc(item.get('decision') or 'waiting')}\n\n"
            f"{esc(item.get('body') or '(no additional detail)')}"
        )

    def _refresh_engine_widgets(self, cfg, gp: dict) -> None:
        wk = gp["worker"]
        self.query_one("#engine-providers", Static).update(
            f"[bold #38bdf8]Global Profile[/] ({esc(gp['path'])})\n"
            f"  Frontier: {esc(gp['frontier']['provider'])}\n"
            f"  Worker:   preset={esc(wk['preset'])} · endpoint={esc(wk['endpoint'] or '(unset)')} · model={esc(wk['model'] or '(unset)')}\n"
            f"  Key file: {esc(wk['key_file'])} ({'ready' if wk['key_present'] else 'missing'}) · zdr={wk['zdr']}"
        )
        bt = self.query_one("#board-table", DataTable)
        bt.clear()
        if cfg is None:
            self.query_one("#engine-harness", Static).update("(repository not initialized yet)")
            self.query_one("#engine-swarm", Static).update("(repository not initialized yet)")
            self.query_one("#engine-spec", Static).update("(repository not initialized yet)")
            self.query_one("#engine-routing", Static).update("(repository not initialized yet)")
            return
        spec_path = self.repo / ".openfarm" / "PROJECT_SPEC.md"
        spec_txt = spec_path.read_text(encoding="utf-8", errors="replace").strip() if spec_path.is_file() else "(no PROJECT_SPEC.md yet)"
        nt_lines = "\n".join(f"  - {p}" for p in (getattr(cfg, "never_touch", ()) or ())) or "  (none)"
        inv_cfg = getattr(cfg, "invariants", None)
        inv_summary = (
            f"forbidden_imports={len(getattr(inv_cfg, 'forbidden_imports', ()))} · "
            f"max_file_bytes={getattr(inv_cfg, 'max_file_bytes', 0)} · "
            f"require_exports={len(getattr(inv_cfg, 'require_exports', ()))}"
            if inv_cfg is not None
            else "(none)"
        )
        self.query_one("#engine-spec", Static).update(
            f"[bold #38bdf8]Living Project Spec[/] (.openfarm/PROJECT_SPEC.md)\n\n"
            f"{esc(spec_txt[:3500])}\n\n"
            f"[bold #fbbf24]Locked [policy] never_touch[/]\n{esc(nt_lines)}\n\n"
            f"[bold #4ade80]Architectural Invariants[/]\n  {esc(inv_summary)}"
        )
        try:
            rt = compute_routing_recommendations(self.repo)
            recs = rt.get("recommendations") or []
            if recs:
                r_lines = [
                    f"[bold #38bdf8]Adaptive Model Router[/]  ({rt.get('samples', 0)} historical card runs)\n"
                ]
                for rec in recs:
                    r_lines.append(
                        f"• [bold #f8fafc]{esc(rec['language'])} / {esc(rec['category'])}[/]  "
                        f"pass={int(rec['pass_rate']*100)}% · first-try={int(rec['first_try_rate']*100)}% "
                        f"(n={rec['samples']}, {rec['confidence']})\n"
                        f"  Recommended: [bold #4ade80]{esc(rec['recommended_model'] or 'default')}[/] · "
                        f"effort={esc(rec['recommended_effort'])} · profile={esc(rec['recommended_profile'])}"
                    )
                self.query_one("#engine-routing", Static).update("\n".join(r_lines))
            else:
                self.query_one("#engine-routing", Static).update(
                    "No completed card runs yet — empirical routing recommendations populate automatically after your first wave."
                )
        except Exception as rt_exc:
            self.query_one("#engine-routing", Static).update(f"Routing telemetry unavailable: {esc(str(rt_exc))}")
        state = StateDir(cfg.state_dir)
        self.query_one("#engine-swarm", Static).update(
            esc(
                f"Micromanager: {cfg.micromanager_adapter} ({getattr(cfg.micromanager, 'mode', 'off')})\n"
                f"Worker: {cfg.model_adapter} ({cfg.endpoint})\n"
                f"State dir: {cfg.state_dir}"
            )
        )
        h_enabled = bool(getattr(getattr(cfg, "harness", None), "enabled", False))
        self.query_one("#engine-harness", Static).update(
            esc(f"Harness enabled: {h_enabled}  ·  Dir: {state.harness()}")
        )
        # Load most recent board posts if present
        if state.runs.is_dir():
            run_dirs = sorted([p for p in state.runs.iterdir() if p.is_dir()], key=lambda p: p.name)
            for rdir in reversed(run_dirs):
                bp = rdir / "board" / "board.jsonl"
                if bp.is_file():
                    import json

                    for line in bp.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        bt.add_row(
                            Text(f"#{row.get('seq', '')}"),
                            Text(str(row.get("from", ""))),
                            Text(str(row.get("card", ""))),
                            Text(str(row.get("kind", ""))),
                            Text(str(row.get("body", ""))[:68]),
                        )
                    break

    def _refresh_projects_widgets(self) -> None:
        self._projects_items = list_projects()
        p_cnt = len(self._projects_items)
        self.query_one("#nav-projects", Button).label = f"0 Projects ({p_cnt})" if p_cnt else "0 Projects"
        prt = self.query_one("#projects-table", DataTable)
        prt.clear()
        for item in self._projects_items:
            is_active = Path(item["repo"]).expanduser().resolve() == self.repo
            mark = "●" if is_active else ""
            status = "ready" if item["configured"] else ("dir" if item["repo_exists"] else "missing")
            prt.add_row(
                mark,
                Text(item["name"][:22]),
                Text((item.get("language") or "—")[:10]),
                status,
                Text(str(item["repo"])[-30:]),
                key=item["id"],
            )
        if self._projects_items:
            if self._selected_project_id not in {p["id"] for p in self._projects_items}:
                active = next(
                    (p for p in self._projects_items if Path(p["repo"]).expanduser().resolve() == self.repo),
                    self._projects_items[0],
                )
                self._selected_project_id = active["id"]
            sel = next(p for p in self._projects_items if p["id"] == self._selected_project_id)
            self._render_project_detail(sel)
        else:
            self._selected_project_id = ""
            self.query_one("#project-detail", Static).update(
                "No projects registered yet.\n\nPress [bold #38bdf8]\\[n][/] to add an existing folder or create a new project."
            )

    def _render_project_detail(self, item: dict) -> None:
        self._selected_project_id = item["id"]
        is_active = Path(item["repo"]).expanduser().resolve() == self.repo
        badge = "  [bold #4ade80 on #103623] ACTIVE [/]" if is_active else ""
        status = (
            "[bold #4ade80]configured (.openfarm/config.toml)[/]"
            if item["configured"]
            else ("[bold #fbbf24]directory exists (not yet initialized)[/]" if item["repo_exists"] else "[bold #fb7185]directory not found on disk[/]")
        )
        state_status = "present" if item.get("state_exists") else "none"
        self.query_one("#project-detail", Static).update(
            f"[bold #f8fafc]{esc(item['name'])}[/]  [#94a3b8]({esc(item['id'])})[/]{badge}\n\n"
            f"[#94a3b8]Directory:[/]   {esc(item['repo'])}\n"
            f"[#94a3b8]Status:[/]      {status}\n"
            f"[#94a3b8]Stack:[/]       {esc(item.get('language') or 'auto')}\n"
            f"[#94a3b8]Base branch:[/] {esc(item.get('base_branch') or 'main')}\n"
            f"[#94a3b8]State cache:[/] {esc(item.get('state_dir') or '(none)')} ({state_status})\n"
            f"[#94a3b8]Idea / Note:[/] {esc(item.get('idea') or '(none)')}\n\n"
            f"[#4ade80]Note:[/] Removing a project here only removes it from Openfarm — "
            f"your project files on disk are NEVER deleted or modified."
        )

    # ── Navigation & Palette Dispatch ────────────────────────────────────────

    def action_switch_view(self, view: str) -> None:
        sw = self.query_one("#main-switcher", ContentSwitcher)
        if view in ("projects", "providers", "studio", "review", "findings", "engine"):
            sw.current = view
        if view == "studio" and not self._wave_active():
            studio_sw = self.query_one("#studio-switcher", ContentSwitcher)
            if studio_sw.current == "studio-run":
                self._back_to_plan()
        self.call_after_refresh(self._focus_typing_target)
        for vid, bid in (
            ("projects", "nav-projects"),
            ("studio", "nav-studio"),
            ("review", "nav-review"),
            ("findings", "nav-findings"),
            ("engine", "nav-engine"),
        ):
            btn = self.query_one(f"#{bid}", Button)
            if vid == view:
                btn.add_class("active")
            else:
                btn.remove_class("active")

    def action_refresh_all(self) -> None:
        self.refresh_all_views()

    def dispatch_palette_action(self, action_id: str) -> None:
        if action_id in ("projects", "providers", "review", "findings", "engine"):
            self.action_switch_view(action_id)
        elif action_id == "grill":
            self.action_switch_view("studio")
            self.query_one("#studio-switcher", ContentSwitcher).current = "studio-grill"
        elif action_id == "run":
            self.action_switch_view("studio")
            self.start_nonblocking_wave()
        elif action_id == "land":
            self.action_switch_view("review")
            self._land_approved_queue()
        elif action_id == "frontier-review":
            self.action_switch_view("review")
            self._run_frontier_review_bg()
        elif action_id in ("pause", "resume", "stop"):
            self._run_cli_quick([action_id])

    # ── Public Testable Actions (Providers, Findings, Wave Run) ──────────────

    def save_providers_form(
        self,
        *,
        frontier: str = "claude",
        preset: str = "openrouter",
        endpoint: str = "",
        model: str = "",
        api_key: str = "",
        zdr: bool = True,
        key_file: Path | None = None,
    ) -> dict:
        res = save_global_providers(
            frontier_provider=frontier,
            worker_preset=preset,
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            zdr=zdr,
            key_file=key_file,
        )
        self.refresh_all_views()
        return res

    def apply_finding_decision(
        self,
        finding_id: str,
        decision: str,
        *,
        reason: str = "",
        target_card: str = "",
    ) -> dict | None:
        cfg = self._safe_cfg()
        if cfg is None or not finding_id:
            return None
        res = decide_finding(
            self.repo,
            cfg,
            finding_id,
            decision,
            reason=reason,
            target_card=target_card,
        )
        self.refresh_all_views()
        self.notify(f"{finding_id}: {decision}")
        return res

    def switch_active_project(self, project_id: str) -> dict | None:
        proj = get_project(project_id)
        if proj is None:
            return None
        target = Path(proj["repo"]).expanduser().resolve()
        self.repo = target
        self.config_path = None
        self._stack_user_override = False
        self._grill_history.clear()
        self._grill_pending_question = ""
        self._grill_options = []
        self._render_grill_chat()
        self._load_idea_draft()
        self._selected_review_id = ""
        self._selected_finding_id = ""
        self._approved_to_land.clear()
        self._card_phases.clear()
        self._worker_tokens = 0
        self._supervisor_tokens = 0
        self._tokens_fresh = 0
        self._tokens_cached = 0
        self._spark_points = [0.0, 0.0, 0.0, 0.0, 0.0]
        try:
            register_project(target, project_id=proj["id"])
        except (OSError, Refused) as exc:
            self.log.warning(f"could not update last_opened: {exc}")
        self.refresh_all_views()
        self.notify(f"Switched project → {proj['name']}")
        return proj

    def apply_project_add(
        self,
        *,
        name: str,
        repo: str,
        language: str = "typescript",
        idea: str = "",
        initialize: bool = False,
    ) -> dict:
        target = Path(repo or str(self.repo)).expanduser().resolve()
        if initialize and (not target.exists() or not any(target.iterdir())):
            initialize_project(
                name or target.name or "my-app",
                directory=target,
                language=language or "typescript",
                idea=idea,
                commit=True,
            )
            entry = register_project(target, name=name or target.name, language=language, idea=idea)
        else:
            entry = register_project(target, name=name or target.name, language=language, idea=idea)
        self._selected_project_id = entry["id"]
        self.refresh_all_views()
        self.notify(f"Added project {entry['name']}")
        return entry

    def apply_project_edit(
        self,
        project_id: str,
        *,
        name: str | None = None,
        repo: str | None = None,
        language: str | None = None,
        base_branch: str | None = None,
        idea: str | None = None,
    ) -> dict:
        was_active = False
        prev = get_project(project_id)
        if prev is not None and Path(prev["repo"]).expanduser().resolve() == self.repo:
            was_active = True
        updated = edit_project(
            project_id,
            name=name,
            repo=repo,
            language=language,
            base_branch=base_branch,
            idea=idea,
        )
        if was_active and updated.get("repo"):
            self.repo = Path(updated["repo"]).expanduser().resolve()
            self.config_path = None
        self.refresh_all_views()
        self.notify(f"Updated project {updated['name']}")
        return updated

    def apply_project_remove(
        self,
        project_id: str,
        *,
        clear_state_dir: bool = False,
    ) -> dict:
        prev = get_project(project_id)
        was_active = bool(prev is not None and Path(prev["repo"]).expanduser().resolve() == self.repo)
        res = remove_project(project_id, clear_state_dir=clear_state_dir)
        if was_active:
            remaining = list_projects()
            if remaining:
                self.repo = Path(remaining[0]["repo"]).expanduser().resolve()
                self.config_path = None
        self.refresh_all_views()
        self.notify(f"Removed {project_id} from Openfarm (project files on disk untouched)")
        return res

    # ── Keyboard Shortcuts in Context ────────────────────────────────────────

    def on_key(self, event) -> None:
        if isinstance(self.focused, (Input, TextArea)):
            if event.key == "escape" and self.focused.id == "findings-filter":
                self.query_one("#findings-filter", Input).value = ""
                self._findings_query = ""
                self.refresh_all_views()
                self.query_one("#findings-table", DataTable).focus()
            return
        if len(self.screen_stack) > 1:
            return  # a dialog is open: its keys are its own

        target = self._typing_target()
        if target is not None and event.character and event.character.isalpha():
            target.focus()
            if isinstance(target, TextArea):
                target.insert(event.character)
            else:
                target.insert_text_at_cursor(event.character)
            event.stop()
            event.prevent_default()
            return

        cur_view = self.query_one("#main-switcher", ContentSwitcher).current
        if cur_view == "projects":
            if event.key == "enter" and self._selected_project_id:
                self.switch_active_project(self._selected_project_id)
                self.action_switch_view("studio")
                event.stop()
            elif event.key == "n":
                self._open_new_project_modal()
                event.stop()
            elif event.key == "S":
                self._open_workspace_scan_modal()
                event.stop()
            elif event.key == "e" and self._selected_project_id:
                self._open_edit_project_modal()
                event.stop()
            elif event.key == "d" and self._selected_project_id:
                self._open_delete_project_modal()
                event.stop()
        elif cur_view == "findings":
            if event.key == "w" and self._selected_finding_id:
                self.apply_finding_decision(self._selected_finding_id, "next-wave")
                event.stop()
            elif event.key == "x" and self._selected_finding_id:
                self._open_dismiss_modal()
                event.stop()
            elif event.key == "X" and self._selected_finding_id:
                self._bulk_dismiss_from_selected_card()
                event.stop()
            elif event.key == "W":
                self._synthesize_wave_from_findings()
                event.stop()
            elif event.key == "c" and self._selected_finding_id:
                self._open_fold_modal()
                event.stop()
            elif event.key == "enter" and self._selected_finding_id:
                self._open_full_finding_modal()
                event.stop()
            elif event.key == "slash":
                self.query_one("#findings-filter", Input).focus()
                event.stop()
        elif cur_view == "review":
            if event.key == "a" and self._selected_review_id:
                self._approve_selected_card()
                event.stop()
            elif event.key == "L":
                self._land_approved_queue()
                event.stop()
            elif event.key == "b" and self._selected_review_id:
                self._open_bounce_modal()
                event.stop()
            elif event.key == "m" and self._selected_review_id:
                self._run_frontier_review_bg()
                event.stop()
        elif cur_view == "studio":
            sub = self.query_one("#studio-switcher", ContentSwitcher).current
            if sub == "studio-run" and self._wave_active():
                if event.key == "p":
                    self._pause_btn()
                    event.stop()
                elif event.key == "s":
                    self._stop_btn()
                    event.stop()
                elif event.key == "h":
                    self._open_hint_modal()
                    event.stop()
            elif sub == "studio-plan" and self._selected_plan_id:
                if event.key == "e":
                    self._open_card_editor_modal()
                    event.stop()
                elif event.key == "x":
                    self._open_split_card_modal()
                    event.stop()

    # ── Widget Event Handlers ────────────────────────────────────────────────

    @on(Button.Pressed, "#nav-projects")
    def _nav_projects(self) -> None:
        self.action_switch_view("projects")

    @on(Button.Pressed, "#nav-studio")
    def _nav_studio(self) -> None:
        self.action_switch_view("studio")

    @on(Button.Pressed, "#nav-review")
    def _nav_review(self) -> None:
        self.action_switch_view("review")

    @on(Button.Pressed, "#nav-findings")
    def _nav_findings(self) -> None:
        self.action_switch_view("findings")

    @on(Button.Pressed, "#nav-engine")
    def _nav_engine(self) -> None:
        self.action_switch_view("engine")

    @on(Button.Pressed, "#btn-open-providers")
    @on(Button.Pressed, "#btn-eng-providers")
    def _open_providers(self) -> None:
        self.action_switch_view("providers")

    @on(Button.Pressed, "#btn-regrill")
    def _regrill(self) -> None:
        self.query_one("#studio-switcher", ContentSwitcher).current = "studio-grill"

    @on(Button.Pressed, "#btn-jump-review")
    def _jump_review(self) -> None:
        self.action_switch_view("review")

    @on(Button.Pressed, "#btn-acp-login")
    def _acp_login(self) -> None:
        gp = load_global_providers()
        prov = gp["frontier"]["provider"]
        with self.suspend():
            subprocess.run(self._cli_argv("acp", "login", "--provider", prov), env=self._cli_env())
        self._load_providers_into_form()

    @on(RadioSet.Changed, "#worker-presets")
    def _preset_changed(self, event: RadioSet.Changed) -> None:
        idx = event.radio_set.pressed_index
        if 0 <= idx < len(WORKER_PRESETS):
            p = WORKER_PRESETS[idx]
            if p["endpoint"]:
                self.query_one("#inp-endpoint", Input).value = p["endpoint"]
            if p["default_model"]:
                self.query_one("#inp-model", Input).value = p["default_model"]
            self.query_one("#sw-zdr", Switch).value = bool(p["zdr"])

    @on(Button.Pressed, "#btn-probe")
    def _probe_clicked(self) -> None:
        self._run_probe_bg()

    @work(thread=True)
    def _run_probe_bg(self) -> None:
        ep = self.query_one("#inp-endpoint", Input).value.strip()
        mdl = self.query_one("#inp-model", Input).value.strip()
        key = self.query_one("#inp-key", Input).value.strip()
        zdr = bool(self.query_one("#sw-zdr", Switch).value)
        gp = load_global_providers()
        kf = Path(gp["worker"]["key_file"]) if not key else None
        res = probe_worker_endpoint(ep, key_file=kf, api_key=key, model=mdl, zdr=zdr)
        if res["ok"]:
            msg = f"[bold #4ade80]✓ Live Probe Passed[/]  {res['latency_ms']}ms · HTTP {res['status']} · zdr={res['zdr_sent']} · models={res['models_count']}"
        else:
            err_detail = str(res["error"] or f"HTTP {res['status']}")
            msg = f"[bold #fb7185]✗ Probe Failed[/]  {esc(err_detail)}"
        self.call_from_thread(self.query_one("#probe-status", Static).update, msg)

    @on(Button.Pressed, "#btn-save-prov")
    def _save_prov_clicked(self) -> None:
        rs = self.query_one("#worker-presets", RadioSet)
        idx = rs.pressed_index if rs.pressed_index >= 0 else 0
        preset_id = WORKER_PRESETS[idx]["id"] if 0 <= idx < len(WORKER_PRESETS) else "custom"
        ep = self.query_one("#inp-endpoint", Input).value.strip()
        mdl = self.query_one("#inp-model", Input).value.strip()
        key = self.query_one("#inp-key", Input).value.strip()
        zdr = bool(self.query_one("#sw-zdr", Switch).value)
        try:
            self.save_providers_form(preset=preset_id, endpoint=ep, model=mdl, api_key=key, zdr=zdr)
            self.notify("Saved global provider profile")
            self.action_switch_view("studio")
        except (Refused, AdapterError, ConfigError) as exc:
            self.notify(f"Provider error: {exc}", severity="error")

    def _apply_stack_chip(self, stack_id: str) -> None:
        self._selected_stack = stack_id
        for lang_id, label in (
            ("typescript", "TypeScript / Web"),
            ("python", "Python"),
            ("rust", "Rust"),
            ("go", "Go"),
            ("auto", "Auto-detect"),
        ):
            btn = self.query_one(f"#stk-{lang_id}", Button)
            if lang_id == self._selected_stack:
                btn.add_class("selected")
                btn.label = f"● {label}"
            else:
                btn.remove_class("selected")
                btn.label = f"○ {label}"

    @on(Button.Pressed, ".stack-chip")
    def _stack_selected(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if not bid.startswith("stk-"):
            return
        self._stack_user_override = True
        self._apply_stack_chip(bid[4:])

    def _render_grill_chat(self) -> None:
        chat_box = self.query_one("#grill-chat-box", Vertical)
        if not self._grill_history and not self._grill_pending_question:
            chat_box.add_class("hidden")
            return
        chat_box.remove_class("hidden")
        if self._grill_from_template:
            lines = ["[bold #f59e0b]Basic questions (the Architect was not asked)[/]"]
            asker = "Question:"
        else:
            lines = ["[bold #38bdf8]Questions from the Architect[/]"]
            asker = "Architect:"
        for turn in self._grill_history:
            lines.append(f"[bold #cbd5e1]{asker}[/] {esc(str(turn.get('question') or ''))}")
            lines.append(f"[bold #4ade80]You:[/]       {esc(str(turn.get('answer') or ''))}")
        if self._grill_pending_question:
            lines.append(f"\n[bold #fbbf24]{asker}[/] {esc(self._grill_pending_question)}")
        else:
            lines.append("\n[bold #4ade80]✓ That's enough to go on. Next: Flesh Out Spec & Chunks, or ask another question.[/]")
        self.query_one("#grill-transcript", Static).update("\n".join(lines))
        opt_row = self.query_one("#grill-options-row", Horizontal)
        if self._grill_options and self._grill_pending_question:
            opt_row.remove_class("hidden")
            for idx in range(3):
                btn = self.query_one(f"#btn-grill-opt-{idx}", Button)
                if idx < len(self._grill_options):
                    btn.label = esc(self._grill_options[idx][:48])
                    btn.remove_class("hidden")
                else:
                    btn.add_class("hidden")
        else:
            opt_row.add_class("hidden")

    def _log_architect(self, message: str, *, style: str = "#cbd5e1") -> None:
        if not message:
            return
        ts = time.strftime("%H:%M:%S")
        try:
            self.query_one("#architect-log", RichLog).write(
                f"[#64748b][{ts}][/] [{style}]{rich_escape(message)}[/]"
            )
        except Exception:
            pass

    @on(Button.Pressed, "#btn-arch-cancel")
    def _arch_cancel_clicked(self) -> None:
        if not self._drafting_plan:
            return
        self._architect_cancelled = True
        backend = self._active_architect_backend
        self._active_architect_backend = None
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception as exc:
                self.log.warning(f"architect cancel close error: {exc}")
        self._set_drafting_state(False, "[bold #f59e0b]■ Architect cancelled by user[/]")
        self.notify("Architect call cancelled.", severity="warning")

    @on(Button.Pressed, "#btn-arch-hide")
    def _arch_hide_clicked(self) -> None:
        self.query_one("#architect-panel", Vertical).add_class("hidden")

    def _set_drafting_state(self, active: bool, message: str = "") -> None:
        was_active = self._drafting_plan
        self._drafting_plan = bool(active)
        if active and not self._draft_started_at:
            self._draft_started_at = time.monotonic()
            self._draft_last_heartbeat = 0
        elif not active:
            self._draft_started_at = 0.0
            self._draft_last_heartbeat = 0
        prev_msg = self._draft_phase_msg
        self._draft_phase_msg = message
        for bid, busy_lbl, idle_lbl in (
            ("#btn-grill-ask", "⏳ Grilling...", "Ask Architect to Grill Me"),
            ("#btn-draft-spec", "⏳ Drafting Spec...", "Flesh Out Spec & Chunks"),
            ("#btn-chunk-to-cards", "⏳ Splitting Chunk...", "Split Chunk into Cards"),
            ("#btn-draft-plan", "⏳ Architecting...", "Quick: Draft Cards Now"),
        ):
            try:
                b = self.query_one(bid, Button)
                b.disabled = active
                b.label = busy_lbl if (active and bid == self._busy_button) else idle_lbl
            except Exception:
                pass
        self._architect_failure = None
        for bid in ("#btn-arch-retry", "#btn-arch-template"):
            self.query_one(bid, Button).add_class("hidden")
        try:
            cancel_btn = self.query_one("#btn-arch-cancel", Button)
            if active:
                cancel_btn.remove_class("hidden")
            else:
                cancel_btn.add_class("hidden")
        except Exception:
            pass
        pill = self.query_one("#status-badge", Static)
        panel = self.query_one("#architect-panel", Vertical)
        banner = self.query_one("#architect-banner", Static)
        status_box = self.query_one("#grill-status", Static)
        if active:
            pill.remove_class("status-ready")
            pill.add_class("status-running")
            pill.update("● ARCHITECTING")
            panel.remove_class("hidden")
            status_box.remove_class("hidden")
            if not was_active:
                try:
                    self.query_one("#architect-log", RichLog).clear()
                except Exception:
                    pass
            if message and message != prev_msg:
                self._log_architect(message, style="#38bdf8")
            self._render_draft_status()
        else:
            if not self._running_wave:
                pill.remove_class("status-running")
                pill.add_class("status-ready")
                pill.update("○ READY")
            if message:
                panel.remove_class("hidden")
                banner.update(message)
                status_box.remove_class("hidden")
                status_box.update(message)
                plain = (
                    message.replace("[bold #4ade80]", "")
                    .replace("[bold #fb7185]", "")
                    .replace("[bold #f59e0b]", "")
                    .replace("[/]", "")
                    .replace("\\[", "[")
                )
                log_color = "#fb7185" if "✗" in plain else ("#f59e0b" if "■" in plain else "#4ade80")
                self._log_architect(plain, style=log_color)
            else:
                status_box.add_class("hidden")
                if was_active:
                    banner.update("[bold #4ade80]✓ Step finished[/]  [#94a3b8](Hide Log closes this)[/]")

    def _render_draft_status(self) -> None:
        if not self._drafting_plan:
            return
        elapsed = int(max(0.0, time.monotonic() - self._draft_started_at)) if self._draft_started_at else 0
        mins, secs = divmod(elapsed, 60)
        head = (
            f"[bold #38bdf8]⏳ [{mins:02d}:{secs:02d}][/]  "
            f"[bold #f8fafc]{esc(self._draft_phase_msg or 'Working...')}[/]"
        )
        self.query_one("#architect-banner", Static).update(head)
        self.query_one("#grill-status", Static).update(
            f"{head}\n"
            + ("[#94a3b8]The Architect usually answers in 15 seconds to a few minutes. "
               "The log above shows what it is writing. Cancel stops it.[/]"
               if self._draft_asked else
               "[#94a3b8]Using a basic template. The Architect is not asked.[/]")
        )
        if self._draft_asked and elapsed >= 15 and elapsed - self._draft_last_heartbeat >= 15:
            self._draft_last_heartbeat = elapsed
            self._log_architect(
                f"Still waiting for the Architect's answer ({mins:02d}:{secs:02d} so far)...",
                style="#94a3b8",
            )

    def _tick_draft_timer(self) -> None:
        if self._drafting_plan:
            self._render_draft_status()

    def _architect_on(self) -> bool:
        return bool(self.query_one("#sw-use-architect", Switch).value)

    @on(Switch.Changed, "#sw-use-architect")
    def _architect_switch_changed(self, event: Switch.Changed) -> None:
        self.query_one("#sw-architect-words", Static).update(
            ARCHITECT_ON_WORDS if event.value else ARCHITECT_OFF_WORDS)
        if not event.value:
            self.notify(
                "Architect off. The plan will come from a basic template, not from the Architect.",
                severity="warning")

    def _start_architect_step(self, step: str, *, chunk_id: str | None = None, template: bool = False) -> None:
        """Start a grill, spec, split or plan step. `template` skips the Architect on purpose."""
        if self._drafting_plan:
            self.notify("Another step is still running.")
            return
        idea = self.query_one("#ta-idea", TextArea).text.strip()
        if step in ("grill", "spec") and not idea:
            self.notify("Describe what you want to build first.", severity="warning")
            self.query_one("#ta-idea", TextArea).focus()
            return
        asked = self._architect_on() and not template
        self._busy_button = STEP_BUTTONS[step]
        self._draft_asked = asked
        self._draft_doing = {
            "grill": "asking about your idea",
            "spec": "writing the spec and its chunks",
            "split": f"splitting chunk {chunk_id or 'C1'} into cards",
            "plan": "drafting cards",
        }[step]
        # Nothing says the Architect is being asked until it has really been opened.
        self._set_drafting_state(
            True,
            "Connecting to the Architect..." if asked
            else f"Basic template (the Architect is not asked): {self._draft_doing}...")
        if step == "grill":
            self._grill_ask_bg(idea, template)
        elif step == "spec":
            self._draft_spec_bg(idea, template)
        else:
            self._draft_plan_bg(chunk_id, template, step)

    def _architect_failed(self, step: str, reason: str, chunk_id: str | None = None) -> None:
        """The Architect could not be asked or did not answer: say why, offer Retry. Nothing is drafted."""
        self._set_drafting_state(False, f"[bold #fb7185]✗ The Architect didn't answer:[/] {esc(reason)}")
        self._architect_failure = {"step": step, "chunk_id": chunk_id}
        for bid in ("#btn-arch-retry", "#btn-arch-template"):
            self.query_one(bid, Button).remove_class("hidden")
        self.notify(f"The Architect didn't answer: {reason}", severity="error")

    @on(Button.Pressed, "#btn-arch-retry")
    def _arch_retry_clicked(self) -> None:
        if self._architect_failure:
            self._start_architect_step(**self._architect_failure)

    @on(Button.Pressed, "#btn-arch-template")
    def _arch_template_clicked(self) -> None:
        if self._architect_failure:
            self._start_architect_step(**self._architect_failure, template=True)

    @on(Button.Pressed, "#btn-grill-ask")
    def _grill_ask_clicked(self) -> None:
        self._start_architect_step("grill")

    @on(Button.Pressed, "#btn-grill-opt-0")
    @on(Button.Pressed, "#btn-grill-opt-1")
    @on(Button.Pressed, "#btn-grill-opt-2")
    def _grill_opt_clicked(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        try:
            idx = int(bid.rsplit("-", 1)[-1])
        except ValueError:
            return
        if 0 <= idx < len(self._grill_options):
            self._submit_grill_answer(self._grill_options[idx])

    @on(Button.Pressed, "#btn-grill-send")
    @on(Input.Submitted, "#inp-grill-answer")
    def _grill_send_clicked(self) -> None:
        inp = self.query_one("#inp-grill-answer", Input)
        ans = inp.value.strip()
        if not ans:
            return
        inp.value = ""
        self._submit_grill_answer(ans)

    def _submit_grill_answer(self, answer: str) -> None:
        q = self._grill_pending_question or "Clarification"
        self._grill_history.append({"question": q, "answer": answer})
        self._grill_pending_question = ""
        self._grill_options = []
        self._render_grill_chat()
        idea = self.query_one("#ta-idea", TextArea).text.strip()
        if idea and len(self._grill_history) < 3:
            self._start_architect_step("grill", template=self._grill_from_template)

    @on(Button.Pressed, "#btn-grill-reset")
    def _grill_reset_clicked(self) -> None:
        self._grill_history.clear()
        self._grill_pending_question = ""
        self._grill_options = []
        self._render_grill_chat()

    def _open_backend_for_step(self, step: str, template: bool, chunk_id: str | None = None):
        """Open the Architect for a worker thread. Returns (backend, name, go_on).

        `go_on` is False when the Architect was wanted but could not be opened: the
        failure is on screen and the step must stop without drafting anything.
        """
        if template or not self._architect_on():
            return None, "basic template", True
        backend, reason = open_architect_backend(self.repo, self._safe_cfg())
        if backend is None:
            self.call_from_thread(self._architect_failed, step, reason, chunk_id)
            return None, reason, False
        self._active_architect_backend = backend
        self.call_from_thread(self._set_drafting_state, True, f"{reason}: {self._draft_doing}...")
        return backend, reason, True

    def _close_backend(self, backend) -> None:
        if self._active_architect_backend is backend:
            self._active_architect_backend = None
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception as close_exc:
                self.log.warning(f"architect close error: {close_exc}")

    @work(thread=True)
    def _grill_ask_bg(self, idea: str, template: bool = False) -> None:
        self._architect_cancelled = False
        backend = None
        try:
            backend, _name, go_on = self._open_backend_for_step("grill", template)
            if not go_on:
                return
            turn = architect_grill_turn(
                self.repo,
                idea=idea,
                history=list(self._grill_history),
                language=self._selected_stack,
                backend=backend,
            )
            if self._architect_cancelled:
                return
            self._grill_from_template = backend is None
            if turn.get("ready_to_plan") or not str(turn.get("question") or "").strip():
                self._grill_pending_question = ""
                self._grill_options = []
                msg = (
                    "[bold #4ade80]✓ The Architect has what it needs. Next: Flesh Out Spec & Chunks.[/]"
                    if backend is not None else
                    "[bold #f59e0b]✓ Basic questions done (the Architect was not asked). Next: Flesh Out Spec & Chunks.[/]"
                )
            else:
                self._grill_pending_question = str(turn["question"]).strip()
                self._grill_options = [str(o) for o in (turn.get("options") or [])[:3]]
                msg = ""
            self.call_from_thread(self._set_drafting_state, False, msg)
            self.call_from_thread(self._render_grill_chat)
            if self._grill_pending_question:
                self.call_from_thread(self.query_one("#inp-grill-answer", Input).focus)
        except Exception as exc:
            if self._architect_cancelled:
                return
            if backend is not None:
                self.call_from_thread(self._architect_failed, "grill", str(exc))
            else:
                self.call_from_thread(
                    self._set_drafting_state,
                    False,
                    f"[bold #fb7185]✗ Question error:[/] {esc(str(exc))}",
                )
        finally:
            self._close_backend(backend)

    @on(Button.Pressed, "#btn-draft-spec")
    def _draft_spec_clicked(self) -> None:
        self._start_architect_step("spec")

    @work(thread=True)
    def _draft_spec_bg(self, idea: str, template: bool = False) -> None:
        self._architect_cancelled = False
        backend = None
        try:
            # The Architect is opened before anything is set up, so a failure leaves the folder as it was.
            backend, name, go_on = self._open_backend_for_step("spec", template)
            if not go_on:
                return
            self._ensure_workspace_initialized_for_studio(idea)

            def _on_spec_prog(msg: str) -> None:
                if not self._architect_cancelled:
                    self.call_from_thread(self._set_drafting_state, True, msg)

            res = draft_spec_and_chunks(
                self.repo,
                idea=idea,
                grill_history=list(self._grill_history),
                language=self._selected_stack,
                backend=backend,
                on_progress=_on_spec_prog,
            )
            if self._architect_cancelled:
                return
            made_by = "" if backend is not None else " (basic template, the Architect was not asked)"
            self.call_from_thread(
                self._set_drafting_state,
                False,
                f"[bold #4ade80]✓ Spec & {len(res['chunks'])} chunk(s) ready{made_by}. Review it, or split a chunk into cards.[/]",
            )
            self.call_from_thread(self.refresh_all_views)
            self.call_from_thread(
                setattr,
                self.query_one("#studio-switcher", ContentSwitcher),
                "current",
                "studio-spec",
            )
            self.call_from_thread(
                self.notify,
                f"Spec ready{made_by}: {len(res['chunks'])} chunk(s). Review it, or split a chunk into cards.",
            )
        except Exception as exc:
            if self._architect_cancelled:
                return
            if backend is not None:
                self.call_from_thread(self._architect_failed, "spec", str(exc))
            else:
                self.call_from_thread(
                    self._set_drafting_state,
                    False,
                    f"[bold #fb7185]✗ Spec error:[/] {esc(str(exc))}",
                )
                self.call_from_thread(self.notify, f"Spec error: {exc}", severity="error")
        finally:
            self._close_backend(backend)

    @on(DataTable.RowHighlighted, "#chunks-table")
    def _chunk_row_changed(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            cid = str(event.row_key.value)
            match = next((c for c in self._chunks_items if c["id"] == cid), None)
            if match:
                self._selected_chunk_id = cid
                self.query_one("#chunk-goal-detail", Static).update(
                    f"[bold #38bdf8]{esc(match['id'])}[/] ({esc(match['status'])})\n"
                    f"{esc(match['title'])}\n[#cbd5e1]{esc(match.get('goal') or '')}[/]"
                )

    @on(Button.Pressed, "#btn-spec-save")
    def _spec_save_clicked(self) -> None:
        sp = self.repo / ".openfarm" / "PROJECT_SPEC.md"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(self.query_one("#ta-spec-editor", TextArea).text.rstrip() + "\n", encoding="utf-8")
        self.query_one("#spec-status", Static).update("[bold #4ade80]✓ Saved .openfarm/PROJECT_SPEC.md[/]")
        self.notify("Saved PROJECT_SPEC.md")

    @on(Button.Pressed, "#btn-chunk-done")
    def _chunk_done_clicked(self) -> None:
        if not self._selected_chunk_id:
            return
        try:
            set_chunk_status(self.repo, self._selected_chunk_id, "done")
            self.refresh_all_views()
            self.notify(f"Marked {self._selected_chunk_id} done")
        except Refused as exc:
            self.notify(str(exc), severity="error")

    @on(Button.Pressed, "#btn-chunk-to-cards")
    def _chunk_to_cards_clicked(self) -> None:
        if self._drafting_plan:
            self.notify("Architect is already drafting cards...")
            return
        # Save any user edits to the spec before splitting chunk into cards
        sp = self.repo / ".openfarm" / "PROJECT_SPEC.md"
        spec_txt = self.query_one("#ta-spec-editor", TextArea).text.strip()
        if spec_txt:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(spec_txt + "\n", encoding="utf-8")
        self._start_architect_step("split", chunk_id=self._selected_chunk_id or None)

    @on(Button.Pressed, "#btn-spec-back")
    def _spec_back_clicked(self) -> None:
        self.query_one("#studio-switcher", ContentSwitcher).current = "studio-grill"

    @on(Button.Pressed, "#btn-open-spec")
    def _open_spec_clicked(self) -> None:
        self.query_one("#studio-switcher", ContentSwitcher).current = "studio-spec"

    @on(Button.Pressed, "#btn-next-iter")
    def _next_iter_clicked(self) -> None:
        self.action_switch_view("studio")
        target_sub = "studio-spec" if self._chunks_items else "studio-grill"
        self.query_one("#studio-switcher", ContentSwitcher).current = target_sub

    @on(Button.Pressed, "#btn-draft-plan")
    def _draft_plan_clicked(self) -> None:
        self._start_architect_step("plan")

    def _ensure_workspace_initialized_for_studio(self, idea: str) -> None:
        existing_cfg = self._safe_cfg()
        existing_lang = (
            str(existing_cfg.languages[0]).strip().lower()
            if (existing_cfg is not None and getattr(existing_cfg, "languages", None))
            else ""
        )
        stack_changed_on_blank = (
            self._stack_user_override
            and self._selected_stack != "auto"
            and existing_lang != ""
            and existing_lang != self._selected_stack
            and not any((self.repo / "cards").glob("W*.md"))
        )
        needs_init = (
            (not (self.repo / CONFIG_PATH).is_file())
            or self._skeleton_missing_for_cfg(self.repo, existing_cfg)
            or stack_changed_on_blank
        )
        if needs_init:
            self.call_from_thread(
                self._set_drafting_state,
                True,
                f"Initializing {self._selected_stack} project scaffold...",
            )
            initialize_project(
                self.repo.name or "my-app",
                directory=self.repo,
                idea=idea,
                language=self._selected_stack,
                force=True,
                commit=True,
            )
            cards_dir = self.repo / "cards"
            if cards_dir.is_dir():
                for old_md in cards_dir.glob("*.md"):
                    if not old_md.stem.upper().startswith("FIND-"):
                        old_md.unlink(missing_ok=True)
            fresh_cfg = self._safe_cfg()
            if fresh_cfg is not None:
                for flag_name in ("paused", "stop"):
                    (fresh_cfg.state_dir / flag_name).unlink(missing_ok=True)
                if (self.repo / ".git").exists():
                    from openfarm_core.adapters.files import FileCardSource

                    FileCardSource(
                        self.repo,
                        "cards",
                        worktree=fresh_cfg.state_dir / "cards-source",
                        base_branch=fresh_cfg.base_branch,
                    ).reset_non_finding_cards_from_base(clear_findings=bool(stack_changed_on_blank))

    @staticmethod
    def _skeleton_missing_for_cfg(repo: Path, cfg) -> bool:
        if cfg is None:
            return True
        lang = (cfg.languages[0] if getattr(cfg, "languages", None) else "rust").lower()
        if lang == "rust":
            return not (repo / "Cargo.toml").is_file()
        if lang == "typescript":
            return not (repo / "package.json").is_file()
        if lang == "go":
            return not (repo / "go.mod").is_file()
        if lang == "python":
            return not ((repo / "pyproject.toml").is_file() or any((repo / "src").rglob("*.py")) if (repo / "src").is_dir() else (repo / "pyproject.toml").is_file())
        return False

    @work(thread=True)
    def _draft_plan_bg(self, chunk_id: str | None = None, template: bool = False, step: str = "plan") -> None:
        idea = self.query_one("#ta-idea", TextArea).text.strip() or DEFAULT_GRILL_QUESTIONS[0]["default"]
        self._architect_cancelled = False
        backend = None
        try:
            backend, name, go_on = self._open_backend_for_step(step, template, chunk_id)
            if not go_on:
                return
            self._ensure_workspace_initialized_for_studio(idea)

            def _on_prog(msg: str) -> None:
                if not self._architect_cancelled:
                    self.call_from_thread(self._set_drafting_state, True, f"{msg} ({name})")

            try:
                plan = draft_wave_plan(
                    self.repo,
                    idea=idea,
                    grill_history=list(self._grill_history),
                    chunk_id=chunk_id,
                    backend=backend,
                    include_picked_findings=True,
                    verify_baseline=True,
                    on_progress=_on_prog,
                )
            except Exception as arch_exc:
                if self._architect_cancelled:
                    return
                if backend is None:
                    raise
                # A failed Architect turn is a failure. It never turns into a template by itself.
                self.call_from_thread(self._architect_failed, step, str(arch_exc), chunk_id)
                return
            if self._architect_cancelled:
                return
            self._last_baseline_ok = bool(plan.get("baseline_ok", True))
            self._last_baseline_error = str(plan.get("baseline_error") or "")
            self.call_from_thread(self._set_drafting_state, False, "")
            self.call_from_thread(self.refresh_all_views)
            self.call_from_thread(
                setattr,
                self.query_one("#studio-switcher", ContentSwitcher),
                "current",
                "studio-plan",
            )
            cards = plan["cards"]
            can_build = sum(1 for c in cards if c.get("eligible"))
            parts = [f"Plan ready ({name}): {len(cards)} card(s), {can_build} can be built"]
            if plan.get("retired_cards"):
                parts.append(f"replaced unbuilt {', '.join(plan['retired_cards'])}")
            parts.append("baseline tests pass" if self._last_baseline_ok
                         else f"baseline tests fail ({self._last_baseline_error[:60]})")
            self.call_from_thread(
                self.notify,
                " · ".join(parts),
                severity=("warning" if not self._last_baseline_ok or can_build < len(cards) else "information"),
            )
        except Exception as exc:
            if self._architect_cancelled:
                return
            self.call_from_thread(
                self._set_drafting_state,
                False,
                f"[bold #fb7185]✗ Plan error:[/] {esc(str(exc))}",
            )
            self.call_from_thread(self.notify, f"Plan error: {exc}", severity="error")
        finally:
            self._close_backend(backend)

    @on(DataTable.RowHighlighted, "#projects-table")
    def _project_row_changed(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            pid = str(event.row_key.value)
            match = next((p for p in self._projects_items if p["id"] == pid), None)
            if match:
                self._render_project_detail(match)

    @on(Button.Pressed, "#btn-proj-switch")
    def _proj_switch_btn(self) -> None:
        if self._selected_project_id:
            self.switch_active_project(self._selected_project_id)
            self.action_switch_view("studio")

    @on(Button.Pressed, "#btn-proj-new")
    def _open_new_project_modal(self) -> None:
        base_parent = Path.home() / "projects" if (_is_ephemeral_tmp_path(self.repo) or not self.repo.is_dir()) else self.repo.parent
        default_dir = str(base_parent / "new-project")

        def _done(data: dict | None) -> None:
            if not data or not data.get("repo"):
                return
            try:
                self.apply_project_add(
                    name=data.get("name", ""),
                    repo=data["repo"],
                    language=data.get("language", "typescript"),
                    idea=data.get("idea", ""),
                    initialize=bool(data.get("initialize", False)),
                )
            except (OSError, Refused, ConfigError) as exc:
                self.notify(f"Project error: {exc}", severity="error")

        self.push_screen(NewProjectModal(default_dir=default_dir), _done)

    @on(Button.Pressed, "#btn-proj-edit")
    def _open_edit_project_modal(self) -> None:
        item = next((p for p in self._projects_items if p["id"] == self._selected_project_id), None)
        if item is None:
            return

        def _done(data: dict | None) -> None:
            if not data:
                return
            try:
                self.apply_project_edit(
                    item["id"],
                    name=data.get("name"),
                    repo=data.get("repo"),
                    language=data.get("language"),
                    base_branch=data.get("base_branch"),
                    idea=data.get("idea"),
                )
            except (OSError, Refused, ConfigError) as exc:
                self.notify(f"Edit error: {exc}", severity="error")

        self.push_screen(EditProjectModal(item), _done)

    @on(Button.Pressed, "#btn-proj-delete")
    def _open_delete_project_modal(self) -> None:
        item = next((p for p in self._projects_items if p["id"] == self._selected_project_id), None)
        if item is None:
            return

        def _done(data: dict | None) -> None:
            if not data or not data.get("confirm"):
                return
            try:
                self.apply_project_remove(
                    item["id"],
                    clear_state_dir=bool(data.get("clear_state_dir", False)),
                )
            except (OSError, Refused) as exc:
                self.notify(f"Remove error: {exc}", severity="error")

        self.push_screen(DeleteProjectModal(item), _done)

    @on(Button.Pressed, "#btn-proj-scan")
    def _open_workspace_scan_modal(self) -> None:
        scan_root = Path.home() / "projects"
        items = discover_workspace_projects(scan_root)
        if not items:
            self.notify(f"No project directories found under {scan_root}")
            return

        def _done(data: dict | None) -> None:
            if not data or not data.get("item"):
                return
            item = data["item"]
            action = data.get("action", "register")
            try:
                if action == "adopt" and item["kind"] != "configured":
                    adopt_project(Path(item["repo"]), run_suite=False)
                entry = register_project(
                    item["repo"],
                    name=item["name"],
                    language=item.get("detected_language") or "",
                )
                self._selected_project_id = entry["id"]
                self.refresh_all_views()
                self.notify(f"{action.title()}ed {entry['name']}")
            except (OSError, Refused, ConfigError) as exc:
                self.notify(f"Workspace scan error: {exc}", severity="error")

        self.push_screen(WorkspaceDiscoveryModal(str(scan_root), items), _done)

    @on(Button.Pressed, "#btn-plan-edit")
    def _open_card_editor_modal(self) -> None:
        entry = next((e for e in self._plan_cards if e["id"] == self._selected_plan_id), None)
        if entry is None:
            self.notify("Select a card in the Wave Plan first")
            return

        def _done(data: dict | None) -> None:
            if not data:
                return
            try:
                res = update_card_with_eligibility(
                    self.repo,
                    entry["id"],
                    title=data.get("title"),
                    category=data.get("category"),
                    touches=data.get("touches"),
                    depends=data.get("depends"),
                    done_text=data.get("done_text"),
                )
                self.refresh_all_views()
                if res["eligible"]:
                    self.notify(f"Saved {entry['id']}. It can be built.")
                else:
                    why = "; ".join(str(f.get("detail") or f.get("rule")) for f in res["failures"][:2])
                    self.notify(f"Saved {entry['id']}, but it can't be built yet: {why}", severity="warning")
            except Refused as exc:
                self.notify(f"Not saved: {exc.message}", severity="warning")
            except (OSError, ConfigError) as exc:
                self.notify(f"Not saved: {exc}", severity="error")

        self.push_screen(CardEditorModal(entry), _done)

    @on(Button.Pressed, "#btn-plan-release")
    def _release_blocked_cards_btn(self) -> None:
        blocked = [e["id"] for e in self._plan_cards if e.get("state") == "blocked"]
        if not blocked:
            self.notify("No blocked cards to release")
            return
        for cid in blocked:
            self._run_cli_quick(["release", cid, "--reason", "released from Studio Plan for retry"])
        self.refresh_all_views()

    @on(Button.Pressed, "#btn-plan-split")
    def _open_split_card_modal(self) -> None:
        entry = next((e for e in self._plan_cards if e["id"] == self._selected_plan_id), None)
        if entry is None:
            self.notify("Select a card in the Wave Plan first")
            return

        def _done(data: dict | None) -> None:
            if not data:
                return
            try:
                res = split_card_into_two(
                    self.repo,
                    entry["id"],
                    first_title=data["first_title"],
                    first_touches=data["first_touches"],
                    second_title=data["second_title"],
                    second_touches=data["second_touches"],
                )
                self.refresh_all_views()
                ids = ", ".join(c["id"] for c in res["cards"])
                self.notify(f"Split {entry['id']} → {ids}")
            except (OSError, Refused, ConfigError) as exc:
                self.notify(f"Split error: {exc}", severity="error")

        self.push_screen(SplitCardModal(entry), _done)

    @on(Button.Pressed, "#btn-find-bulk-dismiss")
    def _bulk_dismiss_from_selected_card(self) -> None:
        item = next((f for f in self._findings_items if f["id"] == self._selected_finding_id), None)
        cfg = self._safe_cfg()
        if item is None or cfg is None or not item.get("from_card"):
            return
        res = bulk_decide_findings(
            self.repo,
            cfg,
            decision="dismissed",
            from_card=item["from_card"],
            reason=f"bulk dismissed findings from {item['from_card']}",
        )
        self.refresh_all_views()
        self.notify(f"Bulk dismissed {res['count']} finding(s) from {item['from_card']}")

    @on(Button.Pressed, "#btn-find-synth")
    def _synthesize_wave_from_findings(self) -> None:
        if self._findings_counts.get("picked", 0) <= 0:
            self.notify("Pick at least one finding with [w] before synthesizing a wave", severity="warning")
            return
        try:
            plan = draft_wave_plan(
                self.repo,
                answers=[{"id": "goal", "question": "Goal", "answer": "Synthesize picked worker findings into Wave cards"}],
                backend=None,
                include_picked_findings=True,
                verify_baseline=False,
            )
            self.refresh_all_views()
            self.action_switch_view("studio")
            self.query_one("#studio-switcher", ContentSwitcher).current = "studio-plan"
            self.notify(f"Synthesized {len(plan['promoted_findings'])} finding(s) into Wave Plan")
        except (OSError, Refused, ConfigError) as exc:
            self.notify(f"Synthesis error: {exc}", severity="error")

    @on(Button.Pressed, "#btn-doc-run")
    def _run_doctor_btn(self) -> None:
        self._render_doctor_report(fix=False)

    @on(Button.Pressed, "#btn-doc-fix")
    def _fix_doctor_btn(self) -> None:
        self._render_doctor_report(fix=True)

    def _render_doctor_report(self, *, fix: bool = False) -> None:
        rep = run_doctor(self.repo, config_path=self.config_path, fix=fix)
        icons = {"ok": "[bold #4ade80]✓[/]", "warn": "[bold #fbbf24]![/]", "fail": "[bold #fb7185]✗[/]"}
        lines = [f"[bold #38bdf8]Openfarm Doctor[/]  ({esc(rep['repo'])})\n"]
        for chk in rep["checks"]:
            ic = icons.get(chk["status"], "·")
            fx = " [#fbbf24](fixable)[/]" if chk.get("fixable") else ""
            lines.append(f"  {ic} [bold]{esc(chk['label']):<24}[/] {esc(chk['detail'])}{fx}")
        if rep["fixes_applied"]:
            lines.append("\n[bold #4ade80]Safe Fixes Applied:[/]")
            for item in rep["fixes_applied"]:
                lines.append(f"  + {esc(item)}")
        self.query_one("#engine-doctor", Static).update("\n".join(lines))
        if fix:
            self.refresh_all_views()

    @on(DataTable.RowHighlighted, "#plan-table")
    def _plan_row_changed(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            cid = str(event.row_key.value)
            self._selected_plan_id = cid
            match = next((e for e in self._plan_cards if e["id"] == cid), None)
            if match:
                self._render_plan_detail(match)

    @on(DataTable.RowHighlighted, "#review-table")
    def _review_row_changed(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            cid = str(event.row_key.value)
            match = next((c for c in self._review_cards if c.id == cid), None)
            if match:
                self._render_review_card(match, self._safe_cfg())

    @on(DataTable.RowHighlighted, "#findings-table")
    def _finding_row_changed(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            fid = str(event.row_key.value)
            match = next((f for f in self._findings_items if f["id"] == fid), None)
            if match:
                self._render_finding_detail(match)

    @on(Input.Changed, "#findings-filter")
    def _findings_filter_changed(self, event: Input.Changed) -> None:
        self._findings_query = event.value.strip()
        cfg = self._safe_cfg()
        if cfg is not None:
            fres = list_findings(self.repo, cfg, state_filter="waiting", query=self._findings_query, deduplicate=True)
            self._findings_items = fres["findings"]
            self._findings_counts = fres["counts"]
            self._refresh_findings_widgets()

    # ── Live Non-Blocking Wave Run ───────────────────────────────────────────

    @on(Button.Pressed, "#btn-daemon-start")
    def _daemon_start_btn(self) -> None:
        try:
            res = start_daemon(self.repo, config_path=self.config_path)
            self.query_one("#activity-log", RichLog).write(
                f"[bold #38bdf8]● Detached daemon started (pid {res['pid']})[/]  log: {rich_escape(res['log_path'])}"
            )
            self.refresh_all_views()
            self.notify(f"Started detached daemon (pid {res['pid']})")
        except Refused as exc:
            self.notify(str(exc), severity="warning")

    @on(Button.Pressed, "#btn-daemon-stop")
    def _daemon_stop_btn(self) -> None:
        try:
            res = stop_daemon(self.repo, config_path=self.config_path)
            self.query_one("#activity-log", RichLog).write(
                f"[bold #fbbf24]■ Daemon stop requested[/]  (was_running={res['was_running']}, pid={res['pid']})"
            )
            self.refresh_all_views()
            self.notify("Daemon stop requested")
        except Refused as exc:
            self.notify(str(exc), severity="warning")

    # -- openfarm commands run in their own process ----------------------------------
    # The CLI is never called inside this process with stdout swapped out: two actions
    # at once swap each other's streams back in the wrong order, and a stray print then
    # lands in the terminal Textual is drawing on.

    def _cli_argv(self, *argv: str) -> list[str]:
        base = [sys.executable, "-m", "openfarm_core.cli", "--repo", str(self.repo)]
        if self.config_path is not None:
            base += ["--config", str(self.config_path)]
        return [*base, *argv]

    @staticmethod
    def _cli_env() -> dict:
        # The child must import this same openfarm, wherever the TUI was started from.
        src = str(Path(openfarm_core.__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def _cli(self, *argv: str) -> tuple[int, str]:
        """Run one openfarm command; return its exit code and everything it printed."""
        proc = subprocess.run(self._cli_argv(*argv), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=self._cli_env())
        return proc.returncode, "\n".join(t for t in (proc.stdout.strip(), proc.stderr.strip()) if t)

    @staticmethod
    def _last_line(out: str, fallback: str) -> str:
        return out.strip().splitlines()[-1] if out.strip() else fallback

    def _wave_active(self) -> bool:
        """A wave is running here, or the detached daemon is running one."""
        if self._running_wave:
            return True
        try:
            return bool(daemon_status(self.repo, config_path=self.config_path).get("running"))
        except Exception:
            return False

    def _run_refusal(self) -> str:
        """Why a wave can't start right now ('' when it can): the checks `openfarm run` makes first."""
        cfg = self._safe_cfg()
        if cfg is None:
            return "this folder isn't set up for openfarm yet"
        state = StateDir(cfg.state_dir)
        if state.kill_requested(cfg.kill_switch):
            return "the kill switch exists (Stop was pressed)"
        if state.is_paused():
            return "the queue is paused"
        if state.read_breaker().get("tripped"):
            return "the circuit breaker is tripped"
        return ""

    def _refresh_run_controls(self) -> None:
        """Show only the run buttons that can do something right now."""
        cfg = self._safe_cfg()
        daemon_up = False
        if cfg is not None:
            try:
                daemon_up = bool(daemon_status(self.repo, config_path=self.config_path).get("running"))
            except Exception:
                daemon_up = False
        running = self._running_wave or daemon_up
        held = cfg is not None and bool(self._run_refusal())
        for bid, shown in (
            ("#btn-pause", running), ("#btn-stop", running), ("#btn-hint", running),
            ("#btn-resume", held),
            ("#btn-daemon-start", cfg is not None and not running),
            ("#btn-daemon-stop", daemon_up),
        ):
            self.query_one(bid, Button).set_class(not shown, "hidden")

    def _back_to_plan(self) -> None:
        has_cards = bool(self._plan_cards or self._review_cards)
        self.query_one("#studio-switcher", ContentSwitcher).current = "studio-plan" if has_cards else "studio-grill"

    @on(Button.Pressed, "#btn-run-back")
    def _run_back_clicked(self) -> None:
        self._back_to_plan()

    def _show_run_refusal(self, reason: str) -> None:
        self._append_activity(f"[bold #fb7185]✗ The wave didn't start:[/] {rich_escape(reason)}")
        low = reason.lower()
        if "paused" in low or "kill switch" in low or "breaker" in low:
            self._append_activity("[#fbbf24]Press Resume below to clear it, then launch the wave from the plan.[/]")
        elif "key" in low or "endpoint" in low or "api.example.invalid" in low:
            self._append_activity("[#fbbf24]Open Providers (Ctrl+K, then Providers) to set the worker's endpoint and API key.[/]")
        self.notify(f"The wave didn't start: {reason[:100]}", severity="error")
        self._refresh_run_controls()

    @on(Button.Pressed, "#btn-launch-wave")
    def start_nonblocking_wave(self) -> None:
        self.query_one("#studio-switcher", ContentSwitcher).current = "studio-run"
        if self._running_wave:
            self.notify("A wave is already running")
            return
        if not self._drafting_plan:
            self.query_one("#architect-panel", Vertical).add_class("hidden")
        refusal = self._run_refusal()
        if refusal:
            self._show_run_refusal(refusal)
            return
        self._card_phases.clear()
        self._cards_finished.clear()
        self._run_start_note = ""
        self.query_one("#run-queue-view", Static).update("Starting...")
        self._running_wave = True
        self._run_wave_bg()

    def _forward_run_events(self, events_path: Path, seen: int) -> int:
        """Hand the run's new events to the screen; returns how far the file has been read."""
        try:
            with events_path.open("rb") as handle:
                handle.seek(seen)
                data = handle.read()
        except OSError:
            return seen
        whole = data[: data.rfind(b"\n") + 1]
        for line in whole.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                self.call_from_thread(self._handle_run_event, event)
        return seen + len(whole)

    @work(thread=True)
    def _run_wave_bg(self) -> None:
        self._running_wave = True
        self.call_from_thread(self._set_running_badge, True)
        self.call_from_thread(self._append_activity, "[#94a3b8]Committing the baseline and starting the wave...[/]")
        try:
            ensure_baseline_committed(self.repo)
            cfg = self._safe_cfg()
            run_id = "run-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            events_path = StateDir(cfg.state_dir).run_dir(run_id) / "events.jsonl"
            # Output goes to files, not pipes: nothing reads a pipe while the run is going.
            with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
                proc = subprocess.Popen(
                    self._cli_argv("--json", "run", "--cards", "3", "--run-id", run_id),
                    stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=self._cli_env())
                seen = 0
                while True:
                    finished = proc.poll() is not None
                    seen = self._forward_run_events(events_path, seen)
                    if finished:
                        break
                    time.sleep(0.25)
                out.seek(0)
                err.seek(0)
                raw_out, raw_err = out.read().strip(), err.read().strip()
            rc = proc.returncode
            parsed: dict = {}
            try:
                obj = json.loads(raw_out) if raw_out else {}
                if isinstance(obj, dict):
                    parsed = obj
            except ValueError:
                parsed = {}
            if rc != 0 or parsed.get("refused") or parsed.get("error"):
                reason = str(parsed.get("reason") or parsed.get("error")
                             or self._last_line(raw_err or raw_out, f"the run ended with exit code {rc}"))
                self.call_from_thread(self._show_run_refusal, reason)
            else:
                outcomes = parsed.get("outcomes") if isinstance(parsed.get("outcomes"), list) else []
                if not outcomes:
                    why = f" {self._run_start_note}" if self._run_start_note else ""
                    self.call_from_thread(
                        self._append_activity,
                        f"[#fbbf24]○ The wave ended without starting a card.{rich_escape(why)}[/]",
                    )
                else:
                    ends = ", ".join(f"{o.get('card_id') or o.get('card')}: {o.get('final_state')}" for o in outcomes)
                    self.call_from_thread(
                        self._append_activity,
                        f"[bold #4ade80]✓ Wave finished:[/] {len(outcomes)} card(s) ({rich_escape(ends)}).",
                    )
        except Exception as exc:
            self.call_from_thread(self._append_activity, f"[bold #fb7185]✗ Run error:[/] {rich_escape(str(exc))}")
            self.call_from_thread(self.notify, f"Run error: {str(exc)[:100]}", severity="error")
        finally:
            self._running_wave = False
            self.call_from_thread(self._set_running_badge, False)
            self.call_from_thread(self.refresh_all_views)

    def _set_running_badge(self, active: bool) -> None:
        pill = self.query_one("#status-badge", Static)
        pill.remove_class("status-running", "status-ready")
        if active:
            pill.add_class("status-running")
            pill.update("● RUNNING")
        else:
            pill.add_class("status-ready")
            pill.update("○ READY")
        self._refresh_run_controls()

    def _append_activity(self, markup_line: str) -> None:
        self.query_one("#activity-log", RichLog).write(markup_line)

    def _handle_run_event(self, ev: dict) -> None:
        kind = str(ev.get("kind") or "")
        cid = str(ev.get("card_id") or "")
        phase = str(ev.get("phase") or "")
        msg = str(ev.get("message") or "")
        ts = str(ev.get("ts") or "")[11:19]
        if ev.get("worker_tokens") or ev.get("supervisor_tokens"):
            self._worker_tokens = max(self._worker_tokens, int(ev.get("worker_tokens") or 0))
            self._supervisor_tokens = max(self._supervisor_tokens, int(ev.get("supervisor_tokens") or 0))
            self._tokens_fresh = max(self._tokens_fresh, int(ev.get("tokens_in_fresh") or 0))
            self._tokens_cached = max(self._tokens_cached, int(ev.get("tokens_in_cached") or 0))
            self._spark_points.append(float(self._worker_tokens + self._supervisor_tokens))
            self._update_token_widgets()
        same_phase = bool(cid) and self._card_phases.get(cid) == phase
        if kind == "card_done" and cid:
            self._cards_finished.add(cid)
            self._card_phases[cid] = str(ev.get("final_state") or "finished")
        elif cid and phase and cid not in self._cards_finished:
            self._card_phases[cid] = phase
        if cid:
            q_lines = [
                (f"[bold #4ade80]●[/] {esc(k)} [#cbd5e1]finished: {esc(v)}[/]" if k in self._cards_finished
                 else f"[bold #38bdf8]▶ {esc(k)}[/] [#cbd5e1]{esc(v)}[/]")
                for k, v in self._card_phases.items()
            ]
            self.query_one("#run-queue-view", Static).update("\n".join(q_lines))
        if kind == "phase":
            if not same_phase:  # the same phase is announced more than once by the run
                self._append_activity(f"[#94a3b8]{ts}[/] [bold #38bdf8]▶[/] {rich_escape(cid)} → [bold]{rich_escape(phase)}[/]")
        elif kind == "card_done":
            st = str(ev.get("final_state") or "")
            col = "#4ade80" if st == "in-review" else "#fb7185"
            self._append_activity(f"[#94a3b8]{ts}[/] [bold {col}]●[/] {rich_escape(cid)} finished: {rich_escape(st)} {rich_escape(msg)}")
            self.refresh_all_views()
        elif msg:
            if kind == "run_start":
                self._run_start_note = msg.partition("; ")[2]
                if not self._card_phases:
                    self.query_one("#run-queue-view", Static).update(
                        esc("No card running." + (f" {self._run_start_note}" if self._run_start_note else "")))
            self._append_activity(f"[#94a3b8]{ts}[/] {rich_escape(msg)}")

    # ── Review & Findings Buttons + Modals ───────────────────────────────────

    @on(Button.Pressed, "#btn-find-pick")
    def _pick_btn(self) -> None:
        if self._selected_finding_id:
            self.apply_finding_decision(self._selected_finding_id, "next-wave")

    @on(Button.Pressed, "#btn-find-dismiss")
    def _open_dismiss_modal(self) -> None:
        item = next((f for f in self._findings_items if f["id"] == self._selected_finding_id), None)
        if item is None:
            return

        def _done(reason: str | None) -> None:
            if reason is not None:
                self.apply_finding_decision(item["id"], "dismissed", reason=reason)

        self.push_screen(DismissFindingModal(item["id"], item["title"]), _done)

    @on(Button.Pressed, "#btn-find-fold")
    def _open_fold_modal(self) -> None:
        item = next((f for f in self._findings_items if f["id"] == self._selected_finding_id), None)
        if item is None:
            return
        choices = [(c.id, c.title) for c in self._review_cards if not is_finding_id_or_header(c.id)]
        if not choices:
            self.notify("No existing non-finding cards to fold into", severity="warning")
            return

        def _done(target: str | None) -> None:
            if target:
                self.apply_finding_decision(item["id"], "folded", target_card=target)

        self.push_screen(FoldFindingModal(item["id"], item["title"], choices), _done)

    @on(Button.Pressed, "#btn-find-full")
    def _open_full_finding_modal(self) -> None:
        item = next((f for f in self._findings_items if f["id"] == self._selected_finding_id), None)
        if item is not None:
            self.push_screen(FullFindingModal(item))

    @on(Button.Pressed, "#btn-approve")
    def _approve_selected_card(self) -> None:
        cid = self._selected_review_id
        if not cid:
            return
        rc, out = self._cli("verdict", cid, "clean")
        if rc != 0:
            self.notify(f"Approve failed: {self._last_line(out, f'verdict exited {rc}')[:100]}", severity="error")
            return
        if cid not in self._approved_to_land:
            self._approved_to_land.append(cid)
        self.query_one("#approved-queue-label", Static).update(
            esc(f"Approved to land: {', '.join(self._approved_to_land)}")
        )
        self.notify(f"Approved {cid}. Press [L] or Land Locally to merge it.")
        self.refresh_all_views()

    # -- slow review actions: say it's running, run it off the UI thread, show how it ended --

    def _start_review_action(self, button_id: str, doing: str, argv: list[str], on_done=None) -> None:
        if button_id in self._busy_labels:
            self.notify(f"{doing} is still running")
            return
        btn = self.query_one(button_id, Button)
        self._busy_labels[button_id] = str(btn.label)
        btn.loading = True
        self.query_one("#review-status", Static).update(f"[bold #38bdf8]⏳ {esc(doing)}...[/]")
        self._review_action_bg(button_id, doing, argv, on_done)

    @work(thread=True)
    def _review_action_bg(self, button_id: str, doing: str, argv: list[str], on_done) -> None:
        try:
            rc, out = self._cli(*argv)
        except Exception as exc:  # noqa: BLE001 - the button must come back whatever happened
            rc, out = 1, str(exc)
        self.call_from_thread(self._finish_review_action, button_id, doing, rc, out, on_done)

    def _finish_review_action(self, button_id: str, doing: str, rc: int, out: str, on_done) -> None:
        self._busy_labels.pop(button_id, None)
        self.query_one(button_id, Button).loading = False
        last = self._last_line(out, f"exit code {rc}")
        colour = "#4ade80" if rc == 0 else "#fb7185"
        self.query_one("#review-status", Static).update(
            f"[bold {colour}]{'✓' if rc == 0 else '✗'} {esc(doing)}:[/] {esc(last[:200])}")
        self.notify(f"{doing}: {last[:120]}", severity=("error" if rc != 0 else "information"))
        if on_done is not None:
            on_done(rc, out)
        self.refresh_all_views()

    @on(Button.Pressed, "#btn-promote-int")
    def _promote_integration_btn(self) -> None:
        self._start_review_action("#btn-promote-int", "Merging the integration branch", ["land", "--promote-integration"])

    @on(Button.Pressed, "#btn-land")
    def _land_approved_queue(self) -> None:
        targets = list(self._approved_to_land) or ([self._selected_review_id] if self._selected_review_id else [])
        if not targets:
            self.notify("No cards queued to land")
            return

        def _landed(rc: int, _out: str) -> None:
            if rc == 0:
                self._approved_to_land.clear()
                self.query_one("#approved-queue-label", Static).update("Approved queue: (none)")

        self._start_review_action("#btn-land", f"Landing {', '.join(targets)}", ["land", *targets], _landed)

    @on(Button.Pressed, "#btn-accept-blocked")
    def _open_accept_blocked_modal(self) -> None:
        card = next((c for c in self._review_cards if c.id == self._selected_review_id), None)
        if card is None or str(card.state).lower() != "blocked":
            return

        def _done(note: str | None) -> None:
            if note is None:
                return
            if not note.strip():
                self.notify("A note saying what you checked is required.", severity="warning")
                return
            self._start_review_action(
                "#btn-accept-blocked", f"Accepting {card.id}",
                ["land", card.id, "--accept-blocked", "--note", note.strip()])

        self.push_screen(BounceNoteModal(
            card.id, card.title,
            heading=f"I've checked {card.id}, accept it",
            sub="The farm blocked this card. Say what you checked; your name and this note are recorded, then it merges like Land.",
            ok_label="Accept and Land"), _done)

    @on(Button.Pressed, "#btn-bounce")
    def _open_bounce_modal(self) -> None:
        card = next((c for c in self._review_cards if c.id == self._selected_review_id), None)
        if card is None:
            return

        def _done(note: str | None) -> None:
            if note is not None:
                self._run_cli_quick(["verdict", card.id, "bounced", *(["--note", note] if note else [])])
                self.refresh_all_views()

        self.push_screen(BounceNoteModal(card.id, card.title), _done)

    @on(Button.Pressed, "#btn-freview")
    def _run_frontier_review_bg(self) -> None:
        cid = self._selected_review_id
        if not cid:
            # One card at a time: without a choice nothing is reviewed, least of all the whole diff.
            self.notify("Which card? Select one in the list, then press Frontier Review.", severity="warning")
            self.query_one("#review-table", DataTable).focus()
            return
        cfg = self._safe_cfg()

        def _reviewed(rc: int, out: str) -> None:
            text = out or "(the review printed nothing)"
            if rc == 0 and cfg is not None:
                path = self._frontier_review_path(cfg, cid)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text + "\n", encoding="utf-8")
            if self._selected_review_id == cid:
                self.query_one("#review-frontier-out", Static).update(esc(text))
            self.query_one("#review-tabs", TabbedContent).active = "rev-tab-frontier"

        self._start_review_action(
            "#btn-freview", f"Reviewing {cid} with Claude and Codex",
            ["frontier-review", "--scope", "card", cid], _reviewed)

    @on(Button.Pressed, "#btn-pause")
    def _pause_btn(self) -> None:
        self._run_cli_quick(["pause"])
        self._refresh_run_controls()

    @on(Button.Pressed, "#btn-resume")
    def _resume_btn(self) -> None:
        if self._run_cli_quick(["resume", "--clear-breaker", "--clear-kill"]) == 0:
            self._append_activity("[bold #4ade80]✓ Resumed: pause, breaker and kill switch are cleared.[/]")
        self._refresh_run_controls()

    @on(Button.Pressed, "#btn-stop")
    def _stop_btn(self) -> None:
        self._run_cli_quick(["stop"])
        self._refresh_run_controls()

    @on(Button.Pressed, "#btn-hint")
    def _open_hint_modal(self) -> None:
        active_cid = next(iter(self._card_phases.keys()), "")

        def _done(text: str | None) -> None:
            if text:
                self._run_cli_quick(["swarm", "hint", active_cid or "all", text])

        self.push_screen(HintModal(active_cid), _done)

    def _run_cli_quick(self, argv: list[str]) -> int:
        """A quick openfarm command (pause, release, verdict...): run it and toast its last line."""
        rc, out = self._cli(*argv)
        self.notify(self._last_line(out, f"{argv[0]} exited {rc}")[:120],
                    severity=("error" if rc != 0 else "information"))
        return rc
