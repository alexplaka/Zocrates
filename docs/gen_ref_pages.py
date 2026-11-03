"""Generate the code reference pages and navigation."""

from pathlib import Path
import mkdocs_gen_files

nav = mkdocs_gen_files.Nav()
nav[("API Reference",)] = "index.md"

src = Path(__file__).resolve().parents[1] / "zocrates"

for path in sorted(src.rglob("*.py")):
    path = path.relative_to(src.parent)
    module_path = path.with_suffix("")
    doc_path = path.with_suffix(".md")
    full_doc_path = Path("reference", doc_path)

    parts = tuple(module_path.parts)

    if parts[-1] == "__init__":
        parts = parts[:-1]
        doc_path = doc_path.with_name("index.md")
        full_doc_path = full_doc_path.with_name("index.md")
    elif parts[-1] == "__main__":
        continue

    if not parts:
        continue

    nav[parts] = doc_path.as_posix()

    with mkdocs_gen_files.open(full_doc_path, "w") as fd:
        ident = ".".join(parts)
        fd.write(f"::: {ident}\n")

    mkdocs_gen_files.set_edit_path(full_doc_path, Path("..") / path)

with mkdocs_gen_files.open("reference/index.md", "w") as fd:
    fd.write(
        "# API Reference\n\n"
        "Welcome to the Zocrates Python API Reference documentation.\n\n"
        "Select a module from the sidebar navigation or start with the top-level [`zocrates`](zocrates/index.md) package.\n"
    )

with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())
