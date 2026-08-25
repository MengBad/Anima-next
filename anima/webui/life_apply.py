'''一生快照 staged->live 生效 —— Persona 唯一闸(SAFE_APPLY_PERSONA 默认 OFF)。

fail-closed：开关 OFF 时即使 Owner 也 403；仅在 Owner + confirm + 开关 ON 时
允许把 persona 类快照原子生效到 config/persona_core.yaml，改前备份、写后回读校验失败自动回滚。
开关持久化在 data/life_restore/settings.json（运行数据，非 config.yaml）。
'''
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import yaml


def _settings_path(data_dir) -> Path:
    return Path(data_dir or 'data') / 'life_restore' / 'settings.json'


def get_safe_apply(data_dir) -> bool:
    try:
        return bool(json.loads(_settings_path(data_dir).read_text(encoding='utf-8')).get('safe_apply_persona'))
    except Exception:
        return False


def set_safe_apply(data_dir, enabled, confirm, owner_ok) -> dict[str, Any]:
    if not owner_ok:
        return {'ok': False, 'code': 403, 'error': '需要 Owner 授权'}
    if not confirm:
        return {'ok': False, 'code': 400, 'error': '缺少 confirm'}
    p = _settings_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({'safe_apply_persona': bool(enabled)}), encoding='utf-8')
    return {'ok': True, 'safe_apply_persona': bool(enabled)}


def _personaish(name: str) -> bool:
    n = (name or '').strip()
    return (n.startswith('persona_') and n.endswith('.bak')) or n == 'persona_core.yaml'


def apply_persona(data_dir, project_root, name, confirm, owner_ok) -> dict[str, Any]:
    if not owner_ok:
        return {'ok': False, 'code': 403, 'error': '需要 Owner 授权'}
    if not get_safe_apply(data_dir):
        return {'ok': False, 'code': 403, 'error': 'SAFE_APPLY_PERSONA 关闭，此操作被拒绝（fail-closed）'}
    if not confirm:
        return {'ok': False, 'code': 400, 'error': '缺少 confirm'}
    n = (name or '').strip()
    if not n or '/' in n or chr(92) in n or '..' in n or not _personaish(n):
        return {'ok': False, 'code': 400, 'error': '非法或非人格类快照名'}
    staged = Path(data_dir or 'data') / 'life_restore' / 'restored' / n
    if not staged.is_file():
        return {'ok': False, 'code': 404, 'error': '暂存区无该快照'}
    data = staged.read_bytes()
    if not data:
        return {'ok': False, 'code': 400, 'error': '快照为空'}
    try:
        doc = yaml.safe_load(data.decode('utf-8'))
    except Exception as exc:
        return {'ok': False, 'code': 400, 'error': '快照非合法 YAML: ' + str(exc)[:80]}
    if not isinstance(doc, dict) or not doc.get('system_prompt'):
        return {'ok': False, 'code': 400, 'error': '快照缺少 persona system_prompt 头，拒绝生效'}
    target = Path(project_root) / 'config' / 'persona_core.yaml'
    if not target.is_file():
        return {'ok': False, 'code': 404, 'error': 'live persona_core.yaml 不存在'}
    ts = time.strftime('%Y%m%d-%H%M%S')
    bdir = Path(data_dir) / 'life_restore' / 'backups' / 'live' / ts
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / 'persona_core.yaml'
    backup.write_bytes(target.read_bytes())
    tmp = target.with_name('.persona_core.tmp-' + ts)
    tmp.write_bytes(data)
    os.replace(tmp, target)
    if target.read_bytes() != data:
        target.write_bytes(backup.read_bytes())
        return {'ok': False, 'code': 500, 'error': '写后回读不一致，已自动回滚到备份'}
    return {'ok': True, 'target': str(target), 'backup': str(backup), 'name': n}


def rollback_live(data_dir, project_root, confirm, owner_ok) -> dict[str, Any]:
    if not owner_ok:
        return {'ok': False, 'code': 403, 'error': '需要 Owner 授权'}
    if not confirm:
        return {'ok': False, 'code': 400, 'error': '缺少 confirm'}
    base = Path(data_dir) / 'life_restore' / 'backups' / 'live'
    dirs = sorted((base.glob('*')), key=lambda p: p.name) if base.is_dir() else []
    if not dirs:
        return {'ok': False, 'code': 404, 'error': '没有 live 备份可回滚'}
    backup = dirs[-1] / 'persona_core.yaml'
    if not backup.is_file():
        return {'ok': False, 'code': 404, 'error': '最近备份缺少 persona_core.yaml'}
    target = Path(project_root) / 'config' / 'persona_core.yaml'
    ts = time.strftime('%Y%m%d-%H%M%S')
    tmp = target.with_name('.persona_core.tmp-rb-' + ts)
    tmp.write_bytes(backup.read_bytes())
    os.replace(tmp, target)
    return {'ok': True, 'target': str(target), 'from_backup': str(backup)}
