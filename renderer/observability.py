"""Renderer-only JSON diagnostics. No Flask/DB/model dependencies."""
from datetime import datetime, timezone
import json
import time
from uuid import UUID

IDS = ('request_id', 'origin_request_id', 'project_id', 'page_id', 'task_id', 'item_id', 'snapshot_id')


def job_event(manifest, started, *, outcome, error_code=None):
    context = manifest.get('trace') if isinstance(manifest, dict) else None
    context = context if isinstance(context, dict) else {}
    payload = {'event': 'scene.renderer_job', 'time': datetime.now(timezone.utc).isoformat(),
               'duration_seconds': max(0, time.monotonic() - started),
               'outcome': 'succeeded' if outcome == 'succeeded' else 'failed'}
    for name in IDS:
        try:
            value = context.get(name)
            payload[name] = str(UUID(value)) if isinstance(value, str) else None
        except (ValueError, TypeError, AttributeError):
            payload[name] = None
    kind = manifest.get('kind') if isinstance(manifest, dict) else None
    payload['kind'] = kind if kind in ('pdf', 'pptx', 'pptx_pdf', 'text_layout', 'html_preview') else 'unknown'
    if error_code:
        payload['error_code'] = error_code if error_code in ('RENDER_FAILED', 'RENDER_ISOLATION_FAILED') else 'RENDER_FAILED'
    try:
        print(json.dumps(payload, sort_keys=True, separators=(',', ':')), flush=True)
    except Exception:
        pass
