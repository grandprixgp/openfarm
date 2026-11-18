"""How a provider's child is held: Codex's no-tools flags and a bubblewrap boundary.

Claude's child is locked down by its own settings (isolated home, deny list,
disallowed tools, permission handling). Codex and Antigravity have nothing like
that the farm can rely on, so their children are held here instead:

- Codex with tool policy `none` gets the strict `--disable ...` flag set, checked
  live against codex-cli 0.166.0 (no file read, no web fetch);
- everything flags cannot cover runs inside a bubblewrap (`bwrap`) boundary. The
  boundary is an allow-list: the system folders a program needs, the program's
  own install tree, the session folder read-only with `never_touch`,
  `forbidden_files` and every `.git` hidden, the isolated home read-write and a private `/tmp`.
  Nothing else of the file system is there: not the real home, not the state
  folder, not the worker key file, not other cards' worktrees. The network stays.

`[micromanager] confine` picks the rule: `auto` (the default), `bwrap` (every
provider's child confined, Claude's too) or `off` (nothing confined, and the run
says so). Everything here is a plain function, so the ACP adapter, the bridges
and the driver all build the same argv.
"""

from __future__ import annotations

import dataclasses
import os
import pwd
import shutil
import stat
import subprocess
import threading
from pathlib import Path

from .globs import matches_any, normalise

CONFINE_MODES: tuple[str, ...] = ("auto", "bwrap", "off")
CONFINE_DEFAULT = "auto"
CONFINE_SETTING = '[micromanager] confine'

# The strict set: with these, codex-cli 0.166.0 ran neither a file read nor a web
# fetch and still answered (lead's live check, 17 Nov). `-s read-only` stays too.
_CODEX_STRICT_DISABLE: tuple[str, ...] = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "multi_agent_v2",
    "goals",
    "sleep_tool",
    "code_mode_host",
    "tool_suggest",
    "skill_search",
    "hooks",
)
# Read-only keeps the shell (Codex reads files through it) and turns off the rest.
# The shell needs the code-mode host: with it off, Codex 0.166 answered every read
# with "code-mode host is disabled" (lead's live check, 17 Nov).
_CODEX_READ_ONLY_KEEP = frozenset({"shell_tool", "unified_exec", "code_mode_host"})

# Folders every program needs, mounted read-only (a symlink is copied as one).
_SYSTEM_DIRS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# The few /etc entries for DNS, TLS certificates, users, time and the loader.
_ETC_ENTRIES: tuple[str, ...] = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/host.conf",
    "/etc/nsswitch.conf",
    "/etc/gai.conf",
    "/etc/services",
    "/etc/protocols",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/pki",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/alternatives",
)
_SYSTEM_PREFIXES: tuple[str, ...] = _SYSTEM_DIRS + ("/etc",)
_INTERPRETERS = frozenset({
    "python", "python3", "node", "nodejs", "bun", "deno", "ruby", "perl", "sh", "bash", "dash",
})
# A session folder with more entries than this is refused rather than half-masked.
MAX_SCAN_ENTRIES = 1_000_000
_MAX_SYMLINK_HOPS = 40


