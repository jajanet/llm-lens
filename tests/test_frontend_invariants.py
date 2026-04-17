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

def test_preview_modal_has_select_all_and_apply_selected(preview_js):
    # Apply all is gone; users click Select all (dynamic toggle) then Apply
    # selected, matching the edit-convo pattern.
    assert "data-preview-apply-all" not in preview_js
    assert "data-preview-select-all" in preview_js
    assert "data-preview-apply-selected" in preview_js
    assert "data-preview-cancel" in preview_js

def test_preview_css_exists(styles_css):
    assert ".modal.preview-modal" in styles_css
    assert ".preview-row" in styles_css
    assert ".preview-delta" in styles_css
    assert ".preview-inline" in styles_css
    assert ".preview-stacked" in styles_css



@pytest.fixture(scope="module")
def transforms_js():
    return read(JS / "transforms.js")


@pytest.fixture(scope="module")
def pill_list_js():
    return read(JS / "pill_list.js")


@pytest.fixture(scope="module")
def api_js():
    return read(JS / "api.js")


def test_word_list_kinds_includes_custom_filter(messages_js):
    # The gate that decides whether a transform needs word lists must
    # know about the new kind — otherwise the modal's Calculate-from-
    # cache list never reaches the transform.
    assert "remove_custom_filter" in messages_js
    assert "WORD_LIST_KINDS" in messages_js


def test_transform_labels_has_remove_custom_filter(messages_js):
    assert "remove_custom_filter" in messages_js
    assert "Remove custom filter" in messages_js


def test_ensure_word_lists_fallback_includes_new_keys(messages_js):
    # The fallback object (used when api.getWordLists() fails) must
    # carry the new keys so downstream consumers don't crash.
    assert "custom_filter" in messages_js
    assert "whitelist" in messages_js


def test_empty_custom_filter_guard_in_transform_paths(messages_js):
    # Both single-message and bulk transform paths must guard against
    # an empty custom_filter list — the alert points at Calculate from
    # cache so the user knows where to go.
    assert messages_js.count("Custom filter list is empty") >= 2


def test_transforms_exports_filterByWhitelist(transforms_js):
    assert "export function filterByWhitelist" in transforms_js


def test_transforms_apply_wires_whitelist(transforms_js):
    # The dispatch must filter every remove-* list against the whitelist
    # before passing it into the strip functions.
    assert "filterByWhitelist" in transforms_js
    assert "remove_custom_filter" in transforms_js


def test_pill_list_component_exports_mount(pill_list_js):
    assert "export function mountPillList" in pill_list_js


def test_pill_list_supports_backspace_and_enter(pill_list_js):
    # Both deletion paths (× click, backspace on empty input) and both
    # commit paths (Enter, + button) are part of the spec.
    assert '"Backspace"' in pill_list_js
    assert '"Enter"' in pill_list_js
    assert "pill-remove" in pill_list_js
    assert "pill-add-btn" in pill_list_js


def test_api_has_scan_custom_filter(api_js):
    assert "scanCustomFilter" in api_js
    assert "/custom-filter/scan" in api_js
    # Scan is now per-convo — URL includes folder + convo id placeholders.
    assert "/api/projects/" in api_js and "/conversations/" in api_js


def test_curate_modal_order_priming_first_custom_filter_last(messages_js):
    # Modal order in openWordListsModal: Priming + Verbosity up top
    # (those are the shipped-default, actually-useful ones), then
    # Whitelist, then Custom filter last (experimental, de-emphasized).
    idx_priming = messages_js.find("Priming language</strong>")
    idx_verbosity = messages_js.find("Verbosity</strong>")
    idx_whitelist = messages_js.find("Whitelist</strong>")
    idx_cf = messages_js.find("Custom filter</strong>")
    assert 0 < idx_priming < idx_verbosity < idx_whitelist < idx_cf


def test_curate_modal_has_calculate_from_cache_button(messages_js):
    assert 'data-calc="custom-filter"' in messages_js
    assert "Calculate from cache" in messages_js
    assert "cf-min-length" in messages_js
    assert "cf-min-count" in messages_js


def test_curate_modal_has_prune_whitelist_button(messages_js):
    assert "data-prune-whitelist" in messages_js


