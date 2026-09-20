# Sleipnir

One container image that hosts a whole agent fleet: [Herdr](https://herdr.dev) manages the panes, [Moshi](https://getmoshi.app) reports to the phone, Tailscale carries the traffic, and agent CLIs sit ready to be spawned into panes.

## What runs inside

| Service | Type | Enabled by | If it dies |
| --- | --- | --- | --- |
| `sleipnir-init` | oneshot | always | — |
| `dotfiles-init` | oneshot | `SLEIPNIR_ENABLE_DOTFILES` (default 0) | dependent agent services do not start |
| `permissions-init` | oneshot | always | dependent agent services do not start |
| `tailscaled` | longrun | `SLEIPNIR_ENABLE_TAILSCALE` (default 0) | container exits, Kubernetes restarts it |
| `tailscale-up` | oneshot | `SLEIPNIR_ENABLE_TAILSCALE` | — |
| `herdr` | longrun | `SLEIPNIR_ENABLE_HERDR` (default 1) | container exits |
| `moshi` | longrun | `SLEIPNIR_ENABLE_MOSHI` (default 1) | s6 restarts it |
| `code-tunnel` | longrun | `SLEIPNIR_ENABLE_CODE_TUNNEL` (default 0) | s6 restarts it |
| `rc-agy` | longrun | `SLEIPNIR_ENABLE_RC_AGY` (default 0) | s6 restarts it |
| `rc-agy-log` | longrun | always; logger for `rc-agy` | s6 restarts it |
| `rc-claude` | longrun | `SLEIPNIR_ENABLE_RC_CLAUDE` (default 0) | s6 restarts it |
| `rc-claude-log` | longrun | always; logger for `rc-claude` | s6 restarts it |
| `rc-codex` | longrun | `SLEIPNIR_ENABLE_RC_CODEX` (default 0) | s6 restarts it |
| `scheduler` | longrun | `SLEIPNIR_ENABLE_SCHEDULER` (default 0) | s6 restarts it |

Every service drops to `ubuntu` with `s6-setuidgid`. Only the s6 supervision tree runs as root, which is s6-overlay's normal arrangement.

`permissions-init` runs after `dotfiles-init` and re-tightens `~/.gnupg` and `~/.ssh` to `0700`, plus the files in them to owner-only. Both are PVC mounts, so they arrive as `0775` with the fsGroup setgid bit, and the CephFS volumes carry a default ACL that grants the group write access on new files whatever the umask is. GnuPG and OpenSSH both refuse a home directory anyone but the owner can reach, so without this step `gpg -k` warns about unsafe permissions and `ssh` rejects `~/.ssh/config`.

`rc-claude` runs with `--no-create-session-in-dir`, so nothing is pre-created in `/workspace`; every session comes from claude.ai/code or the app on demand. Those are auto-named, and the name prefix comes from `--remote-control-session-name-prefix` (`CLAUDE_RC_NAME`) rather than `--name`, which only labels a session the service creates itself. Claude lowercases the prefix and replaces everything but letters and digits with `-`.

Its status screen redraws about once a second, and with no TTY under s6 every redraw is reprinted in full, which buries everything else in `kubectl logs`. `remote-control` has no quiet mode, so `rc-claude` is an s6 pipeline instead: `producer-for` sends its stdout to `rc-claude-log`, which is `s6-log -b n3 s1000000 T /var/log/rc-claude` — four rotated 1MB files, timestamped, readable with `tail /var/log/rc-claude/current`. Only stdout is piped, so anything the CLI writes to stderr still reaches the container log.

`rc-agy` needs a detour. On Linux `agy remote-control start` writes a systemd user unit and hands it to `systemctl --user`, which cannot work here: there is no systemd and no session bus, so it fails at `daemon-reload`. The unit it writes points at `agy remote-control serve`, an undocumented foreground subcommand, and that is what the service runs directly under s6 — no systemd involved. The rest of the unit's semantics move into the service directory: `finish` stops the service for good on exit 3 (the unit's `RestartPreventExitStatus`) and otherwise sleeps 10s before s6 restarts it (`RestartSec`), with `timeout-finish` raised to 15s so that sleep survives. The sleep is skipped when the service is being stopped on purpose, so shutting the container down does not wait on it. The name shown in Antigravity Remote Control is set once with `agy remote-control start --name <name>` — it records the name before the systemd step fails — and persists in `~/.gemini/config/config.json`, which is on a PVC.

The three `rc-*` services exist so the agent CLIs own mobile apps keep working. A session started that way is a **separate process** from the ones herdr spawns into panes: it shares the same `~/.claude`, `~/.codex` or `~/.gemini`, but it is not in a pane, so neither herdr nor Moshi can see it. That is expected.


