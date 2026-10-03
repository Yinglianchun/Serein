import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


def helper(name):
    spec = importlib.util.spec_from_file_location('test_' + name, Path(__file__).parents[1] / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance(tmp_path, kind='docker'):
    manager = helper('manage')
    manager.DEPLOY = tmp_path / 'instance with spaces' / 'deploy'
    manager.DEPLOY.mkdir(parents=True)
    (manager.DEPLOY / 'secrets').mkdir()
    for name, value in {
        'config.toml': 'synthetic = true',
        '.env': 'SEREIN_BIND=127.0.0.1\nSEREIN_PORT=19217\n',
        'installation.json': json.dumps({'backend': kind, 'memory_port': 19218, 'preview_port': 19219}),
        'secrets/api-token': 'PRIVATE-TOKEN',
        'secrets/web-auth.json': '{"username":"admin","salt":"PRIVATE-SALT","hash":"PRIVATE-HASH"}',
    }.items():
        (manager.DEPLOY / name).write_text(value, encoding='utf-8')
    return manager


def test_menu_diagnostics_bypasses_dependency_gate_environment_and_lock(tmp_path, monkeypatch):
    manager = helper('manage')
    manager.DEPLOY = tmp_path / 'uninstalled'
    calls = []
    choices = iter(['12', 'q'])
    monkeypatch.setattr(manager, 'choose', lambda *a, **kw: next(choices))
    monkeypatch.setattr(manager, 'doctor', lambda: calls.append('doctor'))
    monkeypatch.setattr(manager, 'pause', lambda: None)
    def forbidden(*a):
        raise AssertionError('diagnostics must not require or prepare dependencies')
    monkeypatch.setattr(manager, 'select_environment', forbidden)
    monkeypatch.setattr(manager, 'ensure_tools', forbidden)
    manager.main()
    assert calls == ['doctor']
    assert not manager.DEPLOY.exists()


@pytest.mark.parametrize('format', ['array', 'jsonl'])
def test_docker_diagnostics_scoped_read_only_and_no_private_output(tmp_path, monkeypatch, capsys, format):
    doctor = helper('doctor')
    manager = instance(tmp_path)
    before = {p: p.read_bytes() for p in manager.DEPLOY.rglob('*') if p.is_file()}
    calls = []
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: name)
    monkeypatch.setattr(manager, 'run', lambda args, **kw: SimpleNamespace(stdout='linux' if args[1] == 'info' else 'version'))
    def compose(*args, **kw):
        calls.append((args, kw))
        rows = [{'Service': s, 'State': 'running', 'Health': 'healthy'} for s in ('memory', 'gateway')]
        text = (json.dumps(rows) if format == 'array' else '\n'.join(map(json.dumps, rows))) if args[0] == 'ps' else '401 Bearer PRIVATE-TOKEN private chat text\n429 api_key=SECRET\n'
        return SimpleNamespace(stdout=text)
    monkeypatch.setattr(manager, 'compose', compose)
    probes = []
    monkeypatch.setattr(doctor, 'health', lambda *args: probes.append(args) or True)
    monkeypatch.setattr(manager, 'check_memory', lambda: {'ready': True})
    result = doctor.diagnose(manager)
    assert result == {'issues': 0, 'warnings': 4}
    assert probes == [(19217, '/ready')]
    assert all(args[0] in ('ps', 'logs') and kw['timeout'] == 15 for args, kw in calls)
    output = capsys.readouterr().out
    assert '认证失败' in output and '限流' in output
    assert all(value not in output for value in ('PRIVATE-TOKEN', 'PRIVATE-SALT', 'PRIVATE-HASH', 'private chat text', 'SECRET'))
    assert before == {p: p.read_bytes() for p in manager.DEPLOY.rglob('*') if p.is_file()}


