import base64
import json
import os
import re
import tomllib
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

type SourceKind = Literal[
    'docker',
    'releases',
    'tags',
    'plain',
    'go',
    'node',
    'rust',
    'npm',
]
type UpdatePolicy = Literal['major', 'series']
type Version = tuple[int, int, int, int]


class Dependency(TypedDict):
    name: str
    kind: SourceKind
    source: str
    prefix: str
    policy: UpdatePolicy


class Candidate(TypedDict):
    version: str
    notes: str
    url: str


class Change(Candidate):
    arg: str
    current: str
    status: Literal['update']
    history_complete: bool
    notes_truncated: NotRequired[bool]
    source: NotRequired[str]
    policy: NotRequired[UpdatePolicy]


class Observation(TypedDict):
    arg: str
    status: Literal['unchanged', 'floating', 'manual', 'error']
    current: NotRequired[str]
    source: NotRequired[str]
    policy: NotRequired[UpdatePolicy]
    reason: NotRequired[str]
    error: NotRequired[str]
    history_complete: NotRequired[bool]


type Inventory = Change | Observation


class Plan(TypedDict):
    owner: str
    repository: str
    source_sha: str
    original_content: str
    updated_content: str
    changes: list[Change]
    inventory: list[Inventory]
    unmanaged_inputs: list[str]
    can_apply: bool


class ReleaseResponse(TypedDict, total=False):
    draft: bool
    prerelease: bool
    tag_name: str
    name: str
    body: str | None
    html_url: str | None


class ImageResponse(TypedDict, total=False):
    architecture: str


class DockerTagResponse(TypedDict):
    name: str
    images: NotRequired[list[ImageResponse]]


class DockerResponse(TypedDict):
    results: list[DockerTagResponse]
    next: str | None


class NodeResponse(TypedDict):
    version: str
    lts: str | bool


class GoResponse(TypedDict):
    version: str
    stable: bool


class NpmResponse(TypedDict):
    version: str


class VSCodeResponse(TypedDict):
    version: str
    productVersion: str


class GitObject(TypedDict):
    sha: str


class RefResponse(TypedDict):
    object: GitObject


class ContentResponse(TypedDict):
    encoding: str
    content: str


class CommitMessage(TypedDict):
    message: str


class CommitResponse(TypedDict):
    commit: CommitMessage


class CompareResponse(TypedDict):
    status: str
    commits: list[CommitResponse]
    total_commits: int


class RustPackage(TypedDict):
    version: str


class RustPackages(TypedDict):
    rust: RustPackage


class RustManifest(TypedDict):
    pkg: RustPackages


