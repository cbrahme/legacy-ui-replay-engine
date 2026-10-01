import re
from typing import Any, Dict, List, Optional, Tuple, Union
from playwright.async_api import ElementHandle, Frame, Locator, Page

from src.models.artifact import LocatorStrategy


_DOM_INSPECTOR_JS = """
(el) => {
    function sanitize(str) {
        if (!str) return '';
        return str.replace(/[\\r\\n\\t]+/g, ' ').trim();
    }

    function escapeAttr(str) {
        if (!str) return '';
        return str.replace(/['"\\\\]/g, '\\\\$&');
    }

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const id = el.id ? el.id.trim() : '';
    const nameAttr = el.getAttribute('name') ? el.getAttribute('name').trim() : '';
    const placeholder = el.getAttribute('placeholder') ? sanitize(el.getAttribute('placeholder')) : '';
    const title = el.getAttribute('title') ? sanitize(el.getAttribute('title')) : '';
    const ariaLabel = el.getAttribute('aria-label') ? sanitize(el.getAttribute('aria-label')) : '';
    
    // Find associated label text
    let labelText = '';
    if (ariaLabel) {
        labelText = ariaLabel;
    } else if (el.getAttribute('aria-labelledby')) {
        const labelledBy = document.getElementById(el.getAttribute('aria-labelledby'));
        if (labelledBy) labelText = sanitize(labelledBy.innerText || labelledBy.textContent);
    } else if (id) {
        const labelEl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (labelEl) labelText = sanitize(labelEl.innerText || labelEl.textContent);
    }
    if (!labelText) {
        const parentLabel = el.closest('label');
        if (parentLabel) labelText = sanitize(parentLabel.innerText || parentLabel.textContent);
    }

    // Determine Role
    let role = el.getAttribute('role');
    if (!role) {
        if (tag === 'input') {
            if (['button', 'submit', 'reset'].includes(type)) role = 'button';
            else if (type === 'checkbox') role = 'checkbox';
            else if (type === 'radio') role = 'radio';
            else role = 'textbox';
        } else if (tag === 'button') {
            role = 'button';
        } else if (tag === 'a' && el.hasAttribute('href')) {
            role = 'link';
        } else if (tag === 'select') {
            role = 'combobox';
        } else if (tag === 'textarea') {
            role = 'textbox';
        } else if (tag === 'table') {
            role = 'table';
        } else if (/^h[1-6]$/.test(tag)) {
            role = 'heading';
        } else if (tag === 'img') {
            role = 'img';
        }
    }

    // Determine Accessible Name
    let accessibleName = '';
    if (ariaLabel) {
        accessibleName = ariaLabel;
    } else if (labelText) {
        accessibleName = labelText;
    } else if (['button', 'submit', 'reset'].includes(type) && el.value) {
        accessibleName = sanitize(el.value);
    } else if (['button', 'a', 'heading'].includes(role) || ['button', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'].includes(tag)) {
        accessibleName = sanitize(el.innerText || el.textContent);
    } else if (placeholder) {
        accessibleName = placeholder;
    } else if (title) {
        accessibleName = title;
    } else if (nameAttr) {
        accessibleName = nameAttr;
    }

    // Visible text content
    const innerText = sanitize(el.innerText || el.textContent);

    // Frame detection
    let frameSelector = null;
    try {
        const win = el.ownerDocument.defaultView;
        if (win && win !== window.top) {
            const parentDoc = win.parent.document;
            const frames = parentDoc.querySelectorAll('iframe, frame');
            for (const f of frames) {
                if (f.contentWindow === win) {
                    if (f.id) frameSelector = `#${f.id}`;
                    else if (f.name) frameSelector = `frame[name='${f.name}']`;
                    else if (f.getAttribute('src')) {
                        const srcPart = f.getAttribute('src').split('/').pop().split('?')[0];
                        if (srcPart) frameSelector = `iframe[src*='${srcPart}']`;
                        else frameSelector = 'iframe';
                    } else {
                        frameSelector = 'iframe';
                    }
                    break;
                }
            }
        }
    } catch (e) {
        // Cross-origin boundary fallback
        frameSelector = 'iframe';
    }

    // Relational table anchor if inside table
    let tableRelation = null;
    if (tag === 'td' || tag === 'th') {
        const row = el.closest('tr');
        if (row) {
            const prevCells = Array.from(row.children);
            const myIndex = prevCells.indexOf(el);
            if (myIndex > 0) {
                const labelCell = prevCells[myIndex - 1];
                const labelCellText = sanitize(labelCell.innerText || labelCell.textContent);
                if (labelCellText && labelCellText.length < 40) {
                    tableRelation = {
                        prevCellText: labelCellText,
                        colIndex: myIndex + 1
                    };
                }
            }
        }
    }

    // Classes
    const classList = Array.from(el.classList || []).filter(c => !c.startsWith('ng-') && !c.startsWith('v-'));

    return {
        tag,
        type,
        id,
        nameAttr,
        placeholder,
        title,
        role,
        accessibleName,
        labelText,
        innerText,
        frameSelector,
        classList,
        tableRelation
    };
}
"""


