import sys
from types import SimpleNamespace
import pytest
from flow_agent import startup

class Key:
    def __enter__(self): return self
    def __exit__(self, *args): pass

def test_startup_uses_current_user_and_hidden_quoted_exe(monkeypatch):
    values = {}
    def query(key, name):
        if name not in values: raise FileNotFoundError
        return values[name], 1
    def remove(key, name):
        if name not in values: raise FileNotFoundError
        del values[name]
    reg = SimpleNamespace(HKEY_CURRENT_USER='user', KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda *a: Key(), CreateKeyEx=lambda *a: Key(), QueryValueEx=query,
        SetValueEx=lambda key,name,res,kind,value: values.update({name:value}), DeleteValue=remove)
    monkeypatch.setitem(sys.modules, 'winreg', reg)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', r'E:\Apps with spaces\FlowAgentConsole.exe')
    assert startup.status()['enabled'] is False
    assert startup.configure(True)['enabled'] is True
    assert values[startup.VALUE_NAME].endswith('" --no-browser')
    assert values[startup.VALUE_NAME].startswith('"')
    assert startup.configure(False)['enabled'] is False
    assert startup.configure(False)['enabled'] is False

def test_source_mode_cannot_register_python_as_startup(monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    assert not startup.status()['supported']
    with pytest.raises(RuntimeError): startup.configure(True)
