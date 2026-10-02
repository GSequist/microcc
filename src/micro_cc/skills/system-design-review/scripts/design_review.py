#!/usr/bin/env python3
"""Design review: declared contracts -> observed impact -> revision-bound evidence.

Python 3.12+, stdlib only. No inspected application modules are imported.
The gate enforces a review protocol, not the truth of human/LLM reasoning.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

from design_graph import digest, scan, changed_symbols, design_paths, in_roots

MODEL = 'design/architecture.toml'
STATE = '.design'
VERSION = 1
QUESTIONS = {
    'task-lifetime': 'Who owns every task and descendant? What bounds count/lifetime? Walk cancellation, exceptions, shutdown and a stuck child.',
    'cancellation': 'At each changed await boundary, what state is already committed? Who cleans up if cancellation arrives here?',
    'concurrency': 'Enumerate writers by task/thread/process/host; identify the actual critical section and test a conflicting schedule.',
    'persistence': 'Walk crash-before/after each write/ack. Identify atomicity, durability, replay/idempotency and stale-writer handling.',
    'process-lifecycle': 'What survives process death, and who notices or resumes unfinished work? What if two runs overlap?',
    'deployment': 'Which identity, topology and storage assumptions change across local, GUI, headless and ephemeral deployment?',
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def git(repo, *args, check=True):
    proc = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=False)
    if check and proc.returncode:
        raise ValueError(proc.stderr.decode(errors='replace').strip())
    return proc


def safe_path(repo, rel):
    p = Path(rel)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError(f'Expected repository-relative path, got {rel!r}')
    result = (repo / p).resolve()
    if not result.is_relative_to(repo.resolve()):
        raise ValueError(f'Path escapes repository: {rel}')
    return result


def load_model(text):
    model = tomllib.loads(text)
    if model.get('version') != VERSION:
        raise ValueError('architecture.toml must declare version = 1')
    allowed = {'version', 'source_roots', 'contract', 'resource', 'link', 'scenario'}
    if set(model) - allowed:
        raise ValueError(f'Unknown model fields: {set(model) - allowed}')
    roots = model.get('source_roots')
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r and not Path(r).is_absolute() and '..' not in Path(r).parts for r in roots):
        raise ValueError('source_roots must be a nonempty list of repository-relative directories')
    schemas = {
        'contract': ({'id', 'statement', 'anchors', 'assumptions', 'questions', 'scenarios'}, {'id', 'statement', 'anchors', 'assumptions', 'questions'}),
        'resource': ({'id', 'kind', 'lifetime', 'description'}, {'id', 'kind', 'lifetime', 'description'}),
        'link': ({'from', 'to', 'kind', 'why'}, {'from', 'to', 'kind', 'why'}),
        'scenario': ({'id', 'command', 'timeout', 'scope', 'inputs'}, {'id', 'command', 'timeout', 'scope', 'inputs'}),
    }
    for section, (fields, required) in schemas.items():
        rows = model.setdefault(section, [])
        if not isinstance(rows, list):
            raise ValueError(f'{section} must use [[{section}]] tables')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - fields or required - set(row):
                raise ValueError(f'Invalid {section} fields: {row!r}')
            if 'id' in row:
                if not isinstance(row['id'], str) or not row['id'] or row['id'] in seen:
                    raise ValueError(f'Duplicate/empty {section} id: {row.get("id")}')
                seen.add(row['id'])
            for key, value in row.items():
                if key in {'anchors', 'assumptions', 'questions', 'scenarios', 'command', 'inputs'}:
                    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                        raise ValueError(f'{section}.{key} must contain nonempty strings')
                    if key != 'scenarios' and not value:
                        raise ValueError(f'{section}.{key} must not be empty')
                elif key == 'timeout':
                    if type(value) not in (int, float) or not 0 < value <= 300:
                        raise ValueError('scenario timeout must be between 0 and 300 seconds')
                elif not isinstance(value, str) or not value.strip():
                    raise ValueError(f'{section}.{key} must be a nonempty string')
    if not model['contract']:
        raise ValueError('An empty contract model cannot pass a design review')
    scenario_ids = {s['id'] for s in model['scenario']}
    for c in model['contract']:
        c.setdefault('scenarios', [])
        if set(c['scenarios']) - scenario_ids:
            raise ValueError(f'Unknown scenario in {c["id"]}')
    return model


def worktree(repo):
    files = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').stdout.split(b'\0')
    content = {}
    for raw in sorted(set(files)):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        if rel == STATE or rel.startswith(STATE + '/'):
            continue
        path = safe_path(repo, rel)
        if path.is_file():
            content[rel] = path.read_bytes()
    # Include ignored local policy too; never let gitignore make it invisible.
    path = safe_path(repo, MODEL)
    if path.is_file():
        content[MODEL] = path.read_bytes()
    return content


def base_files(repo, commit, paths):
    out = {}
    for path in sorted(paths):
        proc = git(repo, 'show', f'{commit}:{path}', check=False)
        if proc.returncode == 0:
            out[path] = proc.stdout
    return out


def tool_hash():
    root = Path(__file__).parent
    return digest(encoded({p.name: digest(p.read_bytes()) for p in sorted(root.glob('*.py'))}))


def make_plan(repo, base='HEAD', focus=(), audit=False):
    commit = git(repo, 'rev-parse', '--verify', f'{base}^{{commit}}').stdout.decode().strip()
    content = worktree(repo)
    if MODEL not in content:
        raise ValueError(f'No {MODEL}; use the skill to declare the first contracts')
    model = load_model(content[MODEL].decode())
    for scenario in model['scenario']:
        for rel in scenario['inputs']:
            path = safe_path(repo, rel)
            if not path.is_file():
                raise ValueError(f'{scenario["id"]}: missing evidence input {rel}')
            content[rel] = path.read_bytes()
    roots = model['source_roots']
    base_names = {os.fsdecode(p) for p in git(repo, 'ls-tree', '-r', '--name-only', '-z', commit).stdout.split(b'\0') if p}
    # Non-Python source is hashed and requires review, but is not statically analyzed.
    review_suffixes = {'.py', '.js', '.jsx', '.ts', '.tsx', '.sh', '.toml', '.yaml', '.yml', '.json', '.tf', '.hcl', '.sql'}
    selected = {p for p in content.keys() | base_names
                if in_roots(p, roots) or Path(p).suffix in review_suffixes} | {MODEL}
    selected.update(rel for s in model['scenario'] for rel in s['inputs'])
    old_content = base_files(repo, commit, selected)
    git_changed = {os.fsdecode(p) for p in git(repo, 'diff', '--name-only', '-z', commit).stdout.split(b'\0') if p}
    changed_files = sorted(p for p in selected if old_content.get(p) != content.get(p) or p in git_changed)
    before = scan({p: b.decode(errors='replace') for p, b in old_content.items()}, roots)
    after = scan({p: b.decode(errors='replace') for p, b in content.items() if in_roots(p, roots)}, roots)
    changed = changed_symbols(before, after, changed_files)
    seeds = set(changed)
    diagnostics = list(after['diagnostics'])
    nodes = after['nodes']
    resources = {f'resource:{r["id"]}' for r in model['resource']}
    valid = set(nodes) | resources
    edges = before['edges'] + after['edges']
    for link in model['link']:
        for endpoint in ('from', 'to'):
            if link[endpoint] not in valid:
                diagnostics.append(f'Declared link endpoint missing: {link[endpoint]}')
        edges.append({**link, 'basis': 'declared'})
    for c in model['contract']:
        for anchor in c['anchors']:
            if anchor not in valid:
                diagnostics.append(f'{c["id"]}: missing anchor {anchor}')
    for item in focus:
        if item in valid or item in before['nodes']:
            seeds.add(item)
        else:
            matched = {s for s in nodes.keys() | before['nodes'].keys() if s.split('::')[0] == item}
            if not matched:
                raise ValueError(f'Focus does not match a file, symbol or resource: {item}')
            seeds.update(matched)
    boundaries = {a for c in model['contract'] for a in c['anchors']}
    boundaries.update(link['to'] for link in model['link'])
    paths = design_paths(seeds, edges, boundaries)
    model_changed = MODEL in changed_files
    affected = []
    scenarios_by_id = {s['id']: s for s in model['scenario']}
    for c in model['contract']:
        reached = [a for a in c['anchors'] if a in paths]
        evidence_changed = any(p in changed_files for sid in c['scenarios'] for p in scenarios_by_id[sid]['inputs'])
        if audit or model_changed or reached or evidence_changed:
            affected.append({**c, 'paths': {a: paths[a] for a in reached},
                             'trigger': 'audit' if audit else 'model changed' if model_changed else 'scenario input changed' if evidence_changed else 'dependency impact'})
    obligations = []
    for c in affected:
        obligations.append({'id': 'contract:' + c['id'], 'kind': 'contract', 'subject': c['id'],
                            'questions': c['questions'], 'scenarios': c['scenarios']})
    # Batch review by file to avoid hundreds of checkbox-sized answers. Every
    # risky/unmapped symbol remains explicit in the batch, including new callers.
    change_groups = {}
    for sid in sorted(seeds):
        meta = nodes.get(sid, before['nodes'].get(sid))
        if not meta:
            continue
        risks = sorted(set(meta['risks']) | set(before['nodes'].get(sid, {}).get('risks', [])))
        reached = design_paths([sid], edges, boundaries)
        covered = any(a in reached for c in model['contract'] for a in c['anchors'])
        if risks or not covered:
            group = change_groups.setdefault(meta['file'], {'symbols': [], 'risks': set(), 'unmapped': []})
            group['symbols'].append(sid)
            group['risks'].update(risks)
            if not covered:
                group['unmapped'].append(sid)
    for path, group in sorted(change_groups.items()):
        risks = sorted(group['risks'])
        questions = [QUESTIONS[r] for r in risks]
        if group['unmapped']:
            questions.append('Unmapped symbols are listed in this obligation. Trace their consumers; add missing contracts/links or explain why no system property is affected.')
        obligations.append({'id': 'change:' + path, 'kind': 'changed-risk' if risks else 'unmapped-change',
                            'subject': path, 'symbols': group['symbols'], 'unmapped': group['unmapped'],
                            'risks': risks, 'questions': questions, 'scenarios': []})
    for path in changed_files:
        if path != MODEL and (not path.endswith('.py') or not in_roots(path, roots)):
            obligations.append({'id': 'opaque:' + path, 'kind': 'outside-analysis', 'subject': path,
                                'questions': ['This code/configuration/evidence input is outside the configured Python analysis. Trace its consumers, runtime effects and failure boundaries manually; expand source_roots if appropriate.'], 'scenarios': []})
    if model_changed:
        old_model = old_content.get(MODEL, b'').decode(errors='replace')
        obligations.append({'id': 'model-change', 'kind': 'policy-change', 'subject': MODEL,
                            'questions': ['Review the declaration diff, especially removed/weakened guarantees, assumptions, links, source roots and scenarios. Never change the contract merely to match a failing implementation.'], 'scenarios': [],
                            'before': old_model, 'after': content[MODEL].decode()})
    plan = {
        'version': VERSION, 'repo': str(repo.resolve()), 'base': commit, 'focus': sorted(set(focus)), 'audit': audit,
        'tool_hash': tool_hash(), 'runtime': {'python': sys.executable, 'version': sys.version, 'platform': sys.platform},
        'file_modes': {p: (repo / p).stat().st_mode & 0o777 for p in sorted(content)},
        'snapshot': digest(encoded({p: digest(b) for p, b in sorted(content.items())})),
        'model': model, 'changed_files': changed_files, 'changed_symbols': changed,
        'contracts': affected, 'obligations': obligations, 'diagnostics': sorted(set(diagnostics)),
        'coverage': {'symbols': len(nodes), 'static_edges': len(after['edges']), 'declared_links': len(model['link']),
                     'unresolved_calls': len(after['unresolved']),
                     'limits': ['Python AST candidates, not type/control-flow analysis. Import aliases are scope-insensitive.',
                                'Dynamic dispatch, callbacks, monkeypatching, subprocesses, external systems and other languages need declared links/review.',
                                'No complete shared-state or resource-ownership inference. Passing scenarios cover only their declared schedules.']},
        'unresolved_in_impact': [u for u in after['unresolved'] if u['from'] in paths],
    }
    plan['id'] = digest(encoded(plan))
    return plan


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def render(plan):
    lines = ['# Design impact plan', '', f'Plan: `{plan["id"][:16]}`',
             f'Base: `{plan["base"][:12]}`', '',
             f'{len(plan["changed_symbols"])} changed symbols; {len(plan["contracts"])} affected contracts; {len(plan["obligations"])} review obligations.',
             '', '**This is an impact hypothesis and evidence request, not a correctness verdict.**', '', '## Diagnostics']
    lines += ['- ' + d for d in plan['diagnostics']] or ['None.']
    lines += ['', '## Affected contracts']
    for c in plan['contracts']:
        lines += ['', f'### {c["id"]}', c['statement'], '', 'Assumptions:']
        lines += ['- ' + a for a in c['assumptions']]
        for anchor, chain in c['paths'].items():
            if not chain:
                lines.append(f'- Direct change/focus: `{anchor}`')
            else:
                lines.append(f'- Impact reaches `{anchor}`:')
                for edge in chain:
                    if edge.get('direction') == 'dependency-use':
                        lines.append(f'  - Changed usage: `{edge["from"]}` → {edge["kind"]} ({edge["basis"]}) → `{edge["to"]}`')
                    else:
                        lines.append(f'  - `{edge["to"]}` ← {edge["kind"]} ({edge["basis"]}) ← `{edge["from"]}`')
        if not c['paths']:
            lines.append('- Selected by ' + c['trigger'])
        lines += ['Evidence scenarios: ' + (', '.join(c['scenarios']) or '**NONE — manual review only, not tested**')]
    scenario_ids = {s for c in plan['contracts'] for s in c['scenarios']}
    lines += ['', '## Commands proposed (not executed by plan)']
    for s in plan['model']['scenario']:
        if s['id'] in scenario_ids:
            lines += [f'- `{s["id"]}`: `{json.dumps(s["command"])}`', f'  - Scope: {s["scope"]}; timeout: {s["timeout"]}s']
    lines += ['', '## Review obligations']
    for o in plan['obligations']:
        lines += ['', f'### `{o["id"]}` ({o["kind"]})']
        lines += ['- ' + q for q in o['questions']]
        if o.get('symbols'):
            lines.append('- Symbols in this review: ' + ', '.join(f'`{s.split("::", 1)[1]}`' for s in o['symbols']))
        if o.get('unmapped'):
            lines.append('- No declared contract path: ' + ', '.join(f'`{s.split("::", 1)[1]}`' for s in o['unmapped']))
    lines += ['', '## Coverage limits'] + ['- ' + s for s in plan['coverage']['limits']]
    lines += [f'- {plan["coverage"]["unresolved_calls"]} unresolved/external call sites in analyzed source. Impact-relevant sites are retained in plan.json.',
              '', 'Next: inspect changes and paths; run verify --allow-exec; fill review.template.json as review.json; run gate.']
    return '\n'.join(lines) + '\n'


def read_json(path):
    with path.open() as f:
        return json.load(f)


def current_plan(repo):
    stored = read_json(repo / STATE / 'plan.json')
    current = make_plan(repo, stored['base'], stored['focus'], stored['audit'])
    if current['id'] != stored.get('id'):
        raise ValueError('STALE plan: code, declarations, scenarios or tooling changed. Run plan and verify again.')
    return current


def declared_content(repo, model):
    content = worktree(repo)
    for scenario in model['scenario']:
        for rel in scenario['inputs']:
            path = safe_path(repo, rel)
            content[rel] = path.read_bytes()
    return content


@contextlib.contextmanager
def evidence_snapshot(repo, plan):
    """Copy planned inputs, not secrets/ignored runtime state, to a sealed tree.

    User immutable flags where supported prevent ordinary write-and-restore;
    a same-user hostile command can clear flags/chmod, so this is NOT a sandbox.
    """
    content = declared_content(repo, plan['model'])
    if digest(encoded({p: digest(b) for p, b in sorted(content.items())})) != plan['snapshot']:
        raise ValueError('STALE inputs while preparing evidence snapshot')
    with tempfile.TemporaryDirectory(prefix='design-evidence-') as temp:
        root = Path(temp) / 'repo'
        root.mkdir()
        paths = []
        flagged = []
        for rel, data in content.items():
            target = safe_path(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            # Preserve executability without granting write permission.
            executable = plan['file_modes'][rel] & 0o111
            target.chmod(0o555 if executable else 0o444)
            paths.append(target)
        directories = sorted([root, *[p for p in root.rglob('*') if p.is_dir()]], key=lambda p: len(p.parts), reverse=True)
        for directory in directories:
            directory.chmod(0o555)
        if hasattr(os, 'chflags'):
            import stat
            for path in [*paths, *directories]:
                try:
                    os.chflags(path, stat.UF_IMMUTABLE)
                    flagged.append(path)
                except OSError:
                    pass
        try:
            yield root, 'read-only copy; user-immutable flags where supported'
            actual = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            if actual != content:
                raise ValueError('Evidence snapshot changed during scenario; refusing certification')
        finally:
            for path in reversed(flagged):
                try:
                    os.chflags(path, 0)
                except OSError:
                    pass
            for directory in reversed(directories):
                directory.chmod(0o755)
            for path in paths:
                if path.exists():
                    path.chmod(0o644)


def run_scenario(repo, scenario):
    command = [arg.replace('{python}', sys.executable).replace('{repo}', str(repo)) for arg in scenario['command']]
    started = time.monotonic()
    # Commands are trusted repo code, not sandboxed. Separate process group allows bounded cleanup.
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(command, cwd=repo, stdout=output, stderr=subprocess.STDOUT,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                    start_new_session=(os.name == 'posix'))
        except OSError as exc:
            return {'status': 'error', 'output': str(exc), 'returncode': None, 'command': command}
        timed_out = False
        try:
            proc.wait(timeout=scenario['timeout'])
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if os.name == 'posix':
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.kill()
            proc.wait()
        output.seek(0)
        text = output.read(64000).decode(errors='replace')
    status = 'timeout' if timed_out else 'pass' if proc.returncode == 0 else 'fail' if proc.returncode == 1 else 'error'
    return {'status': status, 'returncode': proc.returncode, 'output': text,
            'seconds': round(time.monotonic() - started, 3), 'command': command}


def verify(repo, allow_exec):
    plan = current_plan(repo)
    if plan['diagnostics']:
        raise ValueError('Model/parse diagnostics block verification: ' + '; '.join(plan['diagnostics']))
    ids = {s for c in plan['contracts'] for s in c['scenarios']}
    if ids and not allow_exec:
        raise ValueError('Review scenario commands in plan.md, then use verify --allow-exec. Commands are not sandboxed.')
    evidence = {'plan_id': plan['id'], 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'results': {}}
    for s in plan['model']['scenario']:
        if s['id'] in ids:
            # Fresh copy for each scenario: no previous test's writes/state leak.
            with evidence_snapshot(repo, plan) as (snapshot, protection):
                result = run_scenario(snapshot, s)
            result['scope'] = s['scope']
            result['input_mode'] = protection
            evidence['results'][s['id']] = result
            print(f'{result["status"].upper():7} {s["id"]}')
            for line in result['output'].strip().splitlines():
                print('    ' + line)
            current_plan(repo)
    # Catch concurrent changes even when no scenarios are selected.
    current_plan(repo)
    atomic_json(repo / STATE / 'evidence.json', evidence)
    return 1 if any(r['status'] != 'pass' for r in evidence['results'].values()) else 0


def gate(repo):
    plan = current_plan(repo)
    blockers = list(plan['diagnostics'])
    needed = {s for c in plan['contracts'] for s in c['scenarios']}
    evidence_path = repo / STATE / 'evidence.json'
    evidence = read_json(evidence_path) if evidence_path.exists() else {}
    if evidence.get('plan_id') != plan['id']:
        blockers.append('Missing/stale evidence. Run verify (even if no scenarios are selected).')
    for sid in sorted(needed):
        result = evidence.get('results', {}).get(sid, {})
        if result.get('status') != 'pass' or result.get('returncode') != 0:
            blockers.append(f'Scenario {sid}: {result.get("status", "not run")} — prose cannot waive failed or absent evidence')
    review_path = repo / STATE / 'review.json'
    review = read_json(review_path) if review_path.exists() else {}
    if review.get('plan_id') != plan['id']:
        blockers.append('Missing/stale review.json; start from review.template.json')
    entries = review.get('obligations', {})
    for o in plan['obligations']:
        entry = entries.get(o['id'], {})
        if entry.get('decision') != 'reviewed':
            blockers.append(f'Unreviewed: {o["id"]}')
            continue
        for field in ('reasoning', 'counterexample', 'evidence', 'residual_risk'):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                blockers.append(f'{o["id"]}: missing {field}')
    if blockers:
        print('BLOCKED — design review incomplete or evidence failed')
        print('\n'.join('- ' + b for b in blockers))
        return 1
    print(f'REVIEWED {plan["id"][:16]} — {len(needed)} scenarios passed; {len(plan["obligations"])} obligations recorded.')
    print('This clears the review protocol, not a claim of system correctness. Read residual risks in review.json.')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo-root', type=Path, default=Path.cwd())
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan', help='Read-only analysis; write .design artifacts, never change declarations')
    p.add_argument('--base', default='HEAD')
    p.add_argument('--focus', action='append', default=[], help='Additional proposed-change file, file::symbol or resource:id; does not hide actual changes')
    p.add_argument('--all', action='store_true', dest='audit', help='Audit all declared contracts')
    p = sub.add_parser('verify', help='Execute proposed scenario commands with explicit consent')
    p.add_argument('--allow-exec', action='store_true')
    sub.add_parser('gate', help='Fail unless evidence and review match the current plan/code')
    sub.add_parser('hook', help='Stop hook: report a blocking decision; never execute scenarios')
    args = ap.parse_args(argv)
    repo = args.repo_root.resolve()
    try:
        if args.command == 'plan':
            plan = make_plan(repo, args.base, args.focus, args.audit)
            state = repo / STATE
            atomic_json(state / 'plan.json', plan)
            state.joinpath('plan.md').write_text(render(plan))
            template = {'plan_id': plan['id'], 'obligations': {
                o['id']: {'decision': 'pending', 'reasoning': '', 'counterexample': '', 'evidence': '', 'residual_risk': ''}
                for o in plan['obligations']}}
            atomic_json(state / 'review.template.json', template)
            print(render(plan))
            return 1 if plan['diagnostics'] else 0
        if args.command == 'verify':
            return verify(repo, args.allow_exec)
        if args.command == 'gate':
            return gate(repo)
        # A hook does not autonomously fix or approve anything. Avoid recursive Stop loops.
        event = json.load(sys.stdin)
        if event.get('stop_hook_active'):
            print('{}')
            return 0
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                code = gate(repo)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                code = 1
                print(str(exc))
        print(json.dumps({'decision': 'block', 'reason': output.getvalue() + '\nRead the design-review skill; do not claim completion with unresolved design evidence.'} if code else {}))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Design review error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