def test_pill_list_css_exists(styles_css):
    assert ".pill-list" in styles_css
    assert ".pill-remove" in styles_css
    assert ".pill-input" in styles_css



def test_priming_lowercase_user_text_checkbox_in_modal(messages_js):
    # The checkbox must live inside the Priming language section — its
    # effect is scoped to remove_priming on user-role text.
    assert 'id="wl-lowercase-user-text"' in messages_js
    assert "lowercase user-role text" in messages_js.lower()


def test_ensure_word_lists_fallback_includes_lowercase_user_text(messages_js):
    # If api.getWordLists() fails, the fallback object must carry the
    # new key so downstream code doesn't crash.
    assert "lowercase_user_text" in messages_js


def test_apply_transform_accepts_lowercase_user_text_and_role(transforms_js):
    assert "lowercase_user_text" in transforms_js
    assert "role" in transforms_js



def test_abbreviation_subsection_lives_under_verbosity(messages_js):
    # Must appear inside the Verbosity section — the abbreviation
    # substitution is gated behind that transform's checkbox.
    idx_verbosity = messages_js.find("Verbosity</strong>")
    idx_abbrev = messages_js.find("Abbreviation substitutions")
    assert 0 < idx_verbosity < idx_abbrev


def test_abbreviation_checkbox_in_modal(messages_js):
    assert 'id="wl-apply-abbreviations"' in messages_js
    assert "apply abbreviation substitutions" in messages_js.lower()


def test_abbreviation_description_links_tokenizer_and_caveats(messages_js):
    # User explicitly asked for a tokenizer link + caveats.
    assert "claude-tokenizer.vercel.app" in messages_js or "tiktoken" in messages_js
    assert "Caveats" in messages_js or "caveats" in messages_js


def test_pill_pair_list_exported(pill_list_js):
    assert "export function mountPillPairList" in pill_list_js


def test_apply_transform_accepts_abbreviations_and_apply_flag(transforms_js):
    assert "apply_abbreviations" in transforms_js
    assert "applyAbbreviations" in transforms_js


def test_ensure_word_lists_fallback_includes_abbreviations(messages_js):
    assert "apply_abbreviations" in messages_js



def test_custom_filter_enabled_checkbox_in_modal(messages_js):
    assert 'id="wl-custom-filter-enabled"' in messages_js
    assert "Show" in messages_js and "Remove custom filter" in messages_js


def test_visible_transform_entries_gates_custom_filter(messages_js):
    # The menu builder must filter remove_custom_filter out unless the
    # custom_filter_enabled flag is on — keeps the split-button menu
    # uncluttered for users who don't use the experimental feature.
    assert "visibleTransformEntries" in messages_js
    assert "custom_filter_enabled" in messages_js


def test_ensure_word_lists_fallback_includes_custom_filter_enabled(messages_js):
    # Fallback object must have the key so menu builders don't throw on
    # boot when the API is unreachable.
    assert "custom_filter_enabled: false" in messages_js or 'custom_filter_enabled":false' in messages_js



def test_collapse_punct_checkbox_in_priming_section(messages_js):
    # Lives inside the Priming section; same pattern as the lowercase
    # checkbox. Off by default.
    idx_priming = messages_js.find("Priming language</strong>")
    idx_cb = messages_js.find('id="wl-collapse-punct"')
    assert idx_priming > 0 and idx_cb > idx_priming
    assert "collapse aggressive-repeat punctuation" in messages_js.lower()


def test_transforms_exports_collapse_punct_repeats(transforms_js):
    assert "export function collapsePunctRepeats" in transforms_js


def test_ensure_word_lists_fallback_includes_collapse_punct_repeats(messages_js):
    assert "collapse_punct_repeats: false" in messages_js or 'collapse_punct_repeats":false' in messages_js