class ConfineError(Exception):
    """The boundary cannot be built for this child; starting it is refused."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ConfineSettings:
    """What the config says about confinement, plus paths to hide wherever they are."""

    mode: str = CONFINE_DEFAULT
    read_roots: tuple[str, ...] = ()
    hide_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in CONFINE_MODES:
            raise ConfineError(
                "confine-bad-mode",
                f"{CONFINE_SETTING} must be one of {', '.join(CONFINE_MODES)}, got {self.mode!r}",
            )

    def as_dict(self) -> dict:
        return {"mode": self.mode, "read_roots": list(self.read_roots),
                "hide_paths": list(self.hide_paths)}


def settings_from_config(cfg) -> ConfineSettings:
    """`ConfineSettings` from a loaded config (`[micromanager] confine`, `confine_read_roots`)."""
    mm = getattr(cfg, "micromanager", None)
    mode = str(getattr(mm, "confine", CONFINE_DEFAULT) or CONFINE_DEFAULT)
    roots = tuple(str(r) for r in (getattr(mm, "confine_read_roots", None) or ()))
    hide: list[str] = []
    key_file = getattr(cfg, "key_file", None)
    if key_file:
        hide.append(str(key_file))
    return ConfineSettings(mode=mode, read_roots=roots, hide_paths=tuple(hide))


def as_settings(value) -> ConfineSettings:
    """Accept a `ConfineSettings`, a mode string, a dict or None (the default)."""
    if value is None:
        return ConfineSettings()
    if isinstance(value, ConfineSettings):
        return value
    if isinstance(value, str):
        return ConfineSettings(mode=value)
    if isinstance(value, dict):
        return ConfineSettings(
            mode=str(value.get("mode") or CONFINE_DEFAULT),
            read_roots=tuple(str(r) for r in (value.get("read_roots") or ())),
            hide_paths=tuple(str(p) for p in (value.get("hide_paths") or ())),
        )
    raise ConfineError("confine-bad-mode", f"cannot read confinement settings from {value!r}")


# -- Codex flags --------------------------------------------------------------


def codex_tool_flags(tool_policy: str) -> list[str]:
    """The `codex exec` flags for a tool policy: `none` is the live-checked strict set."""
    if tool_policy not in ("none", "read-only"):
        raise ConfineError(
            "confine-bad-tool-policy",
            f"tool policy must be 'none' or 'read-only', got {tool_policy!r}",
        )
    out: list[str] = []
    for feature in _CODEX_STRICT_DISABLE:
        if tool_policy == "read-only" and feature in _CODEX_READ_ONLY_KEEP:
            continue
        out.extend(["--disable", feature])
    out.extend(["-c", 'web_search="disabled"'])
    return out


# -- deciding how a child is held -----------------------------------------------


@dataclasses.dataclass(frozen=True)
class Hold:
    """How one child is held, or why it may not start."""

    provider: str
    mode: str
    flags: bool = False
    boundary: bool = False
    refused: str = ""
    note: str = ""

    def describe(self) -> str:
        if self.refused:
            return "refused"
        parts = []
        if self.flags:
            parts.append("flags")
        if self.boundary:
            parts.append("boundary")
        return " + ".join(parts) if parts else "nothing"

    def as_dict(self) -> dict:
        return {"mode": self.mode, "held_by": self.describe(), "flags": self.flags,
                "boundary": self.boundary, "refused": self.refused, "note": self.note}


def off_note(provider: str) -> str:
    return (
        f'{CONFINE_SETTING} = "off": the {provider} child runs with no OS boundary and can '
        "read whatever your user can read; only its own tool settings limit it"
    )


def codex_read_only_refusal(reason: str = "") -> str:
    return (
        "Codex with tool policy 'read-only' reads files through its shell, which it runs in "
        "a sandbox of its own, and that sandbox cannot start inside openfarm's boundary here "
        f"({reason or 'nested sandboxes are refused'}), so Codex could read nothing. Use "
        'tool_policy = "none", or the Claude provider for a seat that reads files, or set '
        f'{CONFINE_SETTING} = "off" to run Codex unconfined (it can then read whatever your '
        "user can)."
    )


def needs_nested(provider: str, transport: str, tool_policy: str, mode: str) -> bool:
    """Whether `decide` needs `nested_status` for this child."""
    # A standalone Codex ACP server runs the same sandboxed shell (v0.8.5).
    return (provider == "codex" and transport in ("bridge", "server")
            and tool_policy == "read-only" and mode != "off")


def flags_only_note(provider: str, bwrap_reason: str = "") -> str:
    """The note for a child held by its tool flags alone (no boundary could be built)."""
    why = f" ({bwrap_reason})" if bwrap_reason else ""
    return (
        f"the {provider} child is held by its tool flags alone: bubblewrap is not usable "
        f"here{why}, so there is no OS boundary and a tool the flags miss could read "
        "whatever your user can read"
    )


def hold_note(hold: Hold, bwrap_reason: str = "") -> str:
    """What a run's notes say about a child that is not confined, or '' when it is.

    `off` gives `off_note`; a child held by flags alone (Codex when bwrap is
    unusable) gives `flags_only_note`. Claude under `auto` follows its own rules
    and a refused child never starts, so neither gives a note.
    """
    if hold.refused:
        return ""
    if hold.mode == "off":
        return hold.note or off_note(hold.provider)
    if hold.flags and not hold.boundary:
        return flags_only_note(hold.provider, bwrap_reason)
    return ""


def hold_for(provider: str, settings=None, *, transport: str,
             tool_policy: str = "none") -> Hold:
    """`decide` for a child under `settings`, asking bwrap only when the rule needs it.

    The hold carries its note (`hold_note`) when the child is not confined. This
    is the adapter's own rule (`ClaudeAcpAdapter._decide_hold`), for callers that
    hold no adapter: the driver, and the run's notes and shared supervisor.
    """
    held = as_settings(settings)
    mode = held.mode
    needs_check = not (mode == "off" or (mode == "auto" and provider == "claude"))
    usable, reason = bwrap_status() if needs_check else (False, "")
    nested, nested_why = (True, "")
    if usable and needs_nested(provider, transport, tool_policy, mode):
        nested, nested_why = nested_status()
    hold = decide(provider, transport=transport, tool_policy=tool_policy, mode=mode,
                  bwrap_usable=usable, bwrap_reason=reason,
                  nested_usable=nested, nested_reason=nested_why)
    if not hold.note:
        note = hold_note(hold, reason)
        if note:
            hold = dataclasses.replace(hold, note=note)
    return hold


def decide(
    provider: str,
    *,
    transport: str,
    tool_policy: str,
    mode: str,
    bwrap_usable: bool,
    bwrap_reason: str = "",
    nested_usable: bool = True,
    nested_reason: str = "",
) -> Hold:
    """Pick flags and boundary for a child.

    `transport` is `bridge` (openfarm's own bridge starts one CLI child per
    prompt) or `server` (a standalone ACP server holds every session).

    `nested_usable` says whether a sandbox can start inside the boundary
    (`nested_status`). Codex runs its shell in a sandbox of its own, so its
    `read-only` policy inside the boundary needs that; without it Codex can
    read nothing and the session is refused rather than started blind.
    """
    if mode not in CONFINE_MODES:
        raise ConfineError(
            "confine-bad-mode",
            f"{CONFINE_SETTING} must be one of {', '.join(CONFINE_MODES)}, got {mode!r}",
        )
    flags = provider == "codex" and transport == "bridge"
    if mode == "off":
        return Hold(provider, mode, flags=flags, note=off_note(provider))
    if mode == "auto" and provider == "claude":
        return Hold(provider, mode)
    if mode == "auto" and flags and tool_policy == "none":
        return Hold(provider, mode, flags=True, boundary=bwrap_usable)
    if bwrap_usable:
        if flags and tool_policy == "read-only" and not nested_usable:
            return Hold(provider, mode, flags=True, refused=codex_read_only_refusal(nested_reason))
        if (provider == "codex" and transport == "server" and tool_policy == "read-only"
                and not nested_usable):
            # v0.8.5: refused up front as the bridge is, not started to read nothing.
            return Hold(provider, mode, refused=codex_read_only_refusal(nested_reason))
        return Hold(provider, mode, flags=flags, boundary=True)
    if mode == "bwrap":
        why = f'{CONFINE_SETTING} = "bwrap" confines every provider'
    elif provider == "codex" and transport == "bridge":
        why = f"Codex with tool policy {tool_policy!r} needs the boundary"
    elif provider == "codex":
        why = "a standalone Codex ACP server needs the boundary"
    else:
        why = "every Antigravity child needs the boundary"
    return Hold(
        provider, mode, flags=flags,
        refused=(
            f"{why}, but bubblewrap is not usable here ({bwrap_reason or 'unknown reason'}). "
            f'Install bubblewrap (bwrap), or set {CONFINE_SETTING} = "off" to run it '
            "unconfined (it can then read whatever your user can)."
        ),
    )


# -- bubblewrap -----------------------------------------------------------------

_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: dict[str, tuple[bool, str]] = {}


def find_bwrap(env: dict | None = None) -> str:
    path = (env or os.environ).get("PATH") if env is not None else None
    return shutil.which("bwrap", path=path) or ""


def bwrap_status(bwrap: str | None = None, *, refresh: bool = False) -> tuple[bool, str]:
    """Whether bwrap is installed and can build a boundary here (cached per path)."""
    exe = bwrap if bwrap is not None else find_bwrap()
    if not exe:
        return False, "bwrap is not installed (not found on PATH)"
    with _STATUS_LOCK:
        if not refresh and exe in _STATUS_CACHE:
            return _STATUS_CACHE[exe]
    true_bin = next((p for p in ("/usr/bin/true", "/bin/true") if os.path.exists(p)), "")
    if not true_bin:
        result = (False, "no /usr/bin/true to test bwrap with")
    else:
        argv = [exe, *_base_args(), "--tmpfs", "/tmp", "--", true_bin]
        try:
            proc = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=15,
                                  stdin=subprocess.DEVNULL)
            if proc.returncode == 0:
                result = (True, f"{exe} works")
            else:
                first = (proc.stderr or proc.stdout or "").strip().splitlines()
                detail = first[0][:200] if first else f"exit {proc.returncode}"
                result = (False, f"bwrap cannot build a boundary here: {detail}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = (False, f"bwrap could not run: {exc}")
    with _STATUS_LOCK:
        _STATUS_CACHE[exe] = result
    return result


def nested_status(bwrap: str | None = None, *, refresh: bool = False) -> tuple[bool, str]:
    """Whether a second sandbox can start inside the boundary (cached per path).

    Codex wraps each shell command in a bubblewrap sandbox of its own. Where the
    system forbids a sandboxed program from making new namespaces (Ubuntu's
    AppArmor rule for unprivileged user namespaces does), that inner sandbox
    fails and every command with it. Untested on a machine that allows it.
    """
    exe = bwrap if bwrap is not None else find_bwrap()
    usable, why = bwrap_status(exe, refresh=refresh)
    if not usable:
        return False, why
    key = f"nested:{exe}"
    with _STATUS_LOCK:
        if not refresh and key in _STATUS_CACHE:
            return _STATUS_CACHE[key]
    true_bin = next((p for p in ("/usr/bin/true", "/bin/true") if os.path.exists(p)), "")
    inner = [exe, "--unshare-user", "--unshare-pid", "--ro-bind", "/", "/",
             "--proc", "/proc", "--dev", "/dev", "--", true_bin]
    argv = [exe, *_base_args(), "--tmpfs", "/tmp", "--", *inner]
    try:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL)
        if proc.returncode == 0:
            result = (True, "a sandbox can start inside the boundary")
        else:
            first = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = first[0][:200] if first else f"exit {proc.returncode}"
            result = (False, f"a sandbox cannot start inside the boundary: {detail}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = (False, f"could not test a sandbox inside the boundary: {exc}")
    with _STATUS_LOCK:
        _STATUS_CACHE[key] = result
    return result


def _reset_status_cache() -> None:
    with _STATUS_LOCK:
        _STATUS_CACHE.clear()


def _base_args() -> list[str]:
    args = ["--die-with-parent", "--unshare-all", "--share-net", "--new-session"]
    for d in _SYSTEM_DIRS:
        if os.path.islink(d):
            args.extend(["--symlink", os.readlink(d), d])
        elif os.path.isdir(d):
            args.extend(["--ro-bind", d, d])
    for entry in _ETC_ENTRIES:
        if os.path.exists(entry):
            args.extend(["--ro-bind", entry, entry])
    args.extend(["--proc", "/proc", "--dev", "/dev"])
    return args


def _real_home() -> Path | None:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError):
        return None


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _under_system(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _SYSTEM_PREFIXES)


def _install_root(real: Path, guards: list[Path]) -> Path:
    """The folder to mount for a program file.

    A program under `node_modules` gets the outermost `node_modules` folder it
    sits in: npm puts a package's dependencies beside it there, not inside it
    (the Codex ACP server could not find its own platform package otherwise,
    seen live). Otherwise its version folder, or the file itself.
    """
    parts = real.parts
    candidates: list[Path] = []
    if "node_modules" in parts[:-1]:
        first = parts.index("node_modules")
        candidates.append(Path(*parts[:first + 1]))
        idx = len(parts) - 1 - list(reversed(parts)).index("node_modules")
        if idx + 1 < len(parts) - 1:
            end = idx + 2
            if parts[idx + 1].startswith("@") and idx + 2 < len(parts) - 1:
                end = idx + 3
            candidates.append(Path(*parts[:end]))
    elif real.parent.name not in ("bin", "sbin", ""):
        candidates.append(real.parent)
    # Never mount a folder that holds the real home, the session or anything hidden:
    # take the widest folder that holds none of them, else the file alone.
    for root in candidates:
        if not any(_within(guard, root) for guard in guards):
            return root
    return real


def program_mounts(
    exe: str,
    *,
    env_path: str | None = None,
    guards: list[Path] | None = None,
    _depth: int = 0,
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Read-only roots and symlinks a program needs: its install tree and its interpreter.

    Returns `(roots, symlinks)`; each symlink is `(link_target_text, link_path)`.
    """
    roots: list[Path] = []
    links: list[tuple[str, str]] = []
    if not exe or _depth > 3:
        return roots, links
    path = exe
    if "/" not in path:
        found = shutil.which(path, path=env_path)
        if not found:
            return roots, links
        path = found
    path = os.path.abspath(path)
    hops = 0
    while os.path.islink(path) and hops < _MAX_SYMLINK_HOPS:
        target = os.readlink(path)
        if not _under_system(path):
            links.append((target, path))
        path = os.path.normpath(os.path.join(os.path.dirname(path), target))
        hops += 1
    if not os.path.isfile(path):
        return roots, links
    real = Path(path)
    if not _under_system(str(real)):
        roots.append(_install_root(real, list(guards or [])))
    interp = _script_interpreter(real)
    if interp:
        sub_roots, sub_links = program_mounts(interp, env_path=env_path, guards=guards,
                                              _depth=_depth + 1)
        roots.extend(sub_roots)
        links.extend(sub_links)
    return roots, links


