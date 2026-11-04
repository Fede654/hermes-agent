"""Publish idempotent local audit reports and account for auxiliary usage."""
from datetime import datetime, UTC
import json
import os
from pathlib import Path

from agent.memory_audit_context import safe_path


def record_usage(home: Path, session_id: str, report: dict):
    accounting = report.get('accounting') or {}
    usage, route = accounting.get('usage') or {}, accounting.get('route') or {}
    # Unknown cancelled usage stays explicitly unknown in the report, not a claim of zero cost.
    if not accounting:
        return
    from hermes_state import SessionDB
    db = SessionDB(home / 'state.db')
    try:
        details = usage.get('prompt_tokens_details') or {}
        db.record_auxiliary_usage(
            session_id, 'memory_audit', model=route.get('model'), billing_provider=route.get('provider'),
            input_tokens=usage.get('prompt_tokens', 0), output_tokens=usage.get('completion_tokens', 0),
            cache_read_tokens=details.get('cached_tokens', 0), api_call_count=1,
        )
    finally:
        db.close()


def publish_report(store, workspace: Path, settings, run_id: str, report: dict):
    from utils import atomic_write_text
    atomic_write_text(store.root / f'{run_id}.json', json.dumps(report, ensure_ascii=False, indent=2))
    journal = safe_path(workspace, settings.journal_path)
    marker = f'<!-- memory-audit:{run_id} -->'
    existing = journal.read_text(encoding="utf-8-sig") if journal.exists() else ''
    if marker in existing:
        return
    day = datetime.now(UTC).date().isoformat()
    lines = [f'\n## {day} — auditoría de memoria', marker,
             f"Sesión: {report['session_id']}; ventana: {report['watermark']}..{report['through_id']}; modo: {report.get('mode', settings.mode)}.",
             f"Estado: {report['status']}; mensajes omitidos por el límite: {report.get('omitted_messages', 0)}."]
    plan = report.get('plan') or {}
    lines.append(str(plan.get('summary') or report.get('error') or 'Sin correcciones.'))
    for gap in report.get('coverage_gaps', []):
        lines.append('- Cobertura limitada: ' + json.dumps(gap, ensure_ascii=False))
    for finding in plan.get('findings', []):
        lines.append(f"- [{finding['layer']}/{finding['kind']}] {finding['summary']} (mensajes {finding['evidence_ids']})")
    for operation in plan.get('operations', []):
        lines.append('- Propuesta: ' + json.dumps(operation, ensure_ascii=False))
    if report.get('receipt'):
        lines.append('Escrituras realizadas: ' + json.dumps(report['receipt'], ensure_ascii=False))
    if report.get('usage_unknown'):
        lines.append('Uso/coste del intento cancelado: desconocido; no se interpreta como coste cero.')
    data = ('\n'.join(lines) + '\n').encode('utf-8')
    journal.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise OSError('Incomplete audit journal append; durable JSON report retained')
        os.fsync(fd)
    finally:
        os.close(fd)
