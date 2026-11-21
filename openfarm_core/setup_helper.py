"""`openfarm setup`: the few things openfarm needs before it can build, asked in order.

Who plans and reviews (and whether that sign-in is ready), where the builders'
requests go and with which key, which models that address offers, and which model
and effort each job uses. Every answer is also a flag, so the same helper runs
unattended (`--yes` or `--json` asks nothing).
"""

from __future__ import annotations

from pathlib import Path

from . import model_choices as mc
from .errors import Refused
from .providers import (
    WORKER_PRESETS,
    load_global_providers,
    probe_worker_endpoint,
    save_global_providers,
)

FRONTIERS = ("claude", "codex")  # Antigravity only runs when a person starts it, so it is not offered
#: Names the provider's own CLI takes for `--model`. Any other name can be typed.
FRONTIER_MODELS = {"claude": ("opus", "sonnet", "haiku"), "codex": ()}


def catalogue(*, timeout_s: float = 8.0) -> dict:
    """What can be chosen for each job right now: the builders' models as their address lists them, and efforts."""
    gp = load_global_providers()
    worker = gp["worker"]
    probe = probe_worker_endpoint(worker["endpoint"], key_file=Path(worker["key_file"]) if worker.get("key_file") else None,
                                  model=worker["model"], zdr=bool(worker.get("zdr", True)), timeout_s=timeout_s)
    frontier = gp["frontier"]["provider"]
    frontier_models = list(FRONTIER_MODELS.get(frontier, ()))
    return {
        "frontier": frontier,
        "endpoint": worker["endpoint"],
        "architect": {"models": frontier_models, "efforts": list(mc.FRONTIER_EFFORTS)},
        "reviewer": {"models": frontier_models, "efforts": list(mc.FRONTIER_EFFORTS)},
        "builder": {"models": list(probe.get("models") or []), "efforts": list(mc.BUILDER_EFFORTS),
                    "ok": bool(probe["ok"]), "error": str(probe.get("error") or "")},
    }


def _pick(ask, out, title: str, options: list[str], default: str, *, free: bool = False) -> str:
    """One question. Enter keeps `default`; a number picks an option; with `free`, anything else is taken as typed."""
    while True:
        if options:
            out(title)
            for number, option in enumerate(options, 1):
                out(f"  {number}. {option}{'   (now)' if option == default else ''}")
            said = ask(f"  Number{', or type a name' if free else ''} [{default or 'not chosen'}]: ").strip()
        else:
            said = ask(f"{title} [{default or 'none'}]: ").strip()
        if not said:
            return default
        if said.isdigit() and 1 <= int(said) <= len(options):
            return options[int(said) - 1]
        if said in options or free:
            return said
        out(f"  {said!r} is not one of the choices.")


def _pick_model(ask, out, models: list[str], default: str) -> str:
    """Choose from a long list: a word narrows it, a number or a full name picks."""
    shown = models
    while True:
        if len(shown) > 25:
            out(f"  {len(shown)} models. Type part of a name to narrow the list, or a full name.")
            said = ask(f"  Builders' model [{default or 'none'}]: ").strip()
        else:
            return _pick(ask, out, "  Builders' model:", shown, default, free=True)
        if not said:
            return default
        if said in models:
            return said
        narrowed = [m for m in models if said.lower() in m.lower()]
        if not narrowed:
            return said  # not in the list: taken as typed, the address may still serve it
        shown = narrowed