def _script_interpreter(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return ""
    if not head.startswith(b"#!"):
        return ""
    line = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    words = line.split()
    if not words:
        return ""
    if Path(words[0]).name == "env":
        for word in words[1:]:
            if word.startswith("-") or "=" in word:
                continue
            return word
        return ""
    return words[0]


def scan_masks(
    session_dir: Path,
    hide_globs,
    *,
    max_entries: int = MAX_SCAN_ENTRIES,
    hide_git: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Files and folders inside `session_dir` matching `hide_globs`, to hide.

    A folder that matches is hidden whole and not walked; `.git` folders are never
    walked. A symlink that matches hides the in-folder file it points at (one
    pointing outside points at nothing visible).

    With `hide_git`, every `.git` entry (a repository's folder, or a worktree's
    or submodule's pointer file) is hidden too: the history holds every committed
    version of every file, the hidden ones included, and a child with a shell
    could read them back with `git show`.
    """
    root = Path(session_dir).resolve()
    globs = [normalise(str(g)) for g in (hide_globs or ()) if normalise(str(g))]
    files: list[Path] = []
    dirs: list[Path] = []
    if not globs and not hide_git:
        return files, dirs
    seen = 0
    # Each entry is (the real folder, the path it is reached by inside the session).
    # A symlinked folder inside the session is walked again under its link's name
    # (v0.8.5): `never_touch = ["link/secret"]` must hide the file `link/` leads to.
    stack: list[tuple[Path, str]] = [(root, "")]
    walked_links: set[Path] = set()
    while stack:
        current, prefix = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > max_entries:
                raise ConfineError(
                    "confine-too-many-files",
                    f"{root} has more than {max_entries} entries, too many to scan for "
                    "never_touch / forbidden_files masks; the child is not started",
                )
            full = Path(entry.path)
            rel = f"{prefix}/{entry.name}" if prefix else full.relative_to(root).as_posix()
            hit = bool(globs) and matches_any(rel, globs)
            if hide_git and entry.name == ".git":
                hit = True
            try:
                if entry.is_symlink():
                    target = full.resolve()
                    inside = _within(target, root) and target != root
                    if hit:
                        if inside:
                            (dirs if target.is_dir() else files).append(target)
                    elif (inside and globs and target.is_dir()
                          and target not in walked_links):
                        walked_links.add(target)
                        stack.append((target, rel))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if hit:
                        dirs.append(full)
                    elif entry.name != ".git":
                        stack.append((full, rel))
                elif hit:
                    files.append(full)
            except OSError:
                continue
    return files, dirs


def boundary_argv(
    child_argv: list[str],
    *,
    session_dir: Path | str,
    home_dir: Path | str | None,
    hide_globs=(),
    hide_paths=(),
    read_roots=(),
    env: dict | None = None,
    bwrap: str | None = None,
) -> list[str]:
    """Wrap `child_argv` in a bubblewrap boundary (see the module docstring)."""
    if not child_argv:
        raise ConfineError("confine-empty-argv", "nothing to confine: the child argv is empty")
    exe = bwrap or find_bwrap()
    if not exe:
        raise ConfineError("confine-no-bwrap", "bwrap is not installed (not found on PATH)")
    session = Path(session_dir).resolve()
    if not session.is_dir():
        raise ConfineError("confine-no-session-dir", f"session folder {session} does not exist")
    real_home = _real_home()
    if real_home is not None and _within(real_home, session):
        raise ConfineError(
            "confine-session-is-home",
            f"refusing to mount {session} as the session folder: it holds your home folder",
        )
    home = Path(home_dir).resolve() if home_dir else None
    env_path = (env or {}).get("PATH") if env is not None else os.environ.get("PATH")

    hidden_abs = [Path(p).resolve() for p in (hide_paths or ()) if str(p)]
    guards = [session] + hidden_abs + ([real_home] if real_home is not None else [])
    prog_roots: list[Path] = []
    prog_links: list[tuple[str, str]] = []
    targets = [child_argv[0]]
    if (Path(child_argv[0]).name.rstrip("0123456789.") in _INTERPRETERS
            and len(child_argv) > 1 and os.path.isfile(child_argv[1])):
        targets.append(child_argv[1])
    for target in targets:
        roots, links = program_mounts(target, env_path=env_path, guards=guards)
        prog_roots.extend(roots)
        prog_links.extend(links)

    extra_roots: list[Path] = []
    for raw in read_roots or ():
        p = Path(os.path.expanduser(str(raw)))
        if p.is_absolute() and p.exists():
            resolved = p.resolve()
            # v0.8.5: a read root gets the guard an install folder gets: never
            # the home folder or anything holding it (its keys and logins).
            if real_home is not None and _within(real_home, resolved):
                raise ConfineError(
                    "confine-read-root-holds-home",
                    f"refusing to mount confine_read_roots entry {raw!r}: it holds your "
                    "home folder; name the folders inside it the child needs",
                )
            extra_roots.append(resolved)

    args: list[str] = [exe, *_base_args(), "--tmpfs", "/tmp"]
    bound: list[Path] = []
    for root in _dedupe(prog_roots + extra_roots):
        if _under_system(str(root)):
            continue
        args.extend(["--ro-bind", str(root), str(root)])
        bound.append(root)
    for target_text, link_path in _dedupe_links(prog_links):
        lp = Path(link_path)
        if any(_within(lp, b) for b in bound) or _under_system(link_path):
            continue
        args.extend(["--symlink", target_text, link_path])
    args.extend(["--ro-bind", str(session), str(session)])
    bound.append(session)

    files, dirs = scan_masks(session, hide_globs, hide_git=True)
    for hp in hidden_abs:
        if not hp.exists() or not any(_within(hp, b) for b in bound):
            continue
        (dirs if hp.is_dir() else files).append(hp)
    for d in _dedupe(dirs):
        args.extend(["--tmpfs", str(d), "--remount-ro", str(d)])
    for f in _dedupe(files):
        if any(_within(f, d) for d in dirs):
            continue
        args.extend(["--ro-bind", "/dev/null", str(f)])

    if home is not None:
        if real_home is not None and _within(real_home, home):
            raise ConfineError(
                "confine-home-is-real",
                f"refusing to mount {home} read-write: it holds your real home folder",
            )
        args.extend(["--bind", str(home), str(home)])
        args.extend(["--setenv", "HOME", str(home)])
    else:
        # No isolated home: the child gets an empty private one, never the real one.
        fake_home = str(real_home) if real_home is not None else "/home/openfarm"
        args.extend(["--tmpfs", fake_home, "--setenv", "HOME", fake_home])
    # The root is bwrap's own tmpfs, holding the folders made for the mount points;
    # make it read-only so the child writes only in its home and its /tmp.
    args.extend(["--remount-ro", "/"])
    args.extend(["--setenv", "TMPDIR", "/tmp", "--chdir", str(session), "--", *child_argv])
    return args


#: Folders under home that hold logins and keys: hidden from a confined test run.
RUNNER_HIDDEN_HOME = (".ssh", ".gnupg", ".aws", ".config/openfarm", ".claude",
                      ".claude.json", ".codex", ".gemini", ".config/gh", ".netrc",
                      ".docker/config.json", ".kube", ".git-credentials", ".pypirc",
                      ".npmrc")

#: Variables that name a login folder kept outside the default places (v0.8.6).
RUNNER_LOGIN_DIR_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")

_SECRET_ENV_SUFFIXES = ("_TOKEN", "_API_KEY", "_APIKEY", "_SECRET", "_SECRET_KEY",
                        "_ACCESS_KEY", "_PASSWORD", "_PASSWD", "_CREDENTIALS")


def secret_env_names(environ, keep=()) -> list[str]:
    """Names in `environ` that look like tokens, keys or passwords (v0.8.6).

    They are unset inside the runner boundary, since the network is shared and a
    test could send one anywhere. `keep` (what `[runner.env]` sets on purpose)
    stays.
    """
    kept = {str(k) for k in keep}
    out = []
    for name in environ:
        upper = str(name).upper()
        if name in kept:
            continue
        if upper.endswith(_SECRET_ENV_SUFFIXES) or upper in ("GITHUB_TOKEN", "GH_TOKEN"):
            out.append(str(name))
    return sorted(out)


def runner_argv(
    argv: list[str],
    *,
    worktree: Path | str,
    write_roots=(),
    hide_paths=(),
    unset_env=(),
    environ=None,
    bwrap: str | None = None,
) -> list[str]:
    """Wrap one gate command (a build, a test run) in a boundary (v0.8.5).

    The whole system is read-only, so toolchains installed anywhere still work;
    only the card's worktree, the extra `write_roots` (a build target folder, a
    cache) and a private /tmp can be written. A test that writes through a linked
    `node_modules` into the base checkout, or anywhere else, gets an error instead.
    Logins and keys under home, and `hide_paths`, are hidden, and so are the login
    folders `CLAUDE_CONFIG_DIR` and `CODEX_HOME` name wherever they are (v0.8.6).
    The variables in `unset_env` are unset. The network is shared.
    """
    exe = bwrap or find_bwrap()
    if not exe:
        raise ConfineError("confine-no-bwrap", "bwrap is not installed (not found on PATH)")
    tree = Path(worktree).resolve()
    real_home = _real_home()
    if real_home is not None and _within(real_home, tree):
        raise ConfineError("confine-session-is-home",
                           f"refusing to make {tree} writable: it holds your home folder")
    args = [exe, "--die-with-parent", "--unshare-all", "--share-net", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    writable = [tree]
    for raw in write_roots or ():
        if not str(raw):
            continue
        path = Path(os.path.expanduser(str(raw)))
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
        if real_home is not None and _within(real_home, resolved):
            raise ConfineError("confine-write-root-holds-home",
                               f"refusing to make {raw!r} writable: it holds your home folder")
        writable.append(resolved)
    for path in _dedupe(writable):
        args.extend(["--bind", str(path), str(path)])
    hidden: list[Path] = [Path(p) for p in (hide_paths or ()) if str(p)]
    if real_home is not None:
        hidden += [real_home / name for name in RUNNER_HIDDEN_HOME]
    source = os.environ if environ is None else environ
    for name in RUNNER_LOGIN_DIR_ENV:
        raw = str(source.get(name) or "").strip()
        if raw and os.path.isabs(os.path.expanduser(raw)):
            hidden.append(Path(os.path.expanduser(raw)))
    for path in _dedupe(hidden):
        if not os.path.lexists(path):
            continue
        real = path.resolve()
        if any(_within(real, w) for w in writable):
            continue
        if real.is_dir():
            args.extend(["--tmpfs", str(real), "--remount-ro", str(real)])
        else:
            args.extend(["--ro-bind", "/dev/null", str(real)])
    for name in sorted({str(n) for n in unset_env or () if str(n)}):
        args.extend(["--unsetenv", name])
    args.extend(["--setenv", "TMPDIR", "/tmp", "--chdir", str(tree), "--", *argv])
    return args


def _dedupe(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p not in out:
            out.append(p)
    return out


def _dedupe_links(links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for target, path in links:
        if path in seen:
            continue
        seen.add(path)
        out.append((target, path))
    return out


def is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


# -- the bridges' side ------------------------------------------------------------


def bridge_plan(provider: str, tool_policy: str, meta) -> Hold:
    """The hold a bridge applies to one prompt's child.

    The adapter decides and sends it in `_meta.openfarm.confine`; a bridge started
    by anything else decides itself under `auto`.
    """
    if isinstance(meta, dict) and isinstance(meta.get("flags"), bool) \
            and isinstance(meta.get("boundary"), bool):
        mode = str(meta.get("mode") or CONFINE_DEFAULT)
        if mode not in CONFINE_MODES:
            mode = CONFINE_DEFAULT
        return Hold(provider, mode, flags=meta["flags"], boundary=meta["boundary"])
    usable, reason = (False, "") if provider == "claude" else bwrap_status()
    nested, nested_why = (True, "")
    if usable and needs_nested(provider, "bridge", tool_policy, CONFINE_DEFAULT):
        nested, nested_why = nested_status()
    return decide(provider, transport="bridge", tool_policy=tool_policy, mode=CONFINE_DEFAULT,
                  bwrap_usable=usable, bwrap_reason=reason,
                  nested_usable=nested, nested_reason=nested_why)


def bridge_child_argv(child_argv: list[str], hold: Hold, meta, *, session_dir: str,
                      env: dict | None = None) -> list[str]:
    """`child_argv`, wrapped in the boundary when `hold` says so."""
    if hold.refused:
        raise ConfineError("confine-unavailable", hold.refused)
    if not hold.boundary:
        return list(child_argv)
    meta = meta if isinstance(meta, dict) else {}
    source_env = dict(os.environ if env is None else env)
    return boundary_argv(
        child_argv,
        session_dir=session_dir,
        home_dir=source_env.get("HOME") or None,
        hide_globs=[str(g) for g in (meta.get("hideGlobs") or ())],
        hide_paths=[str(p) for p in (meta.get("hidePaths") or ())],
        read_roots=[str(r) for r in (meta.get("readRoots") or ())],
        env=source_env,
        bwrap=str(meta.get("bwrap") or "") or None,
    )


def end_process_group(proc, *, grace_s: float = 1.0) -> None:
    """End `proc` and everything in its process group (it must lead its group)."""
    import signal as _signal

    if proc is None:
        return
    if proc.poll() is not None:
        # The leader is gone but what it started may still run in its group.
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    try:
        os.killpg(proc.pid, _signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, _signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
