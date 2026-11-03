#!/usr/bin/env python3
"""Append Sleipnir's session hooks without replacing user settings or hook order."""
import json
import os
from pathlib import Path
import sys
import tempfile


SCRIPT = "/usr/local/bin/sleipnir-agent-resume"
BLOCK = "sleipnir-agent-resume"
HERE = Path(__file__).resolve().parent


def session_start(config, agent):
    """Claude Code / Codex: append to hooks.SessionStart. False when already there."""
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise ValueError("SessionStart must be an array")
    command = f"{SCRIPT} hook {agent}"
    # Keep existing entries and their positions: Codex hook trust depends on them.
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            if any(isinstance(h, dict) and h.get("command") == command for h in group["hooks"]):
                return False
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    return True


def agy_block(config, agent):
    """Antigravity: a named hook block. SessionStart covers new conversations,
    PreInvocation resumed ones (it has no session event for those)."""
    handler = {"type": "command", "command": f"{SCRIPT} hook {agent}", "timeout": 10}
    block = {"SessionStart": [handler], "PreInvocation": [handler]}
    current = config.get(BLOCK)
    if current == block:
        return False
    if current is not None and not ours(current):
        raise ValueError(f"{BLOCK} is already defined by something else")
    config[BLOCK] = block
    return True


def ours(block):
    return isinstance(block, dict) and any(
        isinstance(h, dict) and str(h.get("command", "")).startswith(SCRIPT)
        for handlers in block.values() if isinstance(handlers, list) for h in handlers)


def install(path, agent, merge):
    old = path.read_bytes() if path.exists() else None
    config = json.loads(old) if old is not None else {}
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
    if not merge(config, agent):
        return
    if old is not None:
        # One pre-install copy; repeated container starts do not accumulate backups.
        backup = path.with_name(path.name + ".before-sleipnir-agent-resume")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(old)
    write(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def write(path, text):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o7777)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def own(path, text):
    """A file only Sleipnir writes: replace it when it differs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        write(path, text)


def copilot_hooks(agent):
    # PascalCase event names select the snake_case payload the hook reads.
    hook = {"type": "command", "command": f"{SCRIPT} hook {agent}", "timeoutSec": 10}
    return json.dumps({"version": 1, "hooks": {"SessionStart": [hook]}}, indent=2) + "\n"


def install_agy(gemini):
    (gemini / "config").mkdir(exist_ok=True)
    install(gemini / "config" / "hooks.json", "agy", agy_block)


def main():
    if os.environ.get("SLEIPNIR_ENABLE_AGENT_RESUME", "0") != "1":
        return
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    copilot = Path(os.environ.get("COPILOT_HOME") or home / ".copilot")
    opencode = Path(os.environ.get("OPENCODE_CONFIG_DIR") or config_home / "opencode")
    # (agent, the folder that shows it is set up, how to install there)
    targets = (
        ("claude", home / ".claude", lambda d: install(d / "settings.json", "claude", session_start)),
        ("codex", Path(os.environ.get("CODEX_HOME") or home / ".codex"),
         lambda d: install(d / "hooks.json", "codex", session_start)),
        ("copilot", copilot, lambda d: own(d / "hooks" / f"{BLOCK}.json", copilot_hooks("copilot"))),
        ("agy", home / ".gemini", install_agy),
        ("opencode", opencode, lambda d: own(d / "plugins" / BLOCK / "tui.js",
                                             (HERE / "opencode_tui.js").read_text(encoding="utf-8"))),
    )
    for agent, folder, setup in targets:
        if not folder.is_dir():
            continue
        try:
            setup(folder)
        except (OSError, ValueError) as error:
            print(f"sleipnir: could not install {agent} resume hook in {folder}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
