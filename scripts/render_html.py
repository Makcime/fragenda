from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from fragenda.html_export import (
    generate_class_pdfs,
    generate_scope_pdfs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Smartschool timetable exports to styled PDF schedules using HTML/Jinja templates.",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the Smartschool Excel export (expects the 'input' sheet).",
    )
    parser.add_argument(
        "--sheet",
        default="input",
        help="Worksheet name containing the timetable data.",
    )
    parser.add_argument(
        "--semver",
        required=True,
        help="Version tag used in PDF footer metadata (e.g., '2025-2026-v0.8').",
    )
    parser.add_argument(
        "--scope",
        choices=("classe", "pro", "local"),
        default="classe",
        help="Grouping level for exports ('classe', 'pro', or 'local').",
    )
    parser.add_argument(
        "--template",
        default=Path("templates/html/schedule.html.j2"),
        type=Path,
        help="Path to the Jinja2 HTML template.",
    )
    parser.add_argument(
        "--template-root",
        default=Path("templates/html"),
        type=Path,
        help="Template search directory passed to Jinja2.",
    )
    parser.add_argument(
        "--output-dir",
        default=Path("output/html"),
        type=Path,
        help="Directory where generated PDFs will be written.",
    )
    parser.add_argument(
        "--include-general",
        action="store_true",
        default=False,
        help="Include general-class rows when emitting subgroup sheets (classe scope only).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    df = pd.read_excel(args.input, sheet_name=args.sheet)

    if args.scope == "classe":
        generate_class_pdfs(
            dataframe=df,
            semver=args.semver,
            template_dir=args.template_root,
            template_name=args.template.name,
            output_dir=args.output_dir,
            include_general=args.include_general,
        )
    else:
        generate_scope_pdfs(
            dataframe=df,
            scope=args.scope,
            semver=args.semver,
            template_dir=args.template_root,
            template_name=args.template.name,
            output_dir=args.output_dir,
        )


if __name__ == "__main__":
    main()
