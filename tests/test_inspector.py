import pytest
from playwright.async_api import async_playwright

from src.agent.inspector import DOMInspector
from src.models.artifact import LocatorStrategy


@pytest.mark.asyncio
async def test_inspector_button_role_and_text():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html>
                <body>
                    <button id="search-btn" class="btn btn-primary">Search Records</button>
                </body>
            </html>
        """)

        inspector = DOMInspector()
        strategy = await inspector.inspect_element(page, "#search-btn")

        assert isinstance(strategy, LocatorStrategy)
        assert strategy.primary == "role:button[name='Search Records']"
        assert "#search-btn" in strategy.fallbacks
        assert "text:Search Records" in strategy.fallbacks or any("Search Records" in fb for fb in strategy.fallbacks)
        assert strategy.frame_selector is None
        assert "Search Records" in (strategy.rationale or "")

        await browser.close()


@pytest.mark.asyncio
async def test_inspector_input_textbox_with_label():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html>
                <body>
                    <form>
                        <label for="member_id">Member Search ID</label>
                        <input id="member_id" name="member_id" type="text" placeholder="Enter Member ID" />
                    </form>
                </body>
            </html>
        """)

        inspector = DOMInspector()
        strategy = await inspector.inspect_element(page, "input#member_id")

        assert "role:textbox[name='Member Search ID']" in strategy.primary or "member_id" in strategy.primary
        assert any("member_id" in fb for fb in strategy.fallbacks)
        await browser.close()


@pytest.mark.asyncio
async def test_inspector_table_relation():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html>
                <body>
                    <table>
                        <tr>
                            <td>Savings Balance:</td>
                            <td id="savings-val">$4,250.75</td>
                        </tr>
                    </table>
                </body>
            </html>
        """)

        inspector = DOMInspector()
        strategy = await inspector.inspect_element(page, "#savings-val")

        assert "#savings-val" in strategy.primary or "#savings-val" in strategy.fallbacks
        # Relational XPath should be captured
        assert any("Savings Balance" in fb for fb in strategy.fallbacks)

        await browser.close()


@pytest.mark.asyncio
async def test_inspector_get_interactive_elements():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html>
                <body>
                    <h1>Core Banking Portal</h1>
                    <label for="search">Account Search</label>
                    <input id="search" name="q" type="text" />
                    <button type="submit">Submit</button>
                    <a href="/help">Help</a>
                    <button style="display:none">Hidden Button</button>
                </body>
            </html>
        """)

        inspector = DOMInspector()
        elements = await inspector.get_interactive_elements(page)

        assert len(elements) >= 3  # input, button, link
        tags = [e["tag"] for e in elements]
        assert "input" in tags
        assert "button" in tags
        assert "a" in tags
        # Hidden button should be filtered out
        texts = [e["text"] for e in elements]
        assert "Hidden Button" not in texts

        await browser.close()


@pytest.mark.asyncio
async def test_inspector_iframe_detection():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        # Set up a page with an iframe
        await page.set_content("""
            <html>
                <body>
                    <h2>Main Page</h2>
                    <iframe id="legacy-frame" name="core_frame" srcdoc="<button id='action-btn'>Frame Action</button>"></iframe>
                </body>
            </html>
        """)

        # Wait for frame to be attached
        frame_element = page.frame_locator("#legacy-frame")
        btn_loc = frame_element.locator("#action-btn")
        await btn_loc.wait_for()

        inspector = DOMInspector()
        strategy = await inspector.inspect_element(page, btn_loc)

        assert strategy.frame_selector in ("#legacy-frame", "frame[name='core_frame']", "iframe")
        assert "role:button[name='Frame Action']" in strategy.primary or "#action-btn" in strategy.primary

        await browser.close()


@pytest.mark.asyncio
async def test_inspector_inputs_and_locators():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html>
                <body>
                    <input type="checkbox" id="terms-agree" name="agree" aria-label="I Agree to Terms" />
                    <textarea id="notes-field" name="notes" placeholder="Enter transfer notes"></textarea>
                    <div id="untyped-div" class="badge badge-warning">Warning notice</div>
                    <span name="only-name">Anonymous Span</span>
                </body>
            </html>
        """)

        inspector = DOMInspector()

        # Checkbox
        s_cb = await inspector.inspect_element(page, "#terms-agree", context_name="Terms Checkbox")
        assert "role:checkbox" in s_cb.primary or "#terms-agree" in s_cb.primary
        assert "[Terms Checkbox]" in (s_cb.rationale or "")

        # Textarea
        s_ta = await inspector.inspect_element(page, page.locator("#notes-field"))
        assert "#notes-field" in s_ta.primary or "role:textbox" in s_ta.primary

        # Untyped div with ID and classes
        s_div = await inspector.inspect_element(page, "#untyped-div")
        assert "#untyped-div" in s_div.primary or any("#untyped-div" in fb for fb in s_div.fallbacks)

        # Build test locator coverage
        assert inspector._build_test_locator(page, "label:Notes") is not None
        assert inspector._build_test_locator(page, "text:Warning notice") is not None
        assert inspector._build_test_locator(page, "css:.badge") is not None
        assert inspector._build_test_locator(page, "xpath=//div") is not None

        await browser.close()


def test_inspector_derive_locator_strategy_direct():
    inspector = DOMInspector()
    
    # Minimal element with only tag
    info_tag_only = {
        "tag": "span",
        "role": None,
        "accessibleName": "",
        "labelText": "",
        "innerText": "",
        "id": "",
        "nameAttr": "",
        "classList": [],
        "frameSelector": None,
        "tableRelation": None,
    }
    s1 = inspector._derive_locator_strategy(info_tag_only)
    assert s1.primary == "span"

    # Element with name attribute only
    info_name_only = {
        "tag": "input",
        "role": None,
        "accessibleName": "",
        "labelText": "",
        "innerText": "",
        "id": "",
        "nameAttr": "query_field",
        "classList": ["form-control", "search-box"],
        "frameSelector": None,
        "tableRelation": None,
    }
    s2 = inspector._derive_locator_strategy(info_name_only)
    assert s2.primary == "input[name='query_field']"
    assert "input.form-control.search-box" in s2.fallbacks


@pytest.mark.asyncio
async def test_inspector_apostrophe_and_quote_escaping():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("""
            <html>
                <body>
                    <button id="btn-apostrophe">Member's Account</button>
                    <button id="btn-quote">"VIP" Tier</button>
                </body>
            </html>
        """)

        inspector = DOMInspector()

        # 1. Element with apostrophe in accessible name
        strat_apostrophe = await inspector.inspect_element(page, "#btn-apostrophe")
        assert strat_apostrophe.primary == 'role:button[name="Member\'s Account"]'
        assert any("text:Member's Account" in fb for fb in strat_apostrophe.fallbacks)

        # Verify Playwright locates it with primary
        loc_primary = inspector._build_test_locator(page, strat_apostrophe.primary)
        assert await loc_primary.count() == 1

        # Verify legacy escaped syntax is also successfully resolved by the builder
        loc_legacy = inspector._build_test_locator(page, "role:button[name='Member\\'s Account']")
        assert await loc_legacy.count() == 1

        # 2. Element with double quotes in accessible name
        strat_quote = await inspector.inspect_element(page, "#btn-quote")
        assert strat_quote.primary == "role:button[name='\"VIP\" Tier']"
        loc_quote = inspector._build_test_locator(page, strat_quote.primary)
        assert await loc_quote.count() == 1

        await browser.close()