def run_setup(args, *, ask=input, secret=None, out=print, signed_in=None, sign_in=None) -> dict:
    """Walk the steps and save. Returns what is now set. `ask`, `secret`, `out` are the terminal by default."""
    import getpass

    secret = secret or getpass.getpass
    quiet = bool(getattr(args, "yes", False) or getattr(args, "json", False))
    if quiet:
        out = lambda *_a, **_k: None  # noqa: E731
    flag = lambda name: getattr(args, name, None)  # noqa: E731
    current = load_global_providers()
    worker = current["worker"]

    # 1. who plans and reviews
    frontier = flag("frontier") or current["frontier"]["provider"]
    if frontier not in FRONTIERS:
        frontier = FRONTIERS[0]
    if not quiet and not flag("frontier"):
        out("\n1. Who plans and reviews? This is a sign-in you already have, not an API key.")
        frontier = _pick(ask, out, "", list(FRONTIERS), frontier)
    ready = bool(signed_in(frontier)) if signed_in else None
    if ready is False and not quiet and sign_in is not None:
        out(f"  {frontier.capitalize()} is not signed in.")
        if ask("  Sign in now? [Y/n]: ").strip().lower() in ("", "y", "yes"):
            sign_in(frontier)
            ready = bool(signed_in(frontier))
    if ready is not None:
        out(f"  {frontier.capitalize()} is {'signed in' if ready else 'NOT signed in: run `openfarm acp login --provider ' + frontier + '`'}.")

    # 2. who builds
    presets = {p["id"]: p for p in WORKER_PRESETS}
    given_preset = flag("preset")
    if given_preset is not None and given_preset not in presets:
        raise Refused(
            "unknown-preset",
            f"unknown preset {given_preset!r}; choose one of {', '.join(presets)}",
        )
    preset = given_preset or worker["preset"]
    if preset not in presets:
        preset = "custom" if "custom" in presets else WORKER_PRESETS[0]["id"]
    if not quiet and not given_preset and not flag("endpoint"):
        out("\n2. Who builds? Any address that speaks the OpenAI chat API.")
        labels = [f"{p['id']}: {p['label']} ({p['note']})" for p in WORKER_PRESETS]
        default_label = next((l for l in labels if l.startswith(preset + ":")), labels[0])
        picked = _pick(ask, out, "", labels, default_label)
        preset = picked.split(":", 1)[0]
    changed_preset = preset != worker["preset"]
    endpoint = flag("endpoint") or ("" if changed_preset else worker["endpoint"]) or presets[preset]["endpoint"]
    if flag("endpoint") and not given_preset:
        preset = next((p["id"] for p in WORKER_PRESETS if p["endpoint"] == endpoint), "custom")
    if not quiet and not flag("endpoint"):
        endpoint = _pick(ask, out, "  Address", [], endpoint, free=True)
    api_key = str(flag("api_key") or "")
    needs_key = bool(presets[preset].get("needs_key", True))
    if not quiet and not api_key and needs_key:
        have = worker["key_present"] and not changed_preset
        api_key = secret(f"  API key{' (Enter keeps the saved one)' if have else ''}: ").strip()
    zdr = False if flag("no_zdr") else (bool(presets[preset].get("zdr", True)) if changed_preset else bool(worker["zdr"]))
    if not quiet and not flag("no_zdr"):
        zdr = ask(f"  Ask the provider not to keep what is sent? [{'Y/n' if zdr else 'y/N'}]: ").strip().lower() \
            in (("", "y", "yes") if zdr else ("y", "yes"))
    for_project = bool(flag("project"))
    repo = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    target = mc.project_path(repo) if for_project else mc.global_path()
    chosen = mc.load_choices(target)
    existing_builder = (chosen.get("builder") or {}).get("model", "")
    builder_model = (
        flag("builder_model")
        or ("" if changed_preset else (existing_builder or worker["model"]))
        or presets[preset]["default_model"]
    )
    key_file = Path(flag("key_file")) if flag("key_file") else Path(worker["key_file"])
    saved = save_global_providers(frontier_provider=frontier, worker_preset=preset, endpoint=endpoint, model=builder_model,
                                  key_file=key_file, api_key=api_key, zdr=zdr)

    # 3. what the builders' address offers
    listed = catalogue(timeout_s=float(flag("timeout") or 8.0))
    models = listed["builder"]["models"]
    if listed["builder"]["ok"] and not models:
        out("\n3. The builders' address answered, but not with a list of models. The model is taken as typed.")
    elif listed["builder"]["ok"]:
        out(f"\n3. The builders' address answered and lists {len(models)} model(s).")
    else:
        out(f"\n3. The builders' address did not answer ({listed['builder']['error'] or 'no reason given'}). "
            "The model is taken as typed.")
    builder_picked = False
    if not quiet and not flag("builder_model"):
        builder_model = _pick_model(ask, out, models, builder_model) if models else \
            _pick(ask, out, "  Builders' model", [], builder_model, free=True)
        builder_picked = bool(builder_model)
    if builder_model != saved["worker"]["model"]:
        save_global_providers(frontier_provider=frontier, worker_preset=preset, endpoint=endpoint, model=builder_model,
                              key_file=key_file, zdr=zdr)

    # 4. models and efforts, job by job
    if not quiet:
        out(f"\n4. Model and effort for each job ({'this project' if for_project else 'every project'}). "
            "Enter keeps what is in brackets; an empty model means the provider's own default.")
    for role in mc.ROLES:
        had = chosen.get(role, {})
        model = flag(f"{role}_model")
        if role == "builder":
            model = builder_model if (model or builder_picked or for_project or had.get("model")) else ""
        elif model is None:
            model = had.get("model", "")
            if not quiet:
                model = _pick(ask, out, f"  {mc.ROLE_WORDS[role]}: model", list(listed[role]["models"]), model, free=True)
        effort = flag(f"{role}_effort")
        if effort is None:
            effort = had.get("effort", "")
            if not quiet:
                # Enter with nothing chosen leaves it unchosen, so each project's own setting still applies
                effort = _pick(ask, out, f"  {mc.ROLE_WORDS[role]}: effort", list(mc.efforts_for(role)), effort)
        mc.save_choice(target, role, model=model or "", effort=effort or "")

    result = {"frontier": frontier, "signed_in": ready, "preset": preset, "endpoint": endpoint, "zdr": zdr,
              "key_present": load_global_providers()["worker"]["key_present"], "builders_answer": listed["builder"]["ok"],
              "models_listed": len(models), "saved_for": "project" if for_project else "global",
              "choices": mc.load_choices(target), "providers_file": saved["path"], "models_file": str(target)}
    out("\nDone. " + ("Everything is ready: `openfarm` opens the app."
                      if ready is not False and listed["builder"]["ok"] and result["key_present"] | (not needs_key)
                      else "Something above still needs you before a build can run."))
    return result