# Tag palette: the backend's NUM_COLORS is 8, so the CSS must define all
# eight `.tag-color-N` classes plus `--tag-cN-bg` vars for both the
# default (dark) theme block and the `body.light` override. If these go
# out of sync with NUM_COLORS, the UI renders some tags with no color.
def test_css_defines_eight_tag_colors_in_both_themes():
    css = CSS.read_text()
    for i in range(8):
        assert f".tag-color-{i}" in css, f"missing .tag-color-{i}"
        assert f"--tag-c{i}-bg" in css, f"missing --tag-c{i}-bg var"

    import re
    root_block = re.search(r":root\s*\{([^}]*)\}", css, re.DOTALL)
    light_block = re.search(r"body\.light\s*\{([^}]*)\}", css, re.DOTALL)
    assert root_block and light_block
    for i in range(8):
        assert f"--tag-c{i}-bg" in root_block.group(1), f":root missing --tag-c{i}-bg"
        assert f"--tag-c{i}-bg" in light_block.group(1), f"body.light missing --tag-c{i}-bg"



def test_preview_toggle_is_edit_mode_gated(convos_js):
    """The preview-position toggle button should only render when
    state.editMode is true — it's a curation-mode control, not always-on."""
    # Find the button emit and confirm it's inside an `if (state.editMode)` block.
    assert 'data-action="toggle-preview-position"' in convos_js
    # Naive structural check: the button line must be preceded by an editMode
    # guard within a reasonable window above.
    idx = convos_js.index('data-action="toggle-preview-position"')
    preceding = convos_js[max(0, idx - 800):idx]
    assert "state.editMode" in preceding, (
        "preview-position button should be gated by state.editMode"
    )


def test_preview_toggle_uses_full_word_message_not_msg(convos_js):
    """Button label spells out 'message' — user preference."""
    assert "Show first message" in convos_js
    assert "Show last message" in convos_js
    # And the abbreviated form is not shipped.
    assert "First msg" not in convos_js
    assert "Last msg" not in convos_js


def test_preview_toggle_has_separator_before_view_mode_buttons(convos_js):
    """A visual separator (vertical rule) sits between the preview toggle
    and the list/grid view-mode group so they read as distinct clusters."""
    # Find the list view-mode button; the separator should be just above it.
    list_btn_idx = convos_js.index('data-mode="list"')
    preceding = convos_js[max(0, list_btn_idx - 400):list_btn_idx]
    assert "border-left" in preceding or "border-right" in preceding or "background:var(--border)" in preceding, (
        "expected a visual separator (border/background) right before the view-mode buttons"
    )



def test_bulk_delete_injects_non_prose_warning(messages_js):
    """deleteSelected() uses `_countNonProseIn` + `_nonProseWarningBlock` so
    selecting any non-prose messages shows a warning in the confirm modal."""
    assert "_countNonProseIn" in messages_js
    assert "_nonProseWarningBlock" in messages_js
    # deleteSelected must invoke both when building its confirm body.
    del_start = messages_js.index("export function deleteSelected")
    del_body = messages_js[del_start:del_start + 2000]
    assert "_countNonProseIn(uuids)" in del_body
    assert "_nonProseWarningBlock" in del_body


def test_bulk_transform_confirm_mentions_non_prose_only_when_present(messages_js):
    """bulkTransform's confirm path conditionally appends a non-prose note
    based on `_countNonProseIn` — no more always-on noisy warning for
    prose-only selections."""
    bt_start = messages_js.index("export async function bulkTransform")
    bt_body = messages_js[bt_start:bt_start + 3000]
    assert "_countNonProseIn(ids)" in bt_body
    # The old always-on phrasing should be gone.
    assert "non-prose messages (with tool_use / thinking blocks) will have" not in bt_body


def test_non_prose_helper_reads_backend_flags(messages_js):
    """_isNonProseMsg consults the backend-provided has_tool_use /
    has_thinking booleans — not a brittle re-parse of flattened content."""
    assert "has_tool_use" in messages_js
    assert "has_thinking" in messages_js
    idx = messages_js.index("function _isNonProseMsg")
    body = messages_js[idx:idx + 200]
    assert "has_tool_use" in body and "has_thinking" in body



def test_preview_modal_banner_is_collapsible_details_element():
    """Non-prose banner in the preview modal uses <details>/<summary> so
    it can be collapsed — avoids dominating the diff panel when most rows
    are prose-only."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    assert 'preview-nonprose-banner' in p
    idx = p.index('preview-nonprose-banner')
    body = p[max(0, idx - 100):idx + 1000]
    assert "<details" in body
    assert "<summary" in body


def test_preview_rows_mark_non_prose_with_warning_glyph():
    """Per-row ⚠ marker on non-prose preview rows so user can see which
    ones will lose structural content before applying."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    assert 'preview-nonprose-mark' in p
    assert "row.nonProse" in p


