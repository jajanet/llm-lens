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
