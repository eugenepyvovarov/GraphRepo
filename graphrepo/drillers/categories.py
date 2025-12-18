from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

import graphrepo.drillers.batch_utils as b_utl
import graphrepo.utils as utl
from graphrepo.config import Config
from graphrepo.logger import Logger
from py2neo import Graph

LG = Logger()


@dataclass
class CategorySpec:
    name: str
    description: str = ""
    url: str = ""


@dataclass
class FileCategoryAssignment:
    category: str
    path: Optional[str] = None
    confidence: Optional[float] = None
    merge_hash: Optional[str] = None
    hash: Optional[str] = None


class CategoryManager:
    """
    Manage RepoCategory nodes and BELONGS_TO_REPO_CATEGORY relationships.
    """

    def __init__(self, config_path: str, project_id: Optional[str] = None):
        if not config_path:
            raise FileNotFoundError("config_path is required")
        neo, project = utl.parse_config(config_path)
        if project_id:
            project["project_id"] = project_id
        self.config = Config()
        self.config.configure(**neo, **project)
        self.config.check_config()
        self.repo_root = Path(self.config.ct.repo).resolve()
        self.project_id = self.config.ct.project_id
        self.graph = Graph(
            f"bolt://{self.config.ct.db_url}:{self.config.ct.port}",
            auth=(self.config.ct.db_user, self.config.ct.db_pwd),
        )

    def _load_known_files(self) -> Dict[str, str]:
        data = self.graph.run(
            "MATCH (f:File {project_id: $pid}) RETURN f.merge_hash AS merge_hash, f.hash AS hash",
            pid=self.project_id,
        ).data()
        return {row["merge_hash"]: row["hash"] for row in data if row.get("merge_hash")}

    def _existing_categories(self) -> Dict[str, str]:
        data = self.graph.run(
            "MATCH (c:RepoCategory {project_id: $pid}) RETURN c.name AS name, c.url AS url",
            pid=self.project_id,
        ).data()
        return {row["name"]: row.get("url", "") for row in data}

    def _existing_categories_from_pid(self, pid: str) -> Dict[str, str]:
        data = self.graph.run(
            "MATCH (c:RepoCategory {project_id: $pid}) RETURN c.name AS name, c.url AS url",
            pid=pid,
        ).data()
        return {row["name"]: row.get("url", "") for row in data}

    def ensure_other(self) -> None:
        other = CategorySpec(
            name="Other",
            description="Fallback category for uncategorized files",
            url="/other",
        )
        b_utl.index_categories(
            self.graph, [other.__dict__], self.project_id, self.config.ct.batch_size
        )

    def merge_categories(self, categories: Iterable[CategorySpec]) -> Dict[str, int]:
        self.ensure_other()
        existing = self._existing_categories()
        rows = []
        new_names: Set[str] = set()
        for cat in categories:
            if isinstance(cat, dict):
                cat = CategorySpec(
                    name=cat.get("name", ""),
                    description=cat.get("description", ""),
                    url=cat.get("url", ""),
                )
            if not cat.name:
                continue
            rows.append({"name": cat.name, "description": cat.description, "url": cat.url})
            if cat.name not in existing:
                new_names.add(cat.name)

        if rows:
            b_utl.index_categories(self.graph, rows, self.project_id, self.config.ct.batch_size)
        return {"categories_total": len(rows), "categories_created": len(new_names)}

    def _normalize_assignments(
        self, assignments: Iterable[FileCategoryAssignment], known_files: Dict[str, str]
    ) -> List[Dict]:
        normalized = []
        for assignment in assignments:
            if isinstance(assignment, dict):
                assignment = FileCategoryAssignment(
                    category=assignment.get("category", ""),
                    path=assignment.get("path"),
                    confidence=assignment.get("confidence"),
                    merge_hash=assignment.get("merge_hash"),
                    hash=assignment.get("hash"),
                )
            merge_hash = assignment.merge_hash
            f_hash = assignment.hash
            if not merge_hash and assignment.path:
                hashes = utl.get_path_hashes(assignment.path, self.project_id)
                merge_hash = hashes["merge_hash"]
                f_hash = hashes["hash"]
            if not merge_hash:
                continue
            if merge_hash not in known_files:
                continue
            normalized.append(
                {
                    "merge_hash": merge_hash,
                    "hash": f_hash,
                    "category": assignment.category,
                    "confidence": assignment.confidence,
                }
            )
        return normalized

    def assign_categories(
        self, assignments: Iterable[FileCategoryAssignment], category_project_id: Optional[str] = None
    ) -> Dict[str, int]:
        self.ensure_other()
        known_files = self._load_known_files()
        if not known_files:
            LG.log("No File nodes found for project_id {}".format(self.project_id))
            return {"assigned": 0}

        assignment_list = list(assignments)

        # Ensure categories exist for all assignment targets
        target_cat_pid = category_project_id or self.project_id
        existing = self._existing_categories() if target_cat_pid == self.project_id else self._existing_categories_from_pid(target_cat_pid)
        missing_cats = []
        for assignment in assignment_list:
            if isinstance(assignment, dict):
                cat_name = assignment.get("category", "")
            else:
                cat_name = assignment.category
            if cat_name and cat_name not in existing:
                missing_cats.append(CategorySpec(name=cat_name))
        if missing_cats:
            # Only create in the target category project
            self.merge_categories(missing_cats)

        normalized = self._normalize_assignments(assignment_list, known_files)
        if normalized:
            b_utl.index_file_categories(
                self.graph,
                normalized,
                self.project_id,
                category_project_id=target_cat_pid,
                batch_size=self.config.ct.batch_size,
            )
        print(f"Categorization complete: files classified={len(normalized)}")
        return {"assigned": len(normalized)}

    def auto_categories(
        self,
        routes: Iterable[str],
        category_generator: Optional[Callable[[List[str]], List[CategorySpec]]] = None,
    ) -> Dict[str, int]:
        """
        Generate and persist categories for routes missing in Neo4j.
        Routes should be paths like '/files'.
        """
        existing = self._existing_categories()
        existing_urls = {url for url in existing.values() if url}
        missing_routes = [r for r in routes if r and r not in existing_urls]
        if not missing_routes:
            return {"categories_created": 0}
        if not category_generator:
            raise ValueError("category_generator is required for auto-categories")

        generated = category_generator(missing_routes)
        merged = self.merge_categories(generated)
        print(f"Auto categories complete: created={merged.get('categories_created', 0)}")
        return merged

    def categorize(
        self,
        categories: Optional[Iterable[CategorySpec]] = None,
        assignments: Optional[Iterable[FileCategoryAssignment]] = None,
        routes: Optional[Iterable[str]] = None,
        category_generator: Optional[Callable[[List[str]], List[CategorySpec]]] = None,
        category_project_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        Convenience orchestrator used by the CLI.
        """
        summary = {"categories_total": 0, "categories_created": 0, "assigned": 0}
        if routes is not None:
            auto_res = self.auto_categories(routes, category_generator)
            summary.update(auto_res)
        if categories:
            merge_res = self.merge_categories(categories)
            summary["categories_total"] += merge_res.get("categories_total", 0)
            summary["categories_created"] += merge_res.get("categories_created", 0)
        if assignments:
            assign_res = self.assign_categories(assignments, category_project_id=category_project_id)
            summary["assigned"] = assign_res.get("assigned", 0)
        return summary
