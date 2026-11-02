"""docs/examples.md, the examples gallery, lists every bundled example and
quotes each plan file exactly (scripts/build_examples_page.py writes it)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "docs" / "examples.md").read_text(encoding="utf-8")
EXAMPLES = sorted((ROOT / "firebridgelab" / "examples").glob("*.toml"))


def test_every_example_is_in_the_gallery():
    assert len(EXAMPLES) == 6
    for path in EXAMPLES:
        assert f"`{path.stem}`" in PAGE, path.stem
        assert f"firebridgelab run firebridgelab/examples/{path.name}" in PAGE


def test_the_gallery_quotes_each_plan_file_exactly():
    for path in EXAMPLES:
        quoted = "\n".join(
            f"    {line}" if line else "" for line in path.read_text().rstrip("\n").splitlines()
        )
        assert quoted in PAGE, f"{path.name} changed; rerun scripts/build_examples_page.py"