CATALOG: list[Dependency] = [
    Dependency(
        name='COMPOSER_VERSION',
        kind='docker',
        source='composer/composer',
        prefix='',
        policy='major',
    ),
    Dependency(
        name='UBUNTU_VERSION',
        kind='docker',
        source='library/ubuntu',
        prefix='',
        policy='series',
    ),
    Dependency(
        name='UV_VERSION',
        kind='releases',
        source='astral-sh/uv',
        prefix='',
        policy='major',
    ),
    Dependency(
        name='S6_OVERLAY_VERSION',
        kind='releases',
        source='just-containers/s6-overlay',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='SUPERCRONIC_VERSION',
        kind='releases',
        source='aptible/supercronic',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='TAILSCALE_VERSION',
        kind='releases',
        source='tailscale/tailscale',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='GITHUB_CLI_VERSION',
        kind='releases',
        source='cli/cli',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='HERDR_VERSION',
        kind='releases',
        source='herdrdev/herdr',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='MOSHI_VERSION',
        kind='plain',
        source='https://cdn.getmoshi.app/hook/latest/version.txt',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='DOCKER_VERSION',
        kind='releases',
        source='moby/moby',
        prefix='docker-v',
        policy='major',
    ),
    Dependency(
        name='DOCKER_BUILDX_VERSION',
        kind='releases',
        source='docker/buildx',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='DOCKER_COMPOSE_VERSION',
        kind='releases',
        source='docker/compose',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='DEVTOOLS_COSIGN_VERSION',
        kind='releases',
        source='sigstore/cosign',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='BUN_VERSION',
        kind='releases',
        source='oven-sh/bun',
        prefix='bun-v',
        policy='major',
    ),
    Dependency(
        name='DENO_VERSION',
        kind='releases',
        source='denoland/deno',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='GO_VERSION',
        kind='go',
        source='https://go.dev/dl/?mode=json',
        prefix='go',
        policy='series',
    ),
    Dependency(
        name='NODE_VERSION',
        kind='node',
        source='https://nodejs.org/dist/index.json',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='PHP_VERSION',
        kind='tags',
        source='php/php-src',
        prefix='php-',
        policy='series',
    ),
    Dependency(
        name='PYTHON_VERSION',
        kind='tags',
        source='python/cpython',
        prefix='v',
        policy='series',
    ),
    Dependency(
        name='RUST_VERSION',
        kind='rust',
        source='https://static.rust-lang.org/dist/channel-rust-stable.toml',
        prefix='',
        policy='major',
    ),
    Dependency(
        name='COPILOT_VERSION',
        kind='releases',
        source='github/copilot-cli',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='OPENCODE_VERSION',
        kind='releases',
        source='anomalyco/opencode',
        prefix='v',
        policy='major',
    ),
    Dependency(
        name='CLAUDE_CODE_VERSION',
        kind='npm',
        source='@anthropic-ai/claude-code',
        prefix='',
        policy='major',
    ),
    Dependency(
        name='CODEX_VERSION',
        kind='npm',
        source='@openai/codex',
        prefix='',
        policy='major',
    ),
]
AGENT_LATEST: set[str] = {
    'ANTIGRAVITY_CLI_VERSION',
    'CLAUDE_CODE_VERSION',
    'CODEX_VERSION',
    'COPILOT_VERSION',
    'OPENCODE_VERSION',
}
UNPINNED: dict[str, str] = {
    'ANTIGRAVITY_CLI_VERSION': 'Official manifest only exposes its current version; old-version fallback must never install latest',
    'DEVTOOLS_CLOUDFLARED_VERSION': 'Disabled in devtools; keep none',
    'DEVTOOLS_TAILSCALE_VERSION': 'Disabled in devtools; separate TAILSCALE_VERSION manages the installed binary',
}
MUTABLE_INPUTS: list[str] = [
    'apt packages and transitive dependencies',
    'PHP helper scripts fetched from master and Node signing keys fetched from main',
    'runtime dotfiles, extensions and persistent tool directories',
]


def fetch_text(token: str, url: str) -> str:
    headers = {'User-Agent': 'sleipnir-dependency-updater'}
    if urlparse(url).netloc == 'api.github.com':
        headers.update(
            {
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            }
        )
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return cast(bytes, response.read()).decode()


def fetch_json[Response](token: str, url: str, _: type[Response]) -> Response:
    return cast(Response, json.loads(fetch_text(token, url)))


def version(value: str) -> Version | None:
    if not re.fullmatch(r'\d+(?:\.\d+){1,3}', value):
        return None
    parts = tuple(map(int, value.split('.')))
    padded = parts + (0,) * (4 - len(parts))
    return padded[0], padded[1], padded[2], padded[3]


def read_version(content: str, name: str) -> str:
    matches: list[str] = re.findall(
        rf'^{re.escape(name)}=([^\s]+)[ \t]*$', content, re.MULTILINE
    )
    if len(matches) != 1:
        raise ValueError(
            f'{name}: expected exactly one version entry, found {len(matches)}'
        )
    return matches[0]


def replace_version(content: str, name: str, old: str, new: str) -> str:
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', name):
        raise ValueError('Invalid version key')
    if not (
        version(new)
        or (
            name in {'DEVCONTAINERS_REF', 'VSCODE_COMMIT'}
            and re.fullmatch(r'[a-f0-9]{40}', new)
        )
    ):
        raise ValueError(f'{name}: invalid replacement')
    if read_version(content, name) != old:
        raise ValueError(f'{name}: current value changed')
    return re.sub(
        rf'(^{re.escape(name)}=){re.escape(old)}(?=[ \t]*$)',
        lambda m: m[1] + new,
        content,
        count=1,
        flags=re.MULTILINE,
    )