`scheduler` runs a user-authored crontab with [Supercronic](https://github.com/aptible/supercronic). Supercronic fits the container better than `cron` or a systemd timer: PID 1 is `s6-svscan`, systemd is offline, and Supercronic is one static binary that logs to stdout without needing root or setuid. The service validates the crontab, drops to `ubuntu`, and watches the file for changes, including Kubernetes ConfigMap atomic updates.

The default crontab is `/etc/sleipnir/scheduler/crontab`; override it with `SLEIPNIR_SCHEDULER_CRONTAB`. One file can hold any number of jobs and may set `CRON_TZ` itself. Commands should use absolute paths. They start in `/home/ubuntu` unless `SLEIPNIR_SCHEDULER_WORKDIR` says otherwise. A missing or empty crontab brings the service down cleanly because having nothing scheduled is a normal state.

## Agent CLIs

`claude`, `codex`, `agy`, `copilot`, `opencode` — installed at build time from the same [progamesigner/devcontainers](https://github.com/progamesigner/devcontainers) feature installers the devcontainers use, so the versions and install paths stay consistent between the two. The image also includes the `devtools` feature (with cosign) and uses zsh as `ubuntu`’s login and interactive shell.

`gh` comes from the same feature set. The agents lean on it for anything involving a pull request, and the pod mounts a PVC at `~/.config/gh` to keep the login, so it belongs in the image rather than in `~/.local/bin`.

Beyond the language toolchains the image carries the things an agent reaches for when a repository does not build on the first try: `uv`/`uvx`, `build-essential`, `git`, `jq`, `ripgrep`, `vim`, `curl`, `wget`, `zip`/`unzip`, and `sudo`. `ubuntu` has passwordless sudo — s6 already supervises as root and drops each service with `s6-setuidgid`, so this grants a herdr pane nothing the supervision tree did not already have.

## Docker sidecar

The image includes Docker CLI 29.8.1, Buildx, and Compose, but no Docker daemon. It expects a per-pod `docker:29.8.1-dind-rootless` sidecar and connects through the Unix socket at `/run/docker/docker.sock`. The agent can use the normal workflow without pushing an intermediate image:

```sh
docker build -t app:test .
docker run --rm app:test
docker compose up -d
```

Mount the same socket `emptyDir` at `/run/user/1000` in the sidecar and `/run/docker` in Sleipnir. Mount the workspace at `/workspace` in both containers too: bind-mount source paths are resolved by the daemon, so `docker run -v "$PWD:/app" ...` only works when both containers see the same absolute path.

The daemon data directory `/home/rootless/.local/share/docker` should use a size-limited local `emptyDir`. Do not put Docker's graph data on the shared CephFS workspace or another network filesystem. Losing this volume on pod replacement is intentional: repositories remain on their own volume, while containers, images, and build cache are disposable.

Run the sidecar with an explicit Unix-only host:

```yaml
securityContext:
  fsGroup: 1000
containers:
- name: docker
  image: docker:29.8.1-dind-rootless
  command:
  - dockerd-rootless.sh
  args:
  - --host=unix:///run/user/1000/docker.sock
  securityContext:
    privileged: true
    runAsUser: 1000
    runAsGroup: 1000
  readinessProbe:
    exec:
      command: [docker, --host, unix:///run/user/1000/docker.sock, info]
  volumeMounts:
  - name: docker-socket
    mountPath: /run/user/1000
  - name: docker-data
    mountPath: /home/rootless/.local/share/docker
  - name: workspace
    mountPath: /workspace
volumes:
- name: docker-socket
  emptyDir: {}
- name: docker-data
  emptyDir:
    sizeLimit: 20Gi
```

Add the matching `docker-socket` and `workspace` mounts to the Sleipnir container. The explicit `dockerd-rootless.sh` command bypasses the official DinD entrypoint's automatic TCP listener; do not replace it with the default entrypoint, expose port 2375, or mount the Kubernetes node's Docker/containerd socket.

Docker-in-Docker requires `privileged`, including the rootless variant. Treat each Sleipnir pod as a trusted development worker, apply CPU, memory, PID, and ephemeral-storage limits, and schedule it on a dedicated node pool when repositories are not fully trusted. Rootless dockerd reduces the privileges of nested containers, but it does not remove the outer sidecar's Kubernetes privilege boundary.

Each language also needs the thing that actually installs its dependencies, otherwise the toolchain cannot check out a repository and build it:

| Language | Installer | How it gets in |
| --- | --- | --- |
| PHP | `composer` | `COPY --from=composer/composer:<version>-bin` — that image holds the phar and nothing else |
| Node | `npm` | bundled with the node feature |
| Python | `uv`, `uvx` | `COPY --from=ghcr.io/astral-sh/uv` |
| Rust | `cargo` (with `clippy` and `rustfmt`) | already in the rust feature |
| Go | `go` | already in the go feature |

`UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` point at `/usr/local/uv`, owned by `ubuntu` and on `PATH`, so `uv tool install` from a pane lands somewhere usable. A repository that needs pnpm or yarn can reach them through `corepack`, which Node 24 still bundles.

`herdr` and `moshi` come from the same place, built with `BRIDGE=false`: their devcontainer bridge exists to reach a Mac's socket over SSH, and Sleipnir has no Mac to borrow from. It runs its own herdr server and its own Moshi daemon.

### Updating a CLI without rebuilding

`PATH` puts `/home/ubuntu/.local/bin` ahead of `/usr/local/bin`, and the baseline binaries are owned by `ubuntu`. So `herdr update`, `moshi-hook update`, `claude update` and friends all work from inside the pod. Whether an update survives a restart depends on where that tool installs:

- into `~/.local/bin` — persists, that path is on a PVC
- in place over `/usr/local/bin` — lost on restart, back to the image's version

Delete the override in `~/.local/bin` to fall back to the image baseline.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TS_AUTHKEY` | — | Tailscale auth key. Use a **reusable, tagged** key; an ephemeral node is deleted when it goes offline and comes back with a new address. |
| `TS_HOSTNAME` | `sleipnir` | Tailnet hostname. Set it per pod, otherwise the node shows up under the pod name and changes on every redeploy. |
| `TS_STATE_DIR` | `/var/lib/tailscale` | Also where Tailscale SSH keeps its host keys. |
| `SLEIPNIR_ENABLE_TAILSCALE` | `0` | Set to `1` to run Tailscale and connect the node. |
| `SLEIPNIR_ENABLE_DOTFILES` | `0` | Set to `1` to clone and install dotfiles. |
| `DOTFILES_REPOSITORY` | `https://github.com/progamesigner/dotfiles` | Git repository cloned into `/home/ubuntu/.dotfiles` on startup. |
| `DOTFILES_INSTALL_SCRIPT` | `install.sh` | Repository-relative installer executed as `ubuntu`. |
| `MOSHI_LISTEN` | `0.0.0.0:24544` | Moshi web client. |
| `SLEIPNIR_ENABLE_CODE_TUNNEL` | `0` | Set to `1` to run the VS Code tunnel. |
| `CODE_TUNNEL_NAME` | `sleipnir` | Name shown in vscode.dev. The CLI lowercases the name and accepts only letters, digits and `-`, up to 20 characters, so an uppercase value registers lowercased. |
| `CLAUDE_RC_NAME` | `$TS_HOSTNAME` | Name shown in the Claude app. It is also passed as the session name prefix; without it the prefix falls back to the hostname, which under Kubernetes is the pod name unless the pod sets `hostname`. |
| `HERDR_STARTUP_CWD` | `/workspace` | herdr seeds an initial shell pane here. |
| `SLEIPNIR_ENABLE_SCHEDULER` | `0` | Set to `1` to run the scheduler. |
| `SLEIPNIR_SCHEDULER_CRONTAB` | `/etc/sleipnir/scheduler/crontab` | Authored crontab to validate, watch and run. Mount a file here or point to another path. |
| `SLEIPNIR_SCHEDULER_WORKDIR` | `/home/ubuntu` | Working directory inherited by scheduled commands. Prefer absolute command paths in the crontab. |
### Tailscale

`tailscaled` keeps its identity and SSH host keys in `TS_STATE_DIR` (default
`/var/lib/tailscale`). Its disposable log spool uses `TS_LOGS_DIR` (default
`/run/tailscale/logs`), created with ownership for `ubuntu` before the daemon
starts. Keep that directory on local storage: synchronous log rotation on a
network filesystem can block logging and operations holding backend locks.
Pending uploaded logs and the log ID may be lost on container replacement;
the Tailscale node identity stays on the persistent volume.

### Health checks and recovery

`/usr/local/bin/sleipnir-healthcheck` uses Bash, coreutils, curl and jq already
in the image; it needs no Python runtime. Its `live` mode checks s6 process status
for enabled Herdr, Moshi and Tailscale services, connects to the configured Moshi listener,
and queries Tailscale's LocalAPI with a 3-second request deadline. Each check is
bounded; allow 10 seconds for the Kubernetes exec probe. Disabled services are
skipped. This catches an unresponsive daemon even when its PID and socket remain.

`sleipnir-healthcheck ready` additionally requires Tailscale's `Running` state
and an assigned address. Login or device approval failures leave the container
alive but not ready. Neither probe depends on a public service or a particular
peer, so an upstream outage does not directly cause a restart loop. These are
local checks; they do not prove that tailnet routing, ACLs, SSH or a complete
Moshi session works. Monitor those from another tailnet node separately.

Use `live` for startup (10-second interval, 30 failures) and liveness (30-second
interval, 3 failures), and `ready` for readiness (10-second interval, 2 failures).
On an unexpected Tailscale or Herdr exit, the finish script records a nonzero
exit code and invokes s6-overlay's `halt` entrypoint for an orderly container
shutdown. Kubernetes then restarts the container. A liveness failure provides
a fallback if shutdown or a service hangs; recovery interrupts in-memory agent
sessions. An uninterruptible kernel I/O wait can still require storage/node repair.

Publish the image containing the healthcheck and update the manifests image pin
before enabling these probes.

Run the health and finish regression tests with
`python3 -m unittest discover -s tests -v`. They include real Unix-socket HTTP
success, failure and timeout cases; full s6/container lifecycle validation still
requires a container runtime.

### Tailscale networking

Runs in userspace networking mode, which needs no `NET_ADMIN` and no `/dev/net/tun`. Verified as uid 1000 with an empty capability set: the daemon comes up, `--ssh` is accepted, host keys are generated, inbound SSH reaches the policy check, and `tailscale serve` proxies a TCP port.

Two consequences of userspace mode, neither of which this pod runs into:

- outbound connections **to other tailnet nodes** have to go through the SOCKS5 (`localhost:1055`) or HTTP (`localhost:1056`) proxy. Everything here talks to the public internet or to the cluster instead.
- it cannot use an exit node or a subnet router.

Tailscale SSH needs both a network grant for TCP port 22 and an SSH rule. The following policy limits access to the tailnet owner and the `ubuntu` account:

```json
"tagOwners": {
  "tag:sleipnir": ["autogroup:owner"]
},
"grants": [
  {
    "src": ["autogroup:owner"],
    "dst": ["tag:sleipnir"],
    "ip": ["tcp:22"]
  }
],
"ssh": [
  {
    "action": "accept",
    "src": ["autogroup:owner"],
    "dst": ["tag:sleipnir"],
    "users": ["ubuntu"]
  }
]
```

The node must also carry `tag:sleipnir`, either from a pre-tagged auth key or from the Machines page. Use `"action": "check"` with a `checkPeriod` when periodic reauthentication is preferred.

## Volumes

Each agent keeps its own credentials, so each gets its own volume. On every start, `sleipnir-init` creates these writable directories and corrects their ownership to `ubuntu:ubuntu`; this also handles fresh PVC mount points. Two are easy to miss:

| Path | Holds |
| --- | --- |
| `/home/ubuntu/.claude` | Claude Code, including `.credentials.json` |
| `/home/ubuntu/.codex` | Codex |
| `/home/ubuntu/.gemini` | Antigravity, including `config/hooks.json` |
| `/home/ubuntu/.copilot` | Copilot |
| `/home/ubuntu/.config/opencode` | OpenCode configuration |
| `/home/ubuntu/.local/share/opencode` | OpenCode **data** — a separate directory from the config |
| `/home/ubuntu/.config/herdr` | herdr socket, config, logs |
| `/home/ubuntu/.config/moshi` | Moshi settings |
| `/home/ubuntu/.local/state/moshi` | Moshi **pairing secret** — lose it and you pair the phone again |
| `/home/ubuntu/.local/bin` | CLI updates that outlive a restart |
| `/home/ubuntu/.vscode-cli` | VS Code tunnel; the server downloads on first start |
| `/home/ubuntu/.vscode-server` | Remote extensions and their `globalStorage`; without it every restart reinstalls them |
| `/var/lib/tailscale` | Tailscale state and SSH host keys; owned by `ubuntu`, because `tailscaled` does not run as root |
| `/workspace` | code |

## First start

Nothing is logged in yet, and that is fine: Herdr and Moshi come up on their own, so there is a way in before any agent exists.

1. If enabling Tailscale, approve the node or use a pre-approved tagged auth key.
2. Connect through an enabled access path, such as `ssh ubuntu@<TS_HOSTNAME>` with Tailscale or the Moshi web client on port 24544.
3. Log into each CLI once. Credentials land on the volumes and stay there.
4. Pair Moshi: `moshi-hook pair --token <token>`, then `moshi-hook install`.
5. `code tunnel` prints a device-login URL on its first run; read it with `s6-svc` logs or from the pane.

## Building

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/progamesigner/sleipnir:latest .
```

Release assets disagree about how to spell an architecture, so the Dockerfile maps `TARGETARCH` per tool: s6-overlay wants `x86_64`/`aarch64`, Tailscale takes `amd64`/`arm64` unchanged, the VS Code CLI wants `x64`/`arm64`, and inside the feature installers herdr uses `aarch64` where moshi-hook uses `arm64`.

Schedule the pods onto the x86 nodes. A Raspberry Pi will not carry multiple agent CLIs.
