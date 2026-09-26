import socket
import threading
import time
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from src.target_app.app import app


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornTestServer(uvicorn.Server):
    def install_signal_handlers(self):
        pass


@pytest.fixture(scope="module")
def live_server():
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = UvicornTestServer(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server startup
    base_url = f"http://127.0.0.1:{port}"
    start_time = time.time()
    while time.time() - start_time < 5.0:
        if server.started:
            break
        time.sleep(0.05)

    yield base_url
    server.should_exit = True
    thread.join(timeout=2.0)


def test_playwright_member_search_happy_path(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. Navigate to member search
        page.goto(f"{live_server}/members")
        assert page.is_visible("text=Member Registry & Servicing Query")

        # 2. Fill search input for Eleanor Vance (12345)
        search_input = page.get_by_role("textbox", name="Member Search ID")
        search_input.fill("12345")

        # 3. Click search button
        page.get_by_role("button", name="Search Records").click()

        # 4. Verify arrival at detail page and checkpoint
        page.wait_for_selector("text=Account Summary — Member #12345")
        assert "Eleanor Vance" in page.locator("#member-name-val").inner_text()

        # 5. Extract savings balance
        savings_balance = page.locator("#savings-balance-val").inner_text().strip()
        assert savings_balance == "$4,250.75"

        browser.close()


def test_playwright_member_search_not_found(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. Navigate to member search
        page.goto(f"{live_server}/members")

        # 2. Fill search input for non-existent member (99999)
        search_input = page.get_by_role("textbox", name="Member Search ID")
        search_input.fill("99999")

        # 3. Click search button
        page.get_by_role("button", name="Search Records").click()

        # 4. Verify legitimate business outcome banner is rendered
        warning_banner = page.locator("#not-found-alert")
        assert warning_banner.is_visible()
        assert "Member record not found in system (ID: 99999)" in warning_banner.inner_text()

        browser.close()


def test_playwright_member_locked_account_escalation(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. Navigate to locked member
        page.goto(f"{live_server}/members/67890")

        # 2. Verify security lockout alert is displayed
        lock_alert = page.locator("#account-locked-alert")
        assert lock_alert.is_visible()
        assert "Security Lockout:" in lock_alert.inner_text()
        assert page.locator("#supervisor-override-btn").is_visible()

        # 3. Human supervisor clearance override
        page.locator("#supervisor-override-btn").click()

        # 4. Account is now cleared
        assert not page.locator("#account-locked-alert").is_visible()
        assert "active" in page.locator("#status-badge").inner_text().lower()

        browser.close()
