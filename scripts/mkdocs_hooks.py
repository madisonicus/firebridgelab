"""MkDocs hooks for the documentation site (mkdocs.yml).

The site's pages are the repository's own Markdown, written to read well on
GitHub. Two of them live outside docs/: README.md becomes the home page and
DATA-SOURCES.md the data-sources page, added here as generated files so there
is one copy of each. Their links, and every other page's links, are written
relative to where the file sits in the repository; ``on_page_markdown``
rewrites them for the site:

- a link to a file that is a page of the site points at that page;
- a link to anything else in the repository (code, notebooks, examples, the
  licence) points at the file on GitHub.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/madisonicus/firebridgelab"

# Site page -> the repository file it is built from, for files outside docs/.
GENERATED = {"index.md": "README.md", "data-sources.md": "DATA-SOURCES.md"}
REPO_TO_PAGE = {repo: page for page, repo in GENERATED.items()}

# The target of any [text](target) or ![alt](target), including a badge
# [![alt](image)](target); the target stops at whitespace or ")".
LINK = re.compile(r"(\]\()([^)\s]+)(\))")


def on_files(files, config):
    for page, repo in GENERATED.items():
        text = (ROOT / repo).read_text(encoding="utf-8")
        files.append(File.generated(config, page, content=text))
    return files


def _repo_path(src_uri: str) -> str:
    return GENERATED.get(src_uri, f"docs/{src_uri}")


def _rewrite(target: str, page_src: str, docs_pages: set[str]) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/")):
        return target
    path, _, frag = target.partition("#")
    frag = f"#{frag}" if frag else ""
    if not path:
        return target
    here = posixpath.dirname(_repo_path(page_src))
    resolved = posixpath.normpath(posixpath.join(here, path))
    page_dir = posixpath.dirname(page_src)
    if resolved in REPO_TO_PAGE:
        return posixpath.relpath(REPO_TO_PAGE[resolved], page_dir or ".") + frag
    if resolved.startswith("docs/"):
        inside = resolved[len("docs/"):]
        if inside in docs_pages or (ROOT / resolved).is_file():
            return posixpath.relpath(inside, page_dir or ".") + frag
    kind = "tree" if path.endswith("/") or (ROOT / resolved).is_dir() else "blob"
    return f"{REPO_URL}/{kind}/main/{resolved}{frag}"


def on_page_markdown(markdown, page, config, files):
    docs_pages = {f.src_uri for f in files}
    src = page.file.src_uri

    def sub(m):
        return m.group(1) + _rewrite(m.group(2), src, docs_pages) + m.group(3)

    # Leave fenced code alone.
    out, fence = [], False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        out.append(line if fence else LINK.sub(sub, line))
    return "\n".join(out)