def candidates(
    token: str, kind: SourceKind, source: str, prefix: str, current: str
) -> tuple[list[Candidate], bool]:
    records: list[Candidate] = []
    complete = True
    if kind in ('releases', 'tags'):
        complete = False
        for page in range(1, 6):
            rows = fetch_json(
                token,
                f'https://api.github.com/repos/{source}/{kind}?per_page=100&page={page}',
                list[ReleaseResponse],
            )
            for row in rows:
                if row.get('draft') or row.get('prerelease'):
                    continue
                tag = row.get('tag_name', row.get('name', ''))
                if not tag.startswith(prefix):
                    continue
                value = tag[len(prefix) :]
                if version(value):
                    records.append(
                        {
                            'version': value,
                            'notes': row.get('body') or '',
                            'url': row.get('html_url')
                            or f'https://github.com/{source}/releases/tag/{quote(tag, safe="")}',
                        }
                    )
            if len(rows) < 100 or any(r['version'] == current for r in records):
                complete = True
                break
    elif kind == 'docker':
        url = f'https://hub.docker.com/v2/repositories/{source}/tags?page_size=100&ordering=last_updated'
        complete = False
        for _ in range(5):
            data = fetch_json(token, url, DockerResponse)
            for tag_data in data['results']:
                tag = tag_data['name']
                value = (
                    tag.removesuffix('-bin') if source == 'composer/composer' else tag
                )
                if source == 'composer/composer' and not tag.endswith('-bin'):
                    continue
                arches = {i.get('architecture') for i in tag_data.get('images', [])}
                if version(value) and {'amd64', 'arm64'} <= arches:
                    records.append(
                        {
                            'version': value,
                            'notes': '',
                            'url': f'https://hub.docker.com/r/{source}/tags?name={tag}',
                        }
                    )
            next_url = data['next']
            if not next_url or any(r['version'] == current for r in records):
                complete = True
                break
            url = next_url
    elif kind == 'node':
        records = [
            {
                'version': row['version'].removeprefix('v'),
                'notes': '',
                'url': f'https://nodejs.org/en/blog/release/{row["version"]}',
            }
            for row in fetch_json(token, source, list[NodeResponse])
            if row['lts']
        ]
    elif kind == 'go':
        records = [
            {
                'version': row['version'].removeprefix('go'),
                'notes': '',
                'url': 'https://go.dev/doc/devel/release',
            }
            for row in fetch_json(token, source, list[GoResponse])
            if row['stable']
        ]
    elif kind == 'rust':
        value = cast(RustManifest, tomllib.loads(fetch_text(token, source)))['pkg'][
            'rust'
        ]['version'].split()[0]
        records = [{'version': value, 'notes': '', 'url': 'https://releases.rs/'}]
    elif kind == 'npm':
        package = fetch_json(
            token,
            f'https://registry.npmjs.org/{quote(source, safe="")}/latest',
            NpmResponse,
        )
        records = [
            {
                'version': package['version'],
                'notes': '',
                'url': f'https://www.npmjs.com/package/{source}/v/{package["version"]}',
            }
        ]
    elif kind == 'plain':
        records = [
            {
                'version': fetch_text(token, source).strip().removeprefix(prefix),
                'notes': '',
                'url': source,
            }
        ]
    else:
        raise ValueError(f'Unknown source: {kind}')
    return records, complete


def resolve(token: str, spec: Dependency, content: str) -> Inventory:
    name = spec['name']
    kind, source, prefix = spec['kind'], spec['source'], spec['prefix']
    policy = spec['policy']
    current = read_version(content, name)
    parsed = version(current)
    if not parsed and not (
        current == 'latest'
        and name
        in {
            'DOCKER_BUILDX_VERSION',
            'DOCKER_COMPOSE_VERSION',
            'DEVTOOLS_COSIGN_VERSION',
        }
    ):
        raise ValueError(f'{name}: current value is not a pinned stable version')
    rows, complete = candidates(token, kind, source, prefix, current)
    if not any(version(row['version']) for row in rows):
        raise ValueError(
            f'{name}: source returned no stable versions matching the tag format'
        )
    compatible: list[Candidate] = []
    for row in rows:
        candidate = version(row['version'])
        if not candidate or (parsed and candidate <= parsed):
            continue
        width = 2 if policy == 'series' else 1
        if parsed and candidate[:width] != parsed[:width]:
            continue
        if name == 'TAILSCALE_VERSION' and candidate[1] % 2:
            continue
        compatible.append(row)
    if not compatible:
        return Observation(
            arg=name,
            current=current,
            source=source,
            policy=policy,
            history_complete=complete,
            status='unchanged',
        )
    selected = max(compatible, key=lambda row: version(row['version']) or (0, 0, 0, 0))
    ordered = sorted(
        compatible, key=lambda row: version(row['version']) or (0, 0, 0, 0)
    )
    notes = '\n\n'.join(
        f'{row["version"]}: {row["notes"] or "Release notes unavailable; inspect source link."}\n{row["url"]}'
        for row in ordered
    )
    return Change(
        arg=name,
        current=current,
        source=source,
        policy=policy,
        history_complete=complete,
        status='update',
        version=selected['version'],
        url=selected['url'],
        notes=notes[:12000],
        notes_truncated=len(notes) > 12000,
    )


