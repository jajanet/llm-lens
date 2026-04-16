"""Static-content invariants for the frontend JS/CSS.

These don't execute JS — they grep the shipped source to catch the class of
bug where one module renders a class name and another queries a different
one. The `hydrateNames` regression (selector `.col-name-clickable` after the
renderer switched to `.col-name-text`) is the canonical example: no runtime
error, just "names never load."
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "llm_lens" / "static" / "js"
CSS = ROOT / "llm_lens" / "static" / "css" / "styles.css"


def read(p: Path) -> str:
    return p.read_text()


@pytest.fixture(scope="module")
def convos_js():
    return read(JS / "views" / "conversations.js")


@pytest.fixture(scope="module")
def messages_js():
    return read(JS / "views" / "messages.js")


@pytest.fixture(scope="module")
def projects_js():
    return read(JS / "views" / "projects.js")


@pytest.fixture(scope="module")
def utils_js():
    return read(JS / "utils.js")


@pytest.fixture(scope="module")
def main_js():
    return read(JS / "main.js")


@pytest.fixture(scope="module")
def state_js():
    return read(JS / "state.js")


@pytest.fixture(scope="module")
def styles_css():
    return read(CSS)


# --- hydrateNames selector agreement --------------------------------------
# Regression: renderer wrote .col-name-text inside .col-name; hydrateNames
# queried .col-name-clickable (the old class). Names silently stopped loading.

def test_table_name_cell_has_text_span_hydrate_can_target(convos_js):
    assert 'class="col-name-text"' in convos_js, (
        "renderTable must wrap name in <span class='col-name-text'> so "
        "hydrateNames can patch text without clobbering the copy button"
    )
    assert '.col-name-text' in convos_js, "hydrateNames must query .col-name-text"


def test_card_name_has_text_span_hydrate_can_target(convos_js):
    assert 'class="card-name-text"' in convos_js
    assert '.card-name-text' in convos_js


def test_hydratenames_does_not_reference_removed_clickable_class(convos_js):
    assert 'col-name-clickable' not in convos_js, (
        "legacy class removed — any reference would mean a stale selector"
    )


# --- Copy-button + open-row action coexistence ----------------------------
# Clicking the name cell must open the convo (row's data-action=open-convo).
# The copy button nested inside must carry data-action=copy-resume so
# event delegation (closest [data-action]) picks the button on icon clicks.

def test_row_opens_convo_and_name_cell_is_not_copy_action(convos_js):
    assert 'data-action="open-convo"' in convos_js
    # Name cell itself must not carry copy-resume (that was the old behavior).
    # The copy button inside carries it.
    assert '<td class="col-name' in convos_js
    assert 'class="copy-id-btn" data-action="copy-resume"' in convos_js


# --- Table layout: flex wrapper lives on a div, not the <td> --------------
# Setting `display:flex` directly on a <td> breaks table-cell layout and
# produces a visible gap between the name column and the rest.

def test_flex_wrap_is_on_div_not_td(styles_css):
    assert ".col-name .col-name-wrap" in styles_css, (
        "flex layout for the name cell must target the inner wrapper div"
    )
    # Negative: there should not be a rule setting display:flex directly on
    # the td.col-name.
    bad = "table.tbl .col-name { display: flex"
    assert bad not in styles_css, (
        "display:flex on the <td> breaks table layout — use .col-name-wrap"
    )


def test_renderTable_emits_col_name_wrap(convos_js):
    assert 'class="col-name-wrap"' in convos_js


# --- Graph filters: every renderTokenBars callsite passes state.filters ---
# Regression: the graph ignored archived/deleted toggles because filters
# weren't plumbed through. Any future callsite must pass them too.

def test_rendertokenbars_callsites_pass_filters(projects_js):
    import re
    # Find each renderTokenBars(...) call and check it includes state.filters.
    calls = re.findall(r"renderTokenBars\s*\([^;]*?\)", projects_js, flags=re.DOTALL)
    assert calls, "expected at least one renderTokenBars call in projects.js"
    for call in calls:
        assert "state.filters" in call, (
            f"renderTokenBars call missing `state.filters`:\n{call}"
        )


def test_applyBucketFilters_exported(utils_js):
    assert "export function applyBucketFilters" in utils_js, (
        "applyBucketFilters must be exported so Node tests can cover it"
    )


# --- Last-checkbox guard stays in the toggle-filter handler ---------------

def test_toggle_filter_guards_last_checkbox(main_js):
    # Match the heart of the guard without being too brittle about wording.
    assert "e.target.checked = true" in main_js, (
        "toggle-filter must re-check the box when unchecking the last-on"
    )
    assert '"active", "archived", "deleted"' in main_js


# --- Load-time filters invariant exists ------------------------------------

def test_initial_filters_invariant_in_state(state_js):
    assert "computeInitialFilters" in state_js
    assert 'setItem("filter_active"' in state_js, (
        "repair must persist so next load stays valid"
    )


# --- Conversation header shows name + copy button -------------------------

def test_messages_breadcrumb_shows_convo_name_and_copy(messages_js):
    assert "hydrateConvoName" in messages_js
    assert 'class="bc-convo-name"' in messages_js
    assert 'copy-id-btn-inline' in messages_js
    # No longer hardcodes the literal "Conversation" as the only label.
    # (It remains as a fallback inside renderBreadcrumb — that's fine.)


def test_breadcrumb_inline_copy_style_exists(styles_css):
    assert ".copy-id-btn-inline" in styles_css


# --- Copy-id-btn base style is hover-hidden for rows/cards ----------------

def test_copy_id_btn_hidden_until_hover(styles_css):
    # Base opacity:0 + reveal on row/card hover or keyboard focus.
    assert "opacity: 0" in styles_css
    assert "tr:hover .copy-id-btn" in styles_css
    assert ".card:hover .copy-id-btn" in styles_css


# --- Export / Extract menu + JSONL fields modal ---------------------------
# Ensures the selection bar's Export/Extract button opens a menu with all
# the intended destinations and the modal exposes the expected fields.

@pytest.fixture(scope="module")
def index_html():
    return read(ROOT / "llm_lens" / "static" / "index.html")


@pytest.fixture(scope="module")
def exports_js():
    return read(JS / "exports.js")


def test_download_raw_convo_button_in_header(index_html):
    # The new page-toolbar button sits next to the existing Edit toggle.
    assert 'id="download-raw-convo"' in index_html
    assert 'data-action="download-raw-convo"' in index_html
    # Paranoia: it should sit BEFORE edit-toggle in the DOM so visual order
    # matches the user's mental model ("next to Edit").
    assert index_html.index("download-raw-convo") < index_html.index("edit-toggle")


def test_download_raw_convo_has_tooltip(index_html):
    # A placement without explanation would be mystery-meat; keep the
    # tooltip on the element.
    import re
    m = re.search(r'id="download-raw-convo"[^>]*title="([^"]+)"', index_html)
    assert m, "download-raw-convo button must carry a title/tooltip"
    assert len(m.group(1)) > 10


def test_selection_bar_uses_export_extract_menu(messages_js):
    # Old loose Copy / Save-to-new-convo buttons in the sel-bar are gone;
    # the single menu-opener replaces them.
    assert 'data-action="open-export-menu"' in messages_js
    assert "Export/Extract" in messages_js
    # The old button labels must not linger — they'd confuse users who
    # expect only the menu.
    assert '>Save to new convo<' not in messages_js
    # "Copy" as a bare selection-bar button is gone; the name now lives
    # inside the menu as "Copy plain to clipboard".
    assert 'data-action="copy-selected">Copy<' not in messages_js


def test_export_menu_contains_all_destinations(messages_js):
    # Each menu item's action must be wired.
    for action in (
        "copy-selected",
        "copy-selected-jsonl",
        "download-selected-jsonl",
        "save-selected",
        "open-jsonl-fields",
    ):
        assert f'data-action="{action}"' in messages_js, f"missing {action} menu entry"


def test_export_menu_actions_registered_in_main(main_js):
    # Without these entries in the action map the menu items silently do
    # nothing — catch that at build-time.
    for action in (
        "copy-selected-jsonl",
        "download-selected-jsonl",
        "open-export-menu",
        "open-jsonl-fields",
        "download-raw-convo",
    ):
        assert f'"{action}"' in main_js, f"main.js action map missing {action}"


def test_jsonl_fields_modal_locks_role_and_content(messages_js):
    # role + content are structural; their checkboxes must render disabled.
    # Checking for the exact snippet that flags required fields.
    assert "REQUIRED_EXPORT_FIELDS.includes(f)" in messages_js
    assert "disabled" in messages_js  # at least one disabled attr in the file


def test_jsonl_fields_modal_has_select_all(messages_js):
    assert "data-select-all" in messages_js
    assert ">Select all<" in messages_js


def test_exports_module_declares_required_fields(exports_js):
    # Required-fields constant is the single source of truth for which
    # checkboxes lock.
    assert "REQUIRED_EXPORT_FIELDS" in exports_js
    assert '"role"' in exports_js and '"content"' in exports_js


def test_exports_jsonl_respects_required_even_if_unchecked(exports_js):
    # The serializer itself re-guards required fields so a stale
    # state.downloadFields can't produce a role-less JSONL line.
    assert "REQUIRED_EXPORT_FIELDS.includes(f)" in exports_js


def test_api_has_download_fields_endpoints(main_js):
    # api.js re-exports through main.js' import graph — testing api.js
    # directly is enough. Use the api.js file.
    api_js = read(JS / "api.js")
    assert "getDownloadFields" in api_js
    assert "saveDownloadFields" in api_js
    assert "rawConversationUrl" in api_js


def test_jsonl_fields_modal_css_exists(styles_css):
    assert ".jsonl-fields-list" in styles_css
    assert ".jsonl-field-row" in styles_css


# Preview-before-apply modal. The modal itself is tested via the pure-helper
# unit tests in tests/js/test_preview.mjs; these invariants guard the static
# wiring so a rename in one file doesn't silently break the feature.

@pytest.fixture(scope="module")
def preview_js():
    return read(JS / "preview.js")

def test_preview_module_exports_pure_helpers(preview_js):
    # Unit tests import these by name — if they get renamed or un-exported
    # the JS tests break loudly but Python will catch the missing export too.
    assert "export function diffWords" in preview_js
    assert "export function deltaOf" in preview_js
    assert "export function computeRows" in preview_js
    assert "export function showPreviewModal" in preview_js

def test_state_persists_preview_toggle(state_js):
    # Default ON (only explicit "0" turns it off).
    assert 'localStorage.getItem("previewEnabled") !== "0"' in state_js
    assert "setPreviewEnabled" in state_js
    assert "setPreviewView" in state_js

def test_main_registers_toggle_preview_action(main_js):
    assert '"toggle-preview"' in main_js
    assert "setPreviewEnabled" in main_js

def test_messages_wires_preview_into_both_transform_paths(messages_js):
    assert 'from "../preview.js"' in messages_js
    assert "showPreviewModal" in messages_js
    # Both single-message and bulk entry points must gate on the setting.
    assert messages_js.count("state.previewEnabled") >= 2

def test_transform_menus_include_preview_toggle(messages_js):
    assert 'data-action="toggle-preview"' in messages_js
    # Label flips between "Turn on" and "Turn off" depending on current state,
    # so both strings must exist in the source.
    assert "Turn on preview edits" in messages_js
    assert "Turn off preview edits" in messages_js

def test_preview_modal_has_skip_checkbox(preview_js):
    # Top-bar checkbox that flips the global preview setting in place. The
    # modal stays open so the user still chooses whether to commit the
    # current batch under review.
    assert 'data-preview-skip' in preview_js
    assert 'type="checkbox"' in preview_js

def test_preview_modal_has_apply_all_and_apply_selected(preview_js):
    assert "data-preview-apply-all" in preview_js
    assert "data-preview-apply-selected" in preview_js
    assert "data-preview-cancel" in preview_js

def test_preview_css_exists(styles_css):
    assert ".modal.preview-modal" in styles_css
    assert ".preview-row" in styles_css
    assert ".preview-delta" in styles_css
    assert ".preview-inline" in styles_css
    assert ".preview-stacked" in styles_css
