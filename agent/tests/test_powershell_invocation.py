"""Transparent PowerShell arguments; real quoting checks run on Windows CI."""
import sys

import pytest

from cmdb_agent import discovery
from cmdb_agent.collectors import windows
from cmdb_agent.collectors.common import CommandError


SCRIPT = '''@{name='Zażółć gęślą'; path='C:\\Program Files\\CMDB'; quote='a"b'; value='$HOME & |'} | ConvertTo-Json -Compress'''
EXPECTED = {'name': 'Zażółć gęślą', 'path': 'C:\\Program Files\\CMDB', 'quote': 'a"b', 'value': '$HOME & |'}


@pytest.mark.parametrize('target', ['collector', 'discovery'])
def test_script_is_one_readable_argument(monkeypatch, target):
    import json

    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(EXPECTED, ensure_ascii=False)

    monkeypatch.setattr(windows, 'powershell_executable', lambda: 'trusted-powershell.exe')
    if target == 'collector':
        monkeypatch.setattr(windows, 'run_command', command)
        result = windows.run_powershell(SCRIPT, timeout=420)
        assert calls[0][1] == {'timeout': 420}
    else:
        monkeypatch.setattr(discovery, '_command', command)
        result = discovery._powershell(SCRIPT)
    args = calls[0][0]
    assert result == EXPECTED
    assert args[0] == 'trusted-powershell.exe'
    assert args[-2] == '-Command'
    assert args[-1].endswith(SCRIPT)
    assert '-EncodedCommand' not in args
    assert '-ExecutionPolicy' not in args
    assert 'Bypass' not in args
    assert '-NonInteractive' in args
    assert '-NoProfile' in args


def test_collector_invalid_json_is_reported(monkeypatch):
    monkeypatch.setattr(windows, 'run_command', lambda *a, **kw: 'not JSON')
    with pytest.raises(CommandError, match='spoza JSON'):
        windows.run_powershell(SCRIPT)


def test_collector_failure_is_not_retried_with_bypass(monkeypatch):
    calls = []
    def blocked(*args, **kwargs):
        calls.append(args)
        raise CommandError('blocked')
    monkeypatch.setattr(windows, 'run_command', blocked)
    with pytest.raises(CommandError, match='blocked'):
        windows.run_powershell(SCRIPT)
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires Windows PowerShell 5.1')
@pytest.mark.parametrize('target', ['collector', 'discovery'])
def test_real_windows_unicode_and_quoting(target):
    run = windows.run_powershell if target == 'collector' else discovery._powershell
    assert run(SCRIPT) == EXPECTED
