"""The desktop window owns or reuses exactly one server for its library."""
from __future__ import annotations

import socket
import sys

import httpx
import pytest

from book_library import desktop
from book_library.demo import create_demo
from book_library.onboarding import port_in_use


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class FakeWebview:
    """Records the window and exercises the live server while 'open'."""

    def __init__(self):
        self.windows, self.start_options, self.statuses = [], [], []

    def create_window(self, **options):
        self.windows.append(options)

    def start(self, **options):
        self.start_options.append(options)
        url = self.windows[-1]['url']
        self.statuses.append(httpx.get(url + '/api/status', timeout=10).json())
        assert httpx.get(url + '/', timeout=10).status_code == 200


@pytest.fixture
def lib(tmp_path):
    return create_demo(tmp_path / 'demo')


def test_window_owns_a_server_and_stops_it_on_close(lib):
    port = free_port()
    webview = FakeWebview()
    result = desktop.run_app(lib, port, webview=webview)
    assert result == {'address': f'http://127.0.0.1:{port}', 'server': 'owned', 'root': str(lib.root)}
    assert webview.windows[0]['url'] == f'http://127.0.0.1:{port}'
    assert webview.windows[0]['title'] == 'The Reading Room'
    assert webview.start_options == [{'private_mode': False}]
    assert webview.statuses[0]['root'] == str(lib.root)
    assert not port_in_use('127.0.0.1', port)
    # Citation links follow the address the window actually uses.
    assert lib.cite(lib.search('copper birds', engine='exact')['direct'][0]['id'])['links']['local_citation'].startswith(f'http://127.0.0.1:{port}/')


def test_window_reuses_a_running_server_for_the_same_library_and_leaves_it_running(lib):
    port = free_port()
    running = desktop._default_server(lib, '127.0.0.1', port)
    running.start()
    try:
        def must_not_start(*args):
            raise AssertionError('A second server was started.')
        result = desktop.run_app(lib, port, webview=FakeWebview(), server_factory=must_not_start)
        assert result['server'] == 'reused'
        assert port_in_use('127.0.0.1', port)
    finally:
        running.stop()


def test_window_refuses_a_server_for_a_different_library(lib, tmp_path):
    other = create_demo(tmp_path / 'other')
    port = free_port()
    running = desktop._default_server(other, '127.0.0.1', port)
    running.start()
    try:
        with pytest.raises(ValueError, match='different library'):
            desktop.run_app(lib, port, webview=FakeWebview())
    finally:
        running.stop()


def test_window_refuses_an_unrelated_program_on_the_port(lib, monkeypatch):
    monkeypatch.setattr(desktop, 'PROBE_TIMEOUT_SECONDS', 0.5)  # this listener never answers
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match='Another program'):
            desktop.run_app(lib, port, webview=FakeWebview())


def test_missing_pywebview_is_a_clear_setup_error(lib, monkeypatch):
    monkeypatch.setitem(sys.modules, 'webview', None)
    with pytest.raises(ValueError, match='bin/setup'):
        desktop.run_app(lib, free_port())