def test_bulk_transform_forwards_flags_to_preview_modal():
    """bulkTransform must include has_tool_use/has_thinking on each
    candidate — otherwise the preview modal can't flag non-prose rows."""
    m = read(ROOT / "llm_lens" / "static" / "js" / "views" / "messages.js")
    bt_start = m.index("export async function bulkTransform")
    bt_body = m[bt_start:bt_start + 3000]
    assert "has_tool_use: !!m.has_tool_use" in bt_body
    assert "has_thinking: !!m.has_thinking" in bt_body



def test_preview_banner_hides_when_all_non_prose_unchecked():
    """Static invariant: updateNetDelta hides the non-prose banner when no
    currently-checked rows are non-prose. Unchecking every non-prose row
    in the preview makes the warning go away (nothing destructive left)."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    # The tracker set must be built
    assert "const nonProseIds = new Set" in p
    # updateNetDelta must toggle the banner based on checked-non-prose count
    idx = p.index("function updateNetDelta")
    body = p[idx:idx + 1200]
    assert "checkedNonProse" in body
    assert "preview-nonprose-banner" in body
    assert 'display = checkedNonProse > 0 ? "" : "none"' in body



def test_select_all_is_split_button_with_scope_menu():
    """In edit mode, the Select all button is a split button whose dropdown
    opens a scope menu with "Select prose only" and "Select non-prose only"."""
    m = read(ROOT / "llm_lens" / "static" / "js" / "views" / "messages.js")
    # Split button structure
    assert 'data-action="open-select-scope-menu"' in m
    # Menu items
    assert 'data-action="select-scope"' in m
    assert 'data-scope="prose"' in m
    assert 'data-scope="non_prose"' in m
    # Human-readable labels
    assert "Select prose only" in m
    assert "Select non-prose only" in m


def test_select_by_scope_is_additive_and_respects_search_filter():
    """selectByScope adds to state.msgSelected (doesn't replace), and
    restricts to the currently-filtered message list so search + scope
    compose naturally."""
    m = read(ROOT / "llm_lens" / "static" / "js" / "views" / "messages.js")
    idx = m.index("export function selectByScope")
    body = m[idx:idx + 1200]
    # additive
    assert "state.msgSelected.add" in body
    assert "state.msgSelected.clear" not in body
    # respects the search filter
    assert "state.msgSearch" in body


def test_select_scope_menu_handlers_wired_in_main():
    """Action handlers for the split button are registered in main.js."""
    mj = read(ROOT / "llm_lens" / "static" / "js" / "main.js")
    assert '"open-select-scope-menu"' in mj
    assert '"select-scope"' in mj
    assert "openSelectScopeMenu" in mj
    assert "selectByScope" in mj



def test_preview_modal_has_select_all_split_button_with_scope_menu():
    """Preview modal carries the same Select all + split scope menu as the
    main edit-mode toolbar, so users can quickly narrow the applied set to
    prose-only / non-prose-only within the modal itself."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    # Split button
    assert 'data-preview-select-all' in p
    assert 'data-preview-scope-menu' in p
    # Scope items
    assert 'data-preview-scope="prose"' in p
    assert 'data-preview-scope="non_prose"' in p
    # Labels
    assert "Select prose only" in p
    assert "Select non-prose only" in p


def test_preview_select_all_toggles_based_on_any_unchecked():
    """Click-behavior: if anything is unchecked → select all; otherwise
    deselect all. One button handles both."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    # Handler uses "anyUnchecked" to decide direction
    assert "anyUnchecked" in p
    # And it refreshes the net delta (which also refreshes the button label)
    idx = p.index("anyUnchecked")
    body = p[idx:idx + 400]
    assert "updateNetDelta" in body



def test_preview_modal_removes_apply_all_button():
    """Preview modal no longer has a separate 'Apply all' button — the
    Select all (toggle) + 'Apply selected' flow covers that case in two
    clicks, and the topbar stays uncluttered."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    assert 'data-preview-apply-all' not in p
    assert 'preview-apply-all' not in p


def test_preview_select_all_label_is_dynamic_like_edit_convo_view():
    """The preview modal's Select all button flips between
    'Select all (N)' and 'Deselect all' based on current check state,
    mirroring the edit-convo toolbar's behavior."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    # initial render uses the count-bearing label
    assert "`Select all (${rows.length})`" in p
    # updateNetDelta flips to Deselect all when all are checked
    idx = p.index("function updateNetDelta")
    body = p[idx:idx + 2000]
    assert '"Deselect all"' in body
    assert "allChecked" in body



def test_preview_apply_selected_shows_live_count():
    """Apply selected button shows the current checked count in parens and
    disables when nothing is selected."""
    p = read(ROOT / "llm_lens" / "static" / "js" / "preview.js")
    assert "Apply selected (${rows.length})" in p
    assert "Apply selected (${checked.size})" in p
    # disabled state when zero
    idx = p.index("data-preview-apply-selected]")
    # Find the updateNetDelta reference that sets disabled
    assert "applyBtn.disabled = checked.size === 0" in p


# ── TagScope invariants ────────────────────────────────────────────────
# Both scopes must expose the identical method surface so tag_components
# can be written once and used from either view. If someone adds a
# method to one scope and forgets the other, this test catches it.

import re as _re


def _scope_methods(factory_name):
    """Parse tag_scope.js and return the set of property names present
    on the object returned by the given factory (makeConvoScope or
    makeProjectScope)."""
    src = read(ROOT / "llm_lens" / "static" / "js" / "tag_scope.js")
    m = _re.search(
        rf"export function {factory_name}\(.*?\{{.*?return scope;\s*\}}",
        src,
        _re.DOTALL,
    )
    assert m, f"couldn't locate {factory_name}"
    block = m.group(0)
    # Match property names on the scope object: `name:`, `getLabels:`,
    # `refresh: async () => ...`, etc.
    return set(_re.findall(r"^\s{4}(\w+)\s*:", block, _re.MULTILINE))


def test_both_scopes_share_the_same_surface():
    convo = _scope_methods("makeConvoScope")
    proj = _scope_methods("makeProjectScope")
    # Both must expose the same property names — no divergence.
    assert convo == proj, f"convo - proj: {convo - proj}; proj - convo: {proj - convo}"
    # Minimum surface we rely on from tag_components:
    required = {
        "name", "onChange",
        "getLabels", "getAssignments", "getAssignment", "getActiveFilters",
        "setLabelsLocal", "setAssignmentsLocal", "setActiveFiltersLocal",
        "setLabels", "setAssignment", "bulkAssign",
        "refresh", "defaultIds",
    }
    missing = required - convo
    assert not missing, f"scope missing required methods: {missing}"


def test_tag_components_exports_expected_functions():
    src = read(ROOT / "llm_lens" / "static" / "js" / "tag_components.js")
    for name in [
        "pickDefaultColor",
        "renderSwatchRow",
        "renderTagPills",
        "renderTagBar",
        "toggleTagFilter",
        "toggleKeyTag",
        "openTagEditor",
        "renameTag",
        "addNewTag",
        "openTagAssignPopup",
        "applyExistingTag",
        "createAndAssignTag",
    ]:
        assert f"export function {name}" in src or f"export async function {name}" in src, \
            f"tag_components.js missing export: {name}"


def test_tag_bar_stamps_data_scope():
    """Every tag-bar rendering path must stamp `data-scope` on its
    elements so the click dispatcher can resolve the scope back. If we
    drop this attribute, the wrong view's re-render fires (or none)."""
    src = read(ROOT / "llm_lens" / "static" / "js" / "tag_components.js")
    # All actionable elements inside the tag bar/editor carry data-scope.
    assert 'data-scope="${scope.name}"' in src
    # Popup roots too — apply/create actions read ctx from the popup.
    assert 'popup.dataset.scope = scope.name' in src


def test_api_has_project_tag_methods():
    src = read(ROOT / "llm_lens" / "static" / "js" / "api.js")
    for method in ("getProjectTags", "setProjectTagLabels", "assignProjectTags", "bulkAssignProjectTag"):
        assert f"{method}:" in src or f"{method} =" in src, f"api.js missing {method}"
