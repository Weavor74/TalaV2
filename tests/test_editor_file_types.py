"""
The editor stops treating every file as text.

Steven's original intent for this surface: *"Her original usage intent was a
method to edit files and documents. Be them txt, images or whatever. This
included code."* (UI-114)

It was a `<textarea>`. An image could not be shown at all -- and worse, opening
one read it as UTF-8, filled the editor with replacement characters, and Save
wrote those back over the file. The file was destroyed by looking at it and
pressing Save. The classification below exists mainly to stop that; showing
images is the part that was asked for.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

INDEX = Path("renderer/index.html")
RENDERER = Path("renderer/renderer.js")


@pytest.fixture(scope="module")
def js():
    return RENDERER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


# -- classification, evaluated for real ---------------------------------------

CASES = [
    ("notes.txt", "text"),
    ("README.md", "text"),
    ("server.py", "text"),
    ("renderer.js", "text"),
    ("Makefile", "text"),
    ("Dockerfile", "text"),
    ("photo.png", "image"),
    ("photo.JPG", "image"),
    ("anim.gif", "image"),
    ("icon.svg", "text"),
    ("archive.zip", "binary"),
    ("model.safetensors", "binary"),
    ("app.exe", "binary"),
    ("deep/path/main.rs", "text"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_files_are_classified_correctly(tmp_path):
    """Runs the real helpers out of renderer.js rather than asserting on its
    source, because a classifier that is only asserted about is not tested."""
    src = RENDERER.read_text(encoding="utf-8")
    start = src.index("const EDITOR_IMAGE_TYPES")
    end = src.index("const TabManager = {")
    helpers = src[start:end]

    script = tmp_path / "check.js"
    script.write_text(
        helpers
        + textwrap.dedent(f"""
        const cases = {json.dumps(CASES)};
        const out = cases.map(([p, want]) => [p, editorFileKind(p), want]);
        console.log(JSON.stringify(out));
        """),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr

    wrong = [
        f"{path}: got {got}, expected {want}"
        for path, got, want in json.loads(result.stdout)
        if got != want
    ]
    assert not wrong, wrong


def test_an_unknown_extension_is_binary_not_text(js):
    """Guessing wrong towards text corrupts files; guessing wrong towards
    binary only means the user has to tell us it is text."""
    block = js.split("function editorFileKind(", 1)[1][:700]
    assert "return 'binary';" in block
    # the fallthrough must be binary, so 'binary' is the last return
    returns = re.findall(r"return '(\w+)';", block)
    assert returns[-1] == "binary"


# -- the save guard, which is the part that prevents data loss ----------------

def test_saving_a_non_text_file_is_refused(js):
    block = js.split("async saveActiveFile()", 1)[1][:900]
    assert "tab.readOnly" in block
    assert "'image'" in block and "'binary'" in block
    assert "Refusing to save" in block
    # and it must refuse before touching the buffer
    assert block.index("Refusing to save") < block.index("const data =")


def test_a_failed_read_disables_saving(js):
    """A failed read leaves an error message in the buffer; saving it would
    write that message over the file."""
    block = js.split("async loadTabFileContent(tab)", 1)[1][:3000]
    assert block.count("tab.readOnly = true") >= 2


def test_read_only_is_decided_once_at_load(js):
    """Recomputed at save time it could disagree with what the badge showed."""
    block = js.split("async loadTabFileContent(tab)", 1)[1][:900]
    assert "tab.fileKind = kind" in block
    assert "tab.readOnly = kind !== 'text'" in block


# -- images -------------------------------------------------------------------

def test_the_view_has_an_image_pane(html):
    view = html.split('id="view-editor"', 1)[1].split("</section>", 1)[0]
    assert 'id="editor-image-pane"' in view
    assert 'id="editor-image"' in view


def test_images_are_read_as_base64_not_text(js):
    """file:read already takes an encoding, so carrying bytes safely needed no
    new IPC channel."""
    block = js.split("async loadImageIntoEditor(tab)", 1)[1][:1400]
    assert "encoding: 'base64'" in block
    assert "data:${mime};base64," in block


def test_the_image_mime_type_comes_from_the_extension(js):
    block = js.split("async loadImageIntoEditor(tab)", 1)[1][:1400]
    assert "EDITOR_IMAGE_TYPES[ext]" in block


def test_an_unreadable_image_says_so(js):
    block = js.split("async loadImageIntoEditor(tab)", 1)[1][:1600]
    assert "Could not read the image" in block


# -- what the panel tells the user --------------------------------------------

def test_the_editor_says_what_kind_of_file_is_open(html, js):
    view = html.split('id="view-editor"', 1)[1].split("</section>", 1)[0]
    assert 'id="editor-file-badge"' in view
    assert 'id="editor-file-kind"' in view
    assert "setEditorBadge" in js


def test_code_files_are_named_by_language(js):
    """"This included code" -- at minimum the editor should know what it is
    looking at, even without syntax highlighting."""
    assert "EDITOR_LANGUAGES" in js
    block = js.split("const EDITOR_LANGUAGES", 1)[1][:600]
    for language in ("Python", "JavaScript", "Rust", "SQL"):
        assert language in block


def test_a_binary_file_explains_itself(html, js):
    view = html.split('id="view-editor"', 1)[1].split("</section>", 1)[0]
    assert 'id="editor-binary-pane"' in view
    assert "cannot corrupt it" in view
    assert "not a text file" in js


def test_only_one_pane_shows_at_a_time(js):
    """Two visible panes would put a stale textarea behind an image."""
    block = js.split("showEditorPane(which)", 1)[1][:700]
    for pane in ("text", "preview", "image", "binary"):
        assert f"{pane}:" in block
    assert "'none'" in block