def test_missing_docker_still_checks_health_and_summarizes(tmp_path, monkeypatch, capsys):
    doctor = helper('doctor')
    manager = instance(tmp_path)
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: None)
    def unavailable(*args):
        raise OSError('PRIVATE failure')
    monkeypatch.setattr(doctor, 'health', unavailable)
    monkeypatch.setattr(manager, 'compose', lambda *a, **kw: pytest.fail('Docker unavailable'))
    result = doctor.diagnose(manager)
    output = capsys.readouterr().out
    assert result['issues'] == 2
    assert '排查结果' in output and '本机健康检查失败' in output
    assert 'PRIVATE failure' not in output


def test_timeout_and_invalid_compose_json_do_not_abort_other_checks(tmp_path, monkeypatch, capsys):
    doctor = helper('doctor')
    manager = instance(tmp_path)
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: name)
    monkeypatch.setattr(manager, 'run', lambda *a, **kw: SimpleNamespace(stdout='linux'))
    def compose(*a, **kw):
        raise subprocess.TimeoutExpired('secret-command', 15, output='SECRET')
    monkeypatch.setattr(manager, 'compose', compose)
    monkeypatch.setattr(doctor, 'health', lambda *a: True)
    assert doctor.diagnose(manager)['issues'] == 1
    monkeypatch.setattr(manager, 'compose', lambda *a, **kw: SimpleNamespace(stdout='SECRET invalid JSON'))
    assert doctor.diagnose(manager)['issues'] == 1
    output = capsys.readouterr().out
    assert '排查结果' in output and 'SECRET' not in output and 'secret-command' not in output


def test_local_services_ports_logs_and_policy_failure(tmp_path, monkeypatch, capsys):
    doctor = helper('doctor')
    manager = instance(tmp_path, 'local')
    python = manager.local_python()
    python.parent.mkdir(parents=True)
    python.touch()
    logs = manager.DEPLOY / 'runtime/logs'
    logs.mkdir(parents=True)
    (logs / 'memory.log').write_text('timeout PRIVATE CHAT', encoding='utf-8')
    (logs / 'gateway.log').write_text('address already in use PASSWORD', encoding='utf-8')
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: name)
    monkeypatch.setattr(manager, 'run', lambda args, **kw: SimpleNamespace(stdout='22.12.0' if args[0] == 'node' else 'ok'))
    monkeypatch.setattr(doctor, 'load', lambda *a: SimpleNamespace(control=lambda *args: {'running': True}))
    probes = []
    monkeypatch.setattr(doctor, 'health', lambda *a: probes.append(a) or True)
    monkeypatch.setattr(manager, 'check_memory', lambda: {'ready': False})
    result = doctor.diagnose(manager)
    assert result == {'issues': 0, 'warnings': 3}
    assert probes == [(19217, '/ready'), (19218, '/health')]
    output = capsys.readouterr().out
    assert 'PRIVATE CHAT' not in output and 'PASSWORD' not in output


def test_fresh_install_and_broken_record_continue_without_writing(tmp_path, monkeypatch, capsys):
    doctor = helper('doctor')
    manager = helper('manage')
    manager.DEPLOY = tmp_path / 'missing'
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: None)
    assert doctor.diagnose(manager)['issues'] >= 5
    assert not manager.DEPLOY.exists()
    manager.DEPLOY.mkdir()
    (manager.DEPLOY / 'installation.json').write_text('[]')
    assert doctor.diagnose(manager)['issues'] >= 6
    assert '排查结果' in capsys.readouterr().out


def test_health_ignores_proxies_and_redirects(monkeypatch):
    doctor = helper('doctor')
    handlers = []
    requests = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def opener(*args):
        handlers.extend(args)
        return SimpleNamespace(open=lambda url, **kw: requests.append((url, kw)) or Response())
    monkeypatch.setattr(doctor, 'build_opener', opener)
    assert doctor.health(19217, '/ready')
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, None, None, None, None) is None
    assert requests == [('http://127.0.0.1:19217/ready', {'timeout': 5})]
