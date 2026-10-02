"""Static evidence for design review. Stdlib only; never imports inspected code.

Edges are dependencies: A -> B means a change to B may affect A. Direct call
resolution is deliberately limited. Missing dynamic edges are NOT proof of
independence. Explicit contract links cover relationships Python cannot express.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from collections import defaultdict, deque
from pathlib import PurePosixPath


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


class Semantic(ast.NodeTransformer):
    """Ignore docstrings, but not executable string literals or annotations."""
    def visit(self, node):
        node = super().visit(node)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                first = node.body[0].value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    node.body = node.body[1:]
        return node


def fingerprint(node):
    return digest(ast.dump(Semantic().visit(copy.deepcopy(node)), include_attributes=False))


def own_walk(node):
    """Walk a scope, not the bodies of nested scopes."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from own_walk(child)


def module_name(path, roots):
    p = PurePosixPath(path)
    for root in sorted(roots, key=len, reverse=True):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return str(p.with_suffix("")).replace("/", ".")


def in_roots(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") or r == "." for r in roots)


def imported_names(tree, module, is_package):
    names = {}
    # Scope-insensitive import aliases are candidates, not a type inference engine.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = module.split(".") if is_package else module.split(".")[:-1]
                prefix = package[:len(package) - node.level + 1]
                base = ".".join(prefix + ([base] if base else []))
            for alias in node.names:
                if alias.name != "*":
                    names[alias.asname or alias.name] = f"{base}.{alias.name}".strip(".")
    return names


def risks_for(name, node):
    tail = name.split(".")[-1]
    risks = set()
    if tail in {"create_task", "ensure_future", "TaskGroup", "all_tasks", "cancel", "gather", "wait_for", "timeout"}:
        risks.add("task-lifetime")
    if name == "asyncio.run":
        risks.add("task-lifetime")
    if tail in {"Lock", "RLock", "Semaphore", "flock", "acquire", "release"}:
        risks.add("concurrency")
    if tail in {"write_text", "write_bytes", "replace", "unlink", "execute", "executemany", "commit", "rollback"}:
        risks.add("persistence")
    if tail in {"Popen", "create_subprocess_exec", "create_subprocess_shell", "fork", "kill"}:
        risks.add("process-lifecycle")
    if tail in {"getenv"} or name.startswith("os.environ."):
        risks.add("deployment")
    if tail == "open":
        modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
        modes += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
        if any(isinstance(m, str) and any(c in m for c in "wa+") for m in modes):
            risks.add("persistence")
    return risks


def scan(sources: dict[str, str], roots: list[str]):
    nodes, edges, diagnostics, unresolved = {}, [], [], []
    scopes, module_paths, trees = {}, {}, {}
    for path, source in sorted(sources.items()):
        if not path.endswith(".py") or not in_roots(path, roots):
            continue
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            diagnostics.append(f"Cannot parse {path}:{exc.lineno}: {exc.msg}")
            continue
        mod = module_name(path, roots)
        if mod in module_paths:
            diagnostics.append(f"Ambiguous module {mod}: {module_paths[mod]} and {path}")
        module_paths[mod] = path
        trees[path] = (tree, mod, imported_names(tree, mod, path.endswith('/__init__.py')))

        def collect(body, prefix=""):
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    qual = f"{prefix}.{n.name}".strip(".")
                    # A property's getter/setter legitimately share the Python name.
                    # Keep both bodies in the index, rather than silently dropping one.
                    decorators = [dotted(d) for d in getattr(n, 'decorator_list', [])]
                    accessor = next((d.rsplit('.', 1)[-1] for d in decorators if d.endswith(('.setter', '.deleter'))), None)
                    if accessor:
                        qual += '@' + accessor
                    sid = f"{path}::{qual}"
                    if sid in nodes:
                        diagnostics.append(f"Duplicate/conditional definition: {sid}")
                    nodes[sid] = {"file": path, "symbol": qual, "line": n.lineno,
                                  "hash": fingerprint(n), "risks": [], "calls": []}
                    scopes[sid] = n
                    collect(n.body, qual)
                elif isinstance(n, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
                    collect(n.body, prefix)
                    collect(getattr(n, 'orelse', []), prefix)
                    collect(getattr(n, 'finalbody', []), prefix)
                    for handler in getattr(n, 'handlers', []):
                        collect(handler.body, prefix)
        collect(tree.body)
        shell = ast.Module(body=[n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[])
        sid = f"{path}::<module>"
        nodes[sid] = {"file": path, "symbol": "<module>", "line": 1,
                      "hash": fingerprint(shell), "risks": [], "calls": []}
        scopes[sid] = shell

    module_globals = {}
    for path, (tree, mod, _) in trees.items():
        names = set()
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                names.update(n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name))
        module_globals[path] = names

    global_names = {}
    for sid, node in nodes.items():
        if node['symbol'] != '<module>':
            global_names[f"{trees[node['file']][1]}.{node['symbol']}"] = sid

    def resolve(raw, sid):
        meta = nodes[sid]
        path, qual = meta['file'], meta['symbol']
        _, mod, imports = trees[path]
        parts = raw.split('.')
        # Local lexical defs take precedence over imports.
        lexical = qual.split('.') if qual != '<module>' else []
        for size in range(len(lexical), -1, -1):
            candidate = f"{path}::" + '.'.join(lexical[:size] + [raw])
            if candidate in nodes:
                return candidate, raw
        if parts[0] in {'self', 'cls'}:
            for size in range(len(lexical), 0, -1):
                owner = f"{path}::" + '.'.join(lexical[:size])
                if isinstance(scopes.get(owner), ast.ClassDef):
                    candidate = f"{owner}." + '.'.join(parts[1:])
                    if candidate in nodes:
                        return candidate, raw
        expanded = '.'.join([imports.get(parts[0], parts[0]), *parts[1:]])
        return global_names.get(expanded), expanded

    for sid, scope in scopes.items():
        meta = nodes[sid]
        risks = set()
        state_refs = set()
        for n in own_walk(scope):
            # Conservatively connect references to module globals/imported data.
            # This is not alias-sensitive read/write analysis; the edge states
            # only that a scope references data whose declaration may change.
            if isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Load):
                raw_ref = dotted(n)
                root_name = raw_ref.split('.')[0]
                path = meta['file']
                if root_name in module_globals[path] and not sid.endswith('::<module>'):
                    state_refs.add(path + '::<module>')
                imports = trees[path][2]
                if root_name in imports:
                    expanded_ref = '.'.join([imports[root_name], *raw_ref.split('.')[1:]])
                    for mod, imported_path in module_paths.items():
                        if expanded_ref.startswith(mod + '.') and expanded_ref not in global_names:
                            state_refs.add(imported_path + '::<module>')
            if isinstance(n, ast.Await):
                risks.add('cancellation')
            if not isinstance(n, ast.Call):
                continue
            raw = dotted(n.func)
            target, expanded = resolve(raw, sid) if raw else (None, '<dynamic>')
            risks.update(risks_for(expanded, n))
            meta['calls'].append({"name": expanded, "line": n.lineno})
            if target:
                edges.append({"from": sid, "to": target, "kind": "calls", "basis": "static-candidate"})
            elif expanded not in {'print', 'len', 'str', 'int', 'bool', 'dict', 'list', 'set', 'tuple', 'range', 'isinstance', 'getattr', 'enumerate', 'zip', 'min', 'max', 'sum', 'any', 'all', 'sorted', 'super'}:
                unresolved.append({"from": sid, "call": expanded, "line": n.lineno})
            if expanded.split('.')[-1] in {'create_task', 'ensure_future', 'start_soon'} and n.args:
                arg = n.args[0]
                spawned = dotted(arg.func if isinstance(arg, ast.Call) else arg)
                child, _ = resolve(spawned, sid) if spawned else (None, '')
                if child:
                    edges.append({"from": sid, "to": child, "kind": "spawns", "basis": "static-candidate"})
        for target in sorted(state_refs - {sid}):
            edges.append({'from': sid, 'to': target, 'kind': 'references-state', 'basis': 'static-candidate'})
        meta['risks'] = sorted(risks)
    return {"nodes": nodes, "edges": edges, "diagnostics": diagnostics, "unresolved": unresolved}


def changed_symbols(before, after, changed_files):
    old, new = before['nodes'], after['nodes']
    changed = {sid for sid in old.keys() | new.keys()
               if old.get(sid, {}).get('hash') != new.get(sid, {}).get('hash')}
    # Module globals, imports and decorators may alter all consumers in that file.
    modules = {sid.split('::')[0] for sid in changed if sid.endswith('::<module>')}
    changed.update(sid for sid in old.keys() | new.keys() if sid.split('::')[0] in modules)
    return sorted(changed)


def impact_paths(seeds, edges):
    """Reverse dependency closure; shortest explanation for every reached node."""
    reverse = defaultdict(list)
    for edge in edges:
        reverse[edge['to']].append(edge)
    paths = {sid: [] for sid in seeds}
    queue = deque(sorted(seeds))
    while queue:
        node = queue.popleft()
        for edge in sorted(reverse[node], key=lambda e: (e['from'], e['kind'])):
            if edge['from'] not in paths:
                paths[edge['from']] = paths[node] + [{**edge, 'direction': 'dependent'}]
                queue.append(edge['from'])
    return paths


def design_paths(seeds, edges, boundaries):
    """Also review changed USE of a contract, not just changed implementation.

A new caller of an unchanged store changes writer topology. Walk observed calls
forward to known contract/resource boundaries, then notify their dependents.
Forward paths are explicitly labelled usage, not implementation changes.
"""
    paths = impact_paths(seeds, edges)
    forward = defaultdict(list)
    for edge in edges:
        if edge['kind'] in {'calls', 'spawns', 'references-state'} and edge.get('basis') == 'static-candidate':
            forward[edge['from']].append(edge)
    use = {sid: [] for sid in seeds}
    queue = deque(sorted(seeds))
    while queue:
        node = queue.popleft()
        for edge in sorted(forward[node], key=lambda e: (e['to'], e['kind'])):
            if edge['to'] not in use:
                use[edge['to']] = use[node] + [{**edge, 'direction': 'dependency-use'}]
                queue.append(edge['to'])
    for boundary in sorted(set(use) & set(boundaries)):
        for consumer, suffix in impact_paths([boundary], edges).items():
            if consumer not in paths:
                paths[consumer] = use[boundary] + suffix
    return paths