def resolve_vscode(token: str, current: str) -> Inventory:
    if not re.fullmatch(r'[a-f0-9]{40}', current):
        raise ValueError('VSCODE_COMMIT must be a pinned commit SHA')
    metadata = [
        fetch_json(
            token,
            f'https://update.code.visualstudio.com/api/update/cli-linux-{arch}/stable/'
            + '0' * 40,
            VSCodeResponse,
        )
        for arch in ('x64', 'arm64')
    ]
    commit = metadata[0]['version']
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('VS Code metadata returned an invalid commit')
    if (
        metadata[1]['version'] != commit
        or metadata[0]['productVersion'] != metadata[1]['productVersion']
    ):
        raise ValueError(
            'VS Code stable releases are not synchronized across architectures; retry later'
        )
    if current == commit:
        return Observation(arg='VSCODE_COMMIT', current=current, status='unchanged')
    return {
        'arg': 'VSCODE_COMMIT',
        'current': current,
        'version': commit,
        'status': 'update',
        'url': f'https://github.com/microsoft/vscode/compare/{current}...{commit}',
        'history_complete': False,
        'notes': f'Official stable VS Code CLI {metadata[0]["productVersion"]}, commit {commit}, available for x64 and arm64. Full release notes were not retrieved.',
    }


def resolve_devcontainers(token: str, current: str) -> Inventory:
    if not re.fullmatch(r'[a-f0-9]{40}', current):
        raise ValueError('DEVCONTAINERS_REF must be a pinned commit SHA')
    target = fetch_json(
        token,
        'https://api.github.com/repos/progamesigner/devcontainers/git/ref/heads/main',
        RefResponse,
    )['object']['sha']
    if current == target:
        return Observation(arg='DEVCONTAINERS_REF', current=current, status='unchanged')
    entry: Change = {
        'arg': 'DEVCONTAINERS_REF',
        'current': current,
        'version': target,
        'status': 'update',
        'url': f'https://github.com/progamesigner/devcontainers/compare/{current}...{target}',
        'history_complete': True,
        'notes': '',
    }
    if current != target:
        comparison = fetch_json(
            token,
            f'https://api.github.com/repos/progamesigner/devcontainers/compare/{current}...{target}',
            CompareResponse,
        )
        if comparison['status'] not in ('ahead', 'identical'):
            raise ValueError(
                'devcontainers main no longer descends from the pinned commit'
            )
        entry['notes'] = '\n'.join(
            c['commit']['message'].splitlines()[0] for c in comparison['commits']
        )
        entry['history_complete'] = (
            len(comparison['commits']) == comparison['total_commits']
        )
    return entry


