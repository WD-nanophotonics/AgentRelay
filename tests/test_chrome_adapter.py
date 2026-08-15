from pathlib import Path

import pytest

from dummy_orchestrator.adapters import ChatGPTWebAuditorAdapter, ChromeResolver, HumanRequired


class _Locator:
    def __init__(self, visible=False): self.visible = visible
    def count(self): return 1 if self.visible else 0
    def nth(self, _): return self
    def is_visible(self): return self.visible


class _Page:
    def __init__(self, title="Auditor", challenge=False): self._title, self.challenge = title, challenge
    def title(self): return self._title
    def get_by_text(self, label, exact=True): return _Locator(self.challenge and label == "Verify you are human")


def test_cdp_endpoint_is_loopback_and_challenge_is_precise(tmp_path: Path):
    adapter = ChatGPTWebAuditorAdapter(tmp_path, 5, debug_port=9333)
    assert adapter.endpoint == "http://127.0.0.1:9333"
    assert ChatGPTWebAuditorAdapter._is_challenge(_Page()) is False
    assert ChatGPTWebAuditorAdapter._is_challenge(_Page(challenge=True)) is True


def test_cdp_unavailable_is_human_required(tmp_path: Path):
    adapter = ChatGPTWebAuditorAdapter(tmp_path, 1, debug_port=1)
    with pytest.raises(HumanRequired, match="chrome_cdp"):
        adapter._cdp_json()


def test_chrome_resolver_rejects_missing_configured_path(tmp_path: Path):
    with pytest.raises(HumanRequired, match="chrome_executable"):
        ChromeResolver(str(tmp_path / "missing-chrome.exe")).preflight()


def test_browser_launch_lock_is_singleton(tmp_path: Path):
    adapter = ChatGPTWebAuditorAdapter(tmp_path / "profile", 1)
    lock, fd = adapter._acquire_launch_lock()
    try:
        with pytest.raises(HumanRequired, match="browser_launch_lock"):
            adapter._acquire_launch_lock()
    finally:
        adapter._release_launch_lock(lock, fd)
