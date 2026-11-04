"""Bounded read-only evidence from the deployed three-layer knowledge workspace."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'Audit path escapes its workspace: {relative}')
    return path


def read_evidence(root: Path, relative: str, limit: int = 12000) -> dict:
    path = safe_path(root, relative)
    if not path.exists():
        return {'path': relative, 'missing': True}
    if not path.is_file():
        raise ValueError(f'Audit evidence is not a regular file: {relative}')
    # Hash the full file, but do not load unbounded files into model input.
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        head = stream.read(limit)
        sha.update(head)
        while chunk := stream.read(65536):
            sha.update(chunk)
    return {'path': relative, 'sha256': sha.hexdigest(), 'text': head.decode('utf-8', errors='replace'),
            'bytes': path.stat().st_size, 'truncated': path.stat().st_size > limit}


def inventory(root: Path, relative: str, limit: int = 300) -> dict:
    start = safe_path(root, relative)
    paths = []
    for directory, dirs, files in os.walk(start, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__'))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() not in ('.md', '.txt', '.pdf', '.docx') or path.is_symlink():
                continue
            paths.append(str(path.relative_to(root)))
            if len(paths) >= limit:
                return {'paths': paths, 'truncated': True}
    return {'paths': paths, 'truncated': False}


def workspace_snapshot(home: Path, workspace: Path, settings, window) -> dict:
    doctrine = [read_evidence(workspace, name) for name in settings.doctrine_files]
    if any(item.get('missing') or item.get('truncated') for item in doctrine):
        raise ValueError('Audit doctrine is missing or too large; provide complete doctrine_files')
    native = [read_evidence(home, name) for name in ('memories/MEMORY.md', 'memories/USER.md')]
    wiki = inventory(workspace, settings.wiki_path)
    library = inventory(workspace, settings.library_path)
    transcript = json.dumps(window.rows, ensure_ascii=False)
    terms = set(re.findall(r'\w{4,}', transcript.lower()))
    ranked = sorted(wiki['paths'], key=lambda p: (-sum(t in p.lower() for t in terms), p))
    selected = list(dict.fromkeys([p for p in wiki['paths'] if Path(p).name.lower() in ('index.md', 'aprendizajes.md')] + ranked[:3]))
    pages = [read_evidence(workspace, path, 6000) for path in selected[:5]]
    # Source pointers in selected pages are inspected, never executed as instructions.
    pointers = []
    for page in pages:
        for link in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', page.get('text', '')):
            if '://' in link or link.startswith('#'):
                continue
            target = (Path(page['path']).parent / link).as_posix()
            try:
                path = safe_path(workspace, target)
                pointers.append({'from': page['path'], 'target': target, 'exists': path.is_file()})
            except ValueError:
                pointers.append({'from': page['path'], 'target': target, 'outside_workspace': True})
    sources = []
    for pointer in pointers:
        if pointer.get('exists') and Path(pointer['target']).suffix.lower() in ('.md', '.txt'):
            sources.append(pointer['target'])
    sources += [p for p in library['paths'] if Path(p).name.lower() == 'index.md']
    sources += sorted((p for p in library['paths'] if Path(p).suffix.lower() in ('.md', '.txt')
                       and any(t in p.lower() for t in terms)), key=lambda p: (-sum(t in p.lower() for t in terms), p))[:3]
    source_files = [read_evidence(workspace, path, 6000) for path in dict.fromkeys(sources)][:5]
    ledger = read_evidence(home, 'skills/.curator_ledger.jsonl', 64000)
    journal = read_evidence(workspace, settings.journal_path, 16000)
    # Logs are chronological: prefer the recent tail, explicitly marking gaps.
    for item, root in ((ledger, home), (journal, workspace)):
        if item.get('truncated'):
            with safe_path(root, item['path']).open('rb') as stream:
                stream.seek(max(0, item['bytes'] - 16000))
                item['text'] = stream.read(16000).decode('utf-8', errors='replace')
            item['tail_only'] = True
    timestamps = [row['timestamp'] for row in window.rows if row.get('timestamp') is not None]
    entries = []
    for line in ledger.get('text', '').splitlines():
        try:
            entry = json.loads(line)
            stamp = datetime.fromisoformat(entry['ts']).timestamp()
        except (ValueError, TypeError, KeyError):
            ledger['partial_or_invalid_lines'] = True
            continue
        if not timestamps or min(timestamps) <= stamp <= max(timestamps):
            entries.append(entry)
    ledger['text'] = '\n'.join(json.dumps(entry, ensure_ascii=False) for entry in entries)
    touched = {str(entry.get('skill', '')) for entry in entries}
    skills = inventory(home, 'skills', 300)
    skill_paths = [p for p in skills['paths'] if Path(p).name == 'SKILL.md']
    skill_paths.sort(key=lambda p: (not any(name and name in p for name in touched),
                                   -sum(t in p.lower() for t in terms), p))
    skill_files = [read_evidence(home, path, 10000) for path in skill_paths[:5]]
    return {'doctrine': doctrine, 'native_memory': native, 'wiki': wiki, 'library': library,
            'wiki_pages': pages, 'source_files': source_files, 'source_pointers': pointers, 'skills': skill_files,
            'skill_inventory': skills, 'skill_ledger': ledger, 'journal': journal}


def evidence_fingerprints(snapshot: dict) -> list[dict]:
    result = []
    for key in ('doctrine', 'wiki_pages', 'source_files', 'native_memory', 'skills'):
        for item in snapshot[key]:
            result.append({'root': 'home' if key in ('native_memory', 'skills') else 'workspace',
                           'path': item['path'], 'sha256': item.get('sha256')})
    return result


def evidence_unchanged(fingerprints: list[dict], home: Path, workspace: Path) -> bool:
    for item in fingerprints:
        path = safe_path(home if item['root'] == 'home' else workspace, item['path'])
        current = digest(path) if path.is_file() else None
        if current != item['sha256']:
            return False
    return True