def build_plan(token: str, owner: str, repository: str) -> Plan:
    repo = f'https://api.github.com/repos/{owner}/{repository}'
    source_sha = fetch_json(token, f'{repo}/git/ref/heads/main', RefResponse)['object'][
        'sha'
    ]
    data = fetch_json(
        token, f'{repo}/contents/versions?ref={source_sha}', ContentResponse
    )
    if data.get('encoding') != 'base64':
        raise ValueError('Expected a base64 encoded versions file')
    content = base64.b64decode(data['content'], validate=False).decode()
    for line in content.splitlines():
        if (
            line.strip()
            and not line.lstrip().startswith('#')
            and not re.fullmatch(r'[A-Z][A-Z0-9_]*=[A-Za-z0-9.]+', line)
        ):
            raise ValueError('versions must contain plain KEY=VALUE entries')
    known = (
        {item['name'] for item in CATALOG}
        | {'DEVCONTAINERS_REF', 'VSCODE_COMMIT'}
        | UNPINNED.keys()
    )

    def checked(spec: Dependency) -> Inventory:
        try:
            if (
                spec['name'] in AGENT_LATEST
                and read_version(content, spec['name']) == 'latest'
            ):
                return {
                    'arg': spec['name'],
                    'current': 'latest',
                    'status': 'floating',
                    'reason': 'Agent uses latest at build time by policy; pod upgrades are independent',
                }
            return resolve(token, spec, content)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'arg': spec['name'], 'status': 'error', 'error': str(exc)}

    with ThreadPoolExecutor(max_workers=6) as pool:
        inventory: list[Inventory] = list(pool.map(checked, CATALOG))
    try:
        inventory.append(resolve_vscode(token, read_version(content, 'VSCODE_COMMIT')))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        inventory.append({'arg': 'VSCODE_COMMIT', 'status': 'error', 'error': str(exc)})
    try:
        inventory.append(
            resolve_devcontainers(token, read_version(content, 'DEVCONTAINERS_REF'))
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        inventory.append(
            {'arg': 'DEVCONTAINERS_REF', 'status': 'error', 'error': str(exc)}
        )
    for name, reason in UNPINNED.items():
        inventory.append(
            {
                'arg': name,
                'current': read_version(content, name),
                'status': 'floating'
                if name in AGENT_LATEST and read_version(content, name) == 'latest'
                else 'manual',
                'reason': reason,
            }
        )
    unknown = (
        set(re.findall(r'^([A-Z0-9_]+(?:VERSION|REF|COMMIT))=', content, re.MULTILINE))
        - known
    )
    for name in sorted(unknown):
        inventory.append(
            {'arg': name, 'status': 'manual', 'reason': 'No source mapping in catalog'}
        )
    changes = [row for row in inventory if row['status'] == 'update']
    updated = content
    for row in changes:
        updated = replace_version(updated, row['arg'], row['current'], row['version'])
    return {
        'owner': owner,
        'repository': repository,
        'source_sha': source_sha,
        'original_content': content,
        'updated_content': updated,
        'changes': changes,
        'inventory': inventory,
        'unmanaged_inputs': MUTABLE_INPUTS,
        'can_apply': not any(row['status'] == 'error' for row in inventory),
    }


def dependency_name(key: str) -> str:
    names = {
        'UV_VERSION': 'uv',
        'GO_VERSION': 'Go',
        'PHP_VERSION': 'PHP',
        'NODE_VERSION': 'Node.js',
        'GITHUB_CLI_VERSION': 'GitHub CLI',
        'DEVCONTAINERS_REF': 'devcontainers',
        'VSCODE_COMMIT': 'VS Code CLI',
        'S6_OVERLAY_VERSION': 's6-overlay',
        'DEVTOOLS_COSIGN_VERSION': 'Cosign',
        'DOCKER_BUILDX_VERSION': 'Docker Buildx',
        'DOCKER_COMPOSE_VERSION': 'Docker Compose',
        'ANTIGRAVITY_CLI_VERSION': 'Antigravity CLI',
        'CLAUDE_CODE_VERSION': 'Claude Code',
        'CODEX_VERSION': 'Codex',
    }
    return names.get(key, key.removesuffix('_VERSION').replace('_', ' ').title())


def update_scope(current: str, target: str) -> str:
    old, new = version(current), version(target)
    if old is None or new is None:
        return 'commit'
    if old[0] != new[0]:
        return 'major'
    return 'minor' if old[1] != new[1] else 'patch'


def format_title(changes: list[Change]) -> str:
    if len(changes) == 1:
        change = changes[0]
        return f'chore(deps): update {dependency_name(change["arg"])} to {change["version"]}'
    names = ', '.join(dependency_name(row['arg']) for row in changes[:3])
    suffix = ', and more' if len(changes) > 3 else ''
    return f'chore(deps): update {len(changes)} dependencies ({names}{suffix})'


def format_release_notes(change: Change) -> str:
    heading = escape(
        f'{dependency_name(change["arg"])}: {change["current"]} → {change["version"]}'
    )
    notes = change.get('notes', '')
    excerpt = (
        notes[:4000].replace('@', '@\u200b').replace('<', '&lt;').replace('>', '&gt;')
    )
    lines = [
        '<details>',
        f'<summary>{heading}</summary>',
        '',
        f'[Upstream source]({change["url"]})',
        '',
        excerpt or 'Release notes are not available from this source.',
    ]
    if change.get('notes_truncated') or len(notes) > 4000:
        lines += [
            '',
            '*Notes are truncated. See the upstream source for the full text.*',
        ]
    if not change.get('history_complete', True):
        lines += ['', '*The source lookup may not include every intermediate release.*']
    return '\n'.join([*lines, '', '</details>'])


def format_body(plan: Plan, run_url: str | None = None) -> str:
    changes = plan['changes']
    repository = f'{plan["owner"]}/{plan["repository"]}'
    base = plan['source_sha']
    lines = [
        f'Update **{len(changes)} pinned dependencies** in `versions`.',
        '',
        'Merging this PR triggers the image publish workflow on main. Existing pods are not restarted by this updater.',
        '',
        '| Dependency | Current | Proposed | Change | Source |',
        '| --- | --- | --- | --- | --- |',
    ]
    for change in changes:
        current, target = change['current'], change['version']
        display_current = current[:12] if len(current) == 40 else current
        display_target = target[:12] if len(target) == 40 else target
        lines.append(
            f'| {dependency_name(change["arg"])} | `{display_current}` | `{display_target}` '
            f'| {update_scope(current, target)} | [Upstream]({change["url"]}) |'
        )
    lines += [
        '',
        '### Review',
        '',
        '- [ ] Check upstream notes for compatibility changes and required configuration.',
        '- [ ] Confirm the proposed versions, then merge when ready.',
        '- [ ] Check the image publish result after merging.',
        '',
        'Version lookup succeeded. The proposed image has not been built or validated by this updater.',
        '',
        '### Release notes',
        '',
        'Upstream notes are reproduced without AI rewriting. Missing notes and lookup limits are marked below.',
        '',
    ]
    # Budget each disclosure so even a full inventory preserves closing tags and footer.
    budget = min(6000, 45000 // max(1, len(changes)))
    for change in changes:
        bounded = change.copy()
        notes = change.get('notes', '')
        bounded['notes'] = notes[: max(0, budget - 1500)]
        bounded['notes_truncated'] = change.get('notes_truncated', False) or len(
            bounded['notes']
        ) < len(notes)
        lines += [format_release_notes(bounded), '']
    lines += [
        '<details>',
        '<summary>Update policy and provenance</summary>',
        '',
        '- Only `versions` changes. Major upgrades and prereleases are excluded.',
        '- Runtime series remain pinned; devcontainers and VS Code CLI use fixed commits.',
        '- Agent entries set to `latest` stay unchanged and are reinstalled by their image build stages.',
        '- Source lookup failures prevent partial updates. This updater never merges or deploys.',
        '',
        'Inputs outside this update:',
        '',
        *(f'- {value}.' for value in plan['unmanaged_inputs']),
        '',
        f'Base: [`{base[:7]}`](https://github.com/{repository}/commit/{base})',
    ]
    if run_url:
        lines += ['', f'Run: [GitHub Actions]({run_url})']
    return '\n'.join([*lines, '', '</details>', ''])


def write_outputs(**values: str) -> None:
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        stream.write(''.join(f'{name}={value}\n' for name, value in values.items()))


def main() -> None:
    owner, repository = os.environ['GITHUB_REPOSITORY'].split('/', 1)
    plan = build_plan(os.environ['GH_TOKEN'], owner, repository)
    root = Path(__file__).resolve().parents[2]
    if not plan['can_apply']:
        errors = [row for row in plan['inventory'] if row['status'] == 'error']
        raise ValueError(f'Source lookup failed; versions was not modified: {errors}')
    if (root / 'versions').read_text() != plan['original_content']:
        raise ValueError('Checked-out versions differs from main; rerun the workflow')
    if not plan['changes']:
        write_outputs(changed='false')
        return
    run_id = os.environ.get('GITHUB_RUN_ID')
    run_url = (
        f'https://github.com/{owner}/{repository}/actions/runs/{run_id}'
        if run_id
        else None
    )
    body = format_body(plan, run_url)
    if len(body) > 60000:
        raise ValueError('PR body exceeds the output limit; versions was not modified')
    Path(os.environ['PR_BODY_PATH']).write_text(body)
    (root / 'versions').write_text(plan['updated_content'])
    write_outputs(changed='true', title=format_title(plan['changes']))
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(summary).open('a') as stream:
            stream.write(body)


if __name__ == '__main__':
    main()
