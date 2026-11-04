"""Validated settings for opt-in, profile-scoped knowledge auditing."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditSettings:
    enabled: bool = False
    mode: str = 'dry_run'
    idle_hours: float = 6
    window_days: float = 2
    max_messages: int = 120
    overlap: int = 10
    max_writes: int = 8
    max_input_tokens: int = 64000
    timeout: float = 600
    provider: str = ''
    model: str = ''
    reasoning_effort: str = 'high'
    workspace: str = ''
    doctrine_files: tuple = ('AGENTS.md', 'doctrina/memoria.md', 'doctrina/conocimiento.md', 'SOUL.md')
    wiki_path: str = 'wiki'
    library_path: str = 'biblioteca'
    journal_path: str = 'bitacora.md'
    platforms: tuple = ('telegram',)

    @classmethod
    def from_config(cls, config: dict) -> 'AuditSettings':
        block = dict(config.get('audit') or {})
        unknown = block.keys() - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f'Unknown audit settings: {sorted(unknown)}')
        for key in ('doctrine_files', 'platforms'):
            if key in block:
                if not isinstance(block[key], (list, tuple)) or not all(isinstance(x, str) for x in block[key]):
                    raise ValueError(f'audit.{key} must be a list of strings')
                block[key] = tuple(block[key])
        settings = cls(**block)
        if type(settings.enabled) is not bool or settings.mode not in ('dry_run', 'apply'):
            raise ValueError('audit.enabled must be boolean; mode must be dry_run or apply')
        for key in ('max_messages', 'max_writes', 'max_input_tokens', 'overlap'):
            value = getattr(settings, key)
            if type(value) is not int or value < (0 if key == 'overlap' else 1):
                raise ValueError(f'audit.{key} must be a positive integer (overlap may be zero)')
        import math
        for key in ('idle_hours', 'window_days', 'timeout'):
            value = getattr(settings, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'audit.{key} must be positive and finite')
        if settings.max_writes > 32 or settings.max_input_tokens > 100000 or settings.timeout > 3600:
            raise ValueError('Audit limits exceed safety caps (32 writes, 100000 input tokens, 3600 seconds)')
        if settings.reasoning_effort not in ('medium', 'high'):
            raise ValueError('audit.reasoning_effort must be medium or high')
        return settings

    def workdir(self, config: dict) -> Path:
        value = self.workspace or (config.get('terminal') or {}).get('cwd')
        if not value:
            raise ValueError('Set audit.workspace or terminal.cwd to the deployed knowledge workspace')
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError('Audit workspace must be a directory')
        return path
