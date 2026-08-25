# -*- coding: utf-8 -*-
"""一生 staged->live 生效(fail-closed: SAFE_APPLY_PERSONA 默认 OFF)。"""
import pathlib

import pytest

from anima.webui.life_apply import (
    get_safe_apply, set_safe_apply, apply_persona, rollback_live,
)


OLD = 'system_prompt: |\n  旧人格'
NEW = 'system_prompt: |\n  新人格'


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / 'project'
    cfg = root / 'config'
    cfg.mkdir(parents=True)
    (cfg / 'persona_core.yaml').write_text(OLD, encoding='utf-8')
    data = tmp_path / 'data'
    staged = data / 'life_restore' / 'restored'
    staged.mkdir(parents=True)
    (staged / 'persona_v2.bak').write_text(NEW, encoding='utf-8')
    return {'root': root, 'data': data}


def test_safe_apply_off_by_default(env):
    assert get_safe_apply(env['data']) is False


def test_apply_denied_when_off_even_owner(env):
    out = apply_persona(env['data'], env['root'], 'persona_v2.bak', confirm=True, owner_ok=True)
    assert out['code'] == 403
    assert (env['root'] / 'config' / 'persona_core.yaml').read_text(encoding='utf-8') == OLD


def test_apply_denied_non_owner(env):
    set_safe_apply(env['data'], True, confirm=True, owner_ok=True)
    out = apply_persona(env['data'], env['root'], 'persona_v2.bak', confirm=True, owner_ok=False)
    assert out['code'] == 403


def test_apply_requires_confirm(env):
    set_safe_apply(env['data'], True, confirm=True, owner_ok=True)
    out = apply_persona(env['data'], env['root'], 'persona_v2.bak', confirm=False, owner_ok=True)
    assert out['code'] == 400


def test_apply_rejects_non_persona_name(env):
    set_safe_apply(env['data'], True, confirm=True, owner_ok=True)
    out = apply_persona(env['data'], env['root'], 'identity_x.json', confirm=True, owner_ok=True)
    assert out['code'] == 400


def test_apply_success_backup_and_replaced(env):
    set_safe_apply(env['data'], True, confirm=True, owner_ok=True)
    out = apply_persona(env['data'], env['root'], 'persona_v2.bak', confirm=True, owner_ok=True)
    assert out['ok'] is True
    assert (env['root'] / 'config' / 'persona_core.yaml').read_text(encoding='utf-8') == NEW
    backup = pathlib.Path(out['backup'])
    assert backup.is_file() and backup.read_text(encoding='utf-8') == OLD


def test_apply_corrupt_yaml_rejected_target_untouched(env):
    set_safe_apply(env['data'], True, confirm=True, owner_ok=True)
    bad = env['data'] / 'life_restore' / 'restored' / 'persona_bad.bak'
    bad.write_text(': not: : yaml [', encoding='utf-8')
    out = apply_persona(env['data'], env['root'], 'persona_bad.bak', confirm=True, owner_ok=True)
    assert out['code'] == 400
    assert (env['root'] / 'config' / 'persona_core.yaml').read_text(encoding='utf-8') == OLD


def test_rollback_live_restores_previous(env):
    set_safe_apply(env['data'], True, confirm=True, owner_ok=True)
    apply_persona(env['data'], env['root'], 'persona_v2.bak', confirm=True, owner_ok=True)
    assert (env['root'] / 'config' / 'persona_core.yaml').read_text(encoding='utf-8') == NEW
    rb = rollback_live(env['data'], env['root'], confirm=True, owner_ok=True)
    assert rb['ok'] is True
    assert (env['root'] / 'config' / 'persona_core.yaml').read_text(encoding='utf-8') == OLD
