"""CLI entry points for the local knowledge-audit service."""
import json
import re


def audit_command(args):
    from agent.memory_audit_store import AuditStore
    from agent.memory_audit_runner import tick
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    command = args.audit_command or 'status'
    if command == 'status':
        result = AuditStore(home).status()
    elif command == 'show':
        if not re.fullmatch(r'[0-9a-f]{32}', args.run_id):
            raise ValueError('Invalid audit run id')
        result = AuditStore(home).report(args.run_id)
    else:
        scope_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            result = tick(home, load_config_readonly(), session_id=getattr(args, 'session_id', None))
        finally:
            reset_secret_scope(scope_token)
    print(json.dumps(result, ensure_ascii=False, indent=2))
