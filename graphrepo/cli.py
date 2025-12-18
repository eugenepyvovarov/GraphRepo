import argparse
import json
from pathlib import Path
from typing import List, Optional

from graphrepo.drillers import Driller
from graphrepo.drillers.categories import CategoryManager, CategorySpec, FileCategoryAssignment
from graphrepo.drillers.deps import DependencyDriller


def _load_json_list(path: Optional[str]):
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array in {}".format(path))
    return data


def parse_args():
    parser = argparse.ArgumentParser(description="GraphRepo utility CLI")
    parser.add_argument("--config", required=True, help="Path to GraphRepo YAML config")
    parser.add_argument("--project-id", help="Override project_id from config")
    parser.add_argument("--ignore-file", help="Optional gitignore-style file with extra ignores")

    parser.add_argument("--run-history", action="store_true", help="Run git history drill (existing behaviour)")
    parser.add_argument("--run-deps", action="store_true", help="Extract static imports and keywords from working tree")
    parser.add_argument("--categorize", action="store_true", help="Merge categories and file-category assignments")
    parser.add_argument(
        "--auto-categories",
        action="store_true",
        help="Generate categories for missing routes (requires --routes and a category generator in code)",
    )

    parser.add_argument(
        "--routes",
        help="Path to JSON array of route strings (used with --auto-categories)",
    )
    parser.add_argument(
        "--categories-json",
        help="Path to JSON array of categories {name, description, url}",
    )
    parser.add_argument(
        "--assignments-json",
        help="Path to JSON array of file->category assignments {path|merge_hash, category, confidence?}",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.run_history:
        driller = Driller(config_path=args.config)
        try:
            driller.init_db()
        except Exception:
            # db might already be initialized
            pass
        driller.drill_batch()
        driller.merge_all()

    if args.run_deps:
        deps = DependencyDriller(
            config_path=args.config, project_id=args.project_id, ignore_file=args.ignore_file
        )
        deps.run()

    if args.categorize or args.auto_categories:
        manager = CategoryManager(config_path=args.config, project_id=args.project_id)

        category_specs: List[CategorySpec] = []
        for row in _load_json_list(args.categories_json):
            category_specs.append(
                CategorySpec(
                    name=row.get("name", ""),
                    description=row.get("description", ""),
                    url=row.get("url", ""),
                )
            )

        assignments: List[FileCategoryAssignment] = []
        for row in _load_json_list(args.assignments_json):
            assignments.append(
                FileCategoryAssignment(
                    category=row.get("category", ""),
                    path=row.get("path"),
                    confidence=row.get("confidence"),
                    merge_hash=row.get("merge_hash"),
                    hash=row.get("hash"),
                )
            )

        routes = _load_json_list(args.routes) if args.auto_categories else None

        summary = manager.categorize(
            categories=category_specs if category_specs else None,
            assignments=assignments if assignments else None,
            routes=routes,
        )
        print("Category run complete:", summary)


if __name__ == "__main__":
    main()
