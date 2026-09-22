from __future__ import annotations

import sys
import types

from client.network import windows_system_proxy


def test_windows_system_proxy_reads_simple_wininet_value(monkeypatch) -> None:
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda *_: Key(),
        QueryValueEx=lambda _key, name: (
            1 if name == "ProxyEnable" else "127.0.0.1:7890",
            None,
        ),
    )
    monkeypatch.setattr("client.network.os.name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert windows_system_proxy() == "http://127.0.0.1:7890"