class DOMInspector:
    """
    Playwright DOM and Accessibility Tree introspection engine.
    Programmatically derives and validates a resilient 4-tier locator hierarchy:
    Tier 1 (Primary): Accessible Role and Name (e.g. role:button[name='Search Records'])
    Tier 2: Explicit Text / Label Anchor (e.g. text:Search Records, label:Member ID)
    Tier 3: Scoped Unique CSS (e.g. #savings-balance-val, input[name='member_search'])
    Tier 4: Structural / Canonical XPath (e.g. //input[@name='member_search'])
    """

    def __init__(self, validation_timeout_ms: int = 1000):
        self.validation_timeout_ms = validation_timeout_ms

    async def inspect_element(
        self,
        page: Page,
        element: Union[ElementHandle, Locator, str],
        context_name: Optional[str] = None,
    ) -> LocatorStrategy:
        """
        Inspects an element handle, locator, or selector string on the live page
        and derives a verified multi-tier LocatorStrategy.
        """
        # Resolve to ElementHandle if needed
        element_handle = None
        if isinstance(element, str):
            loc = page.locator(element).first
            element_handle = await loc.element_handle(timeout=self.validation_timeout_ms)
        elif isinstance(element, Locator):
            element_handle = await element.first.element_handle(timeout=self.validation_timeout_ms)
        elif isinstance(element, ElementHandle):
            element_handle = element

        if not element_handle:
            raise ValueError(f"Could not locate element for inspection: {element}")

        # Execute in-page introspection
        raw_info: Dict[str, Any] = await element_handle.evaluate(_DOM_INSPECTOR_JS)

        # Derive candidate locators across 4 tiers
        strategy = self._derive_locator_strategy(raw_info, context_name=context_name)

        # Validate candidates on page to ensure accuracy
        verified_strategy = await self._validate_and_prune_strategy(page, strategy)
        return verified_strategy

    def _derive_locator_strategy(
        self,
        info: Dict[str, Any],
        context_name: Optional[str] = None,
    ) -> LocatorStrategy:
        """
        Builds a multi-tier candidate LocatorStrategy from inspected DOM attributes.
        """
        tag = info.get("tag", "")
        role = info.get("role")
        acc_name = info.get("accessibleName", "")
        label_text = info.get("labelText", "")
        inner_text = info.get("innerText", "")
        elem_id = info.get("id", "")
        name_attr = info.get("nameAttr", "")
        classes = info.get("classList", [])
        frame_selector = info.get("frameSelector")
        table_rel = info.get("tableRelation")

        primary: Optional[str] = None
        fallbacks: List[str] = []
        rationales: List[str] = []

        # 1. Tier 1: Accessible Role & Name
        if role and acc_name and len(acc_name) < 80:
            if "'" not in acc_name:
                primary = f"role:{role}[name='{acc_name}']"
            elif '"' not in acc_name:
                primary = f'role:{role}[name="{acc_name}"]'
            else:
                escaped = acc_name.replace('"', '\\"')
                primary = f'role:{role}[name="{escaped}"]'
            rationales.append(f"Primary uses accessible role '{role}' and name '{acc_name}'")
        elif role and not acc_name:
            primary = f"role:{role}"
            rationales.append(f"Primary uses accessible role '{role}'")

        # 2. Tier 2: Label or Text Anchor
        if label_text and label_text != acc_name:
            fallbacks.append(f"label:{label_text}")
        if inner_text and len(inner_text) < 50:
            clean_text = inner_text.strip()
            text_loc = f"text:{clean_text}"
            if primary != text_loc and text_loc not in fallbacks:
                fallbacks.append(text_loc)

        # 3. Tier 3: Scoped CSS
        if elem_id:
            css_id = f"#{elem_id}"
            if not primary:
                primary = css_id
                rationales.append(f"Primary uses unique element ID '{elem_id}'")
            elif css_id not in fallbacks:
                fallbacks.append(css_id)

        if name_attr:
            css_name = f"{tag}[name='{name_attr}']"
            if not primary:
                primary = css_name
                rationales.append(f"Primary uses input name attribute '{name_attr}'")
            elif css_name not in fallbacks:
                fallbacks.append(css_name)

        if classes:
            class_selector = f"{tag}." + ".".join(classes[:2])
            if class_selector not in fallbacks:
                fallbacks.append(class_selector)

        # 4. Tier 4: Structural / Table XPath
        if elem_id:
            fallbacks.append(f"//*[@id='{elem_id}']")
        elif name_attr:
            fallbacks.append(f"//{tag}[@name='{name_attr}']")

        if table_rel:
            prev_text = table_rel.get("prevCellText")
            if prev_text:
                xpath_rel = f"//td[contains(text(), '{prev_text}')]/following-sibling::td[1]"
                fallbacks.append(xpath_rel)
                rationales.append(f"Includes relational XPath anchored to preceding label '{prev_text}'")

        if inner_text and len(inner_text) < 40 and not table_rel:
            xpath_text = f"//{tag}[contains(text(), '{inner_text[:30]}')]"
            if xpath_text not in fallbacks:
                fallbacks.append(xpath_text)

        # Fallback if primary still None
        if not primary:
            if fallbacks:
                primary = fallbacks.pop(0)
            elif elem_id:
                primary = f"#{elem_id}"
            else:
                primary = tag
            rationales.append(f"Defaulted primary locator to '{primary}'")

        # Deduplicate fallbacks while maintaining order
        deduped_fallbacks: List[str] = []
        for fb in fallbacks:
            if fb != primary and fb not in deduped_fallbacks:
                deduped_fallbacks.append(fb)

        rationale_text = "; ".join(rationales)
        if context_name:
            rationale_text = f"[{context_name}] {rationale_text}"

        return LocatorStrategy(
            frame_selector=frame_selector,
            primary=primary,
            fallbacks=deduped_fallbacks,
            rationale=rationale_text,
        )

    async def _validate_and_prune_strategy(
        self,
        page: Page,
        strategy: LocatorStrategy,
    ) -> LocatorStrategy:
        """
        Validates locators on the live page/frame and discards broken locators.
        """
        root = page.frame_locator(strategy.frame_selector) if strategy.frame_selector else page

        valid_fallbacks: List[str] = []
        for fb in strategy.fallbacks:
            try:
                locator = self._build_test_locator(root, fb)
                count = await locator.count()
                if count > 0:
                    valid_fallbacks.append(fb)
            except Exception:
                # Discard invalid/broken locator
                continue

        return LocatorStrategy(
            frame_selector=strategy.frame_selector,
            primary=strategy.primary,
            fallbacks=valid_fallbacks,
            rationale=strategy.rationale,
        )

    def _build_test_locator(self, root, loc_str: str) -> Locator:
        """Builds Playwright Locator from syntax string."""
        s = loc_str.strip()

        # Matches role:tag or role:tag[name="..."] / role:tag[name='...']
        # \2 ensures the closing quote matches the opening quote
        role_match = re.match(r"^role:([a-zA-Z]+)(?:\[name=(['\"])([\s\S]*?)\2\])?$", s)
        if role_match:
            role = role_match.group(1)
            name = role_match.group(3)
            if name:
                clean_name = name.replace('\\"', '"').replace("\\'", "'")
                return root.get_by_role(role, name=clean_name)
            return root.get_by_role(role)

        if s.startswith("text:"):
            raw_text = s[5:].strip().replace('\\"', '"').replace("\\'", "'")
            return root.get_by_text(raw_text)

        if s.startswith("label:"):
            raw_label = s[6:].strip().replace('\\"', '"').replace("\\'", "'")
            return root.get_by_label(raw_label)

        if s.startswith("css:"):
            return root.locator(s[4:].strip())

        if s.startswith("xpath:"):
            return root.locator(f"xpath={s[6:].strip()}")

        return root.locator(s)

    async def get_interactive_elements(self, page: Page) -> List[Dict[str, Any]]:
        """
        Scans the current page and extracts all interactive or informational
        elements to provide a compact element map for the discovery agent.
        """
        js_extract = """
        () => {
            function sanitize(str) {
                if (!str) return '';
                return str.replace(/[\\r\\n\\t]+/g, ' ').trim();
            }

            const targets = Array.from(document.querySelectorAll(
                'button, input, select, textarea, a[href], [role="button"], [role="link"], table, .alert, .badge'
            ));

            const results = [];
            let index = 1;

            for (const el of targets) {
                // Check visibility
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                    continue;
                }
                if (rect.width === 0 && rect.height === 0) {
                    continue;
                }

                const tag = el.tagName.toLowerCase();
                const id = el.id ? el.id.trim() : '';
                const name = el.getAttribute('name') ? el.getAttribute('name').trim() : '';
                const placeholder = el.getAttribute('placeholder') ? sanitize(el.getAttribute('placeholder')) : '';
                const type = el.getAttribute('type') || '';
                const role = el.getAttribute('role') || '';
                const innerText = sanitize(el.innerText || el.textContent);
                
                let label = '';
                if (id) {
                    const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                    if (l) label = sanitize(l.innerText || l.textContent);
                }
                if (!label && el.getAttribute('aria-label')) {
                    label = sanitize(el.getAttribute('aria-label'));
                }

                results.push({
                    index: index++,
                    tag: tag,
                    id: id,
                    name: name,
                    type: type,
                    role: role,
                    placeholder: placeholder,
                    label: label,
                    text: innerText.slice(0, 80),
                    classes: Array.from(el.classList || []).slice(0, 3).join(' ')
                });
            }
            return results;
        }
        """
        return await page.evaluate(js_extract)
