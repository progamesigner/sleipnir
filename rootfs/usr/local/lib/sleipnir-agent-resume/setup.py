#!/usr/bin/env python3
"""Append Sleipnir's session hooks without replacing user settings or hook order."""
import json
import os
from pathlib import Path
import sys
import tempfile


SCRIPT = "/usr/local/bin/sleipnir-agent-resume"


def install(path, agent):
    old = path.read_bytes() if path.exists() else None
    config = json.loads(old) if old is not None else {}
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
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
                return
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
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
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(config, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    if os.environ.get("SLEIPNIR_ENABLE_AGENT_RESUME", "0") != "1":
        return
    home = Path.home()
    targets = ((home / ".claude/settings.json", "claude"),
               (Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "hooks.json", "codex"))
    for path, agent in targets:
        if not path.parent.is_dir():
            continue
        try:
            install(path, agent)
        except (OSError, ValueError) as error:
            print(f"sleipnir: could not install {agent} resume hook in {path}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
