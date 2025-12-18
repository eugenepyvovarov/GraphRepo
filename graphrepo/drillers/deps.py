import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from grep_ast import filename_to_lang
from grep_ast.tsl import get_language, get_parser
from pathspec import PathSpec
from pathspec.patterns.gitwildmatch import GitWildMatchPattern
from py2neo import Graph

import graphrepo.drillers.batch_utils as b_utl
import graphrepo.utils as utl
from graphrepo.config import Config
from graphrepo.logger import Logger

LG = Logger()

SUPPORTED_IMPORT_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".php"}
DEFAULT_IGNORES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".cache",
}
STOPWORDS = {
    "",
    "src",
    "app",
    "apps",
    "lib",
    "dist",
    "build",
    "index",
    "test",
    "tests",
    "spec",
    "tmp",
    "public",
    "assets",
    "vendor",
    "data",
    "static",
}
KEYWORD_LIMIT = 20


@dataclass
class WorkingFile:
    rel_path: str
    abs_path: Path
    ext: str


class DependencyDriller:
    """
    Static dependency and keyword extractor for the working tree.
    """

    def __init__(self, config_path: str, project_id: Optional[str] = None, ignore_file: Optional[str] = None):
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
        self.ignore_spec = self._build_ignore_spec(ignore_file)

    def _build_ignore_spec(self, ignore_file: Optional[str]) -> PathSpec:
        lines: List[str] = []
        default_lines = [f"{p}/" for p in DEFAULT_IGNORES] + list(DEFAULT_IGNORES)
        lines.extend(default_lines)

        gitignore = self.repo_root / ".gitignore"
        if gitignore.exists():
            lines.extend(gitignore.read_text(encoding="utf-8", errors="ignore").splitlines())
        if ignore_file:
            custom = Path(ignore_file)
            if custom.exists():
                lines.extend(custom.read_text(encoding="utf-8", errors="ignore").splitlines())
        return PathSpec.from_lines(GitWildMatchPattern, lines)

    def _should_ignore(self, rel_path: str) -> bool:
        return self.ignore_spec.match_file(rel_path)

    def _iter_working_files(self) -> Iterable[WorkingFile]:
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            rel_dir = os.path.relpath(dirpath, self.repo_root)
            if rel_dir == ".":
                rel_dir = ""

            # prune ignored directories to avoid descending into them
            dirnames[:] = [
                d for d in dirnames if not self._should_ignore(str(Path(rel_dir, d).as_posix()))
            ]

            for filename in filenames:
                rel_path = Path(rel_dir, filename).as_posix()
                if self._should_ignore(rel_path):
                    continue
                ext = Path(filename).suffix
                if ext.lower() not in SUPPORTED_IMPORT_EXTS:
                    continue
                abs_path = Path(dirpath) / filename
                yield WorkingFile(rel_path=rel_path, abs_path=abs_path, ext=ext.lower())

    def _load_known_files(self) -> Dict[str, str]:
        """
        Returns a map of merge_hash -> hash for files in this project.
        """
        data = self.graph.run(
            "MATCH (f:File {project_id: $pid}) RETURN f.merge_hash AS merge_hash, f.hash AS hash",
            pid=self.project_id,
        ).data()
        return {row["merge_hash"]: row["hash"] for row in data if row.get("merge_hash")}

    def _node_text(self, node, code: bytes) -> str:
        return code[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")

    def _extract_imports(self, file: WorkingFile) -> List[str]:
        lang = filename_to_lang(file.rel_path)
        if not lang:
            return []
        try:
            _ = get_language(lang)
            parser = get_parser(lang)
        except Exception:
            return []

        code_bytes = file.abs_path.read_bytes()
        tree = parser.parse(code_bytes)
        root = tree.root_node

        imports: List[str] = []
        if lang in ("javascript", "typescript"):
            imports = self._extract_js_like_imports(root, code_bytes)
        elif lang == "php":
            imports = self._extract_php_imports(root, code_bytes)

        return imports

    def _extract_js_like_imports(self, root, code_bytes: bytes) -> List[str]:
        imports: List[str] = []

        def walk(node):
            if node.type == "import_statement":
                for child in node.children:
                    if child.type == "string":
                        imports.append(self._node_text(child, code_bytes).strip("\"'`"))
            elif node.type == "call_expression":
                if node.children and node.children[0].type == "identifier":
                    ident = self._node_text(node.children[0], code_bytes)
                    if ident == "require":
                        for child in node.children:
                            if child.type == "arguments":
                                for arg in child.children:
                                    if arg.type == "string":
                                        imports.append(self._node_text(arg, code_bytes).strip("\"'`"))
            for child in node.children:
                walk(child)

        walk(root)
        return imports

    def _extract_php_imports(self, root, code_bytes: bytes) -> List[str]:
        imports: List[str] = []

        def walk(node):
            if node.type in (
                "require_expression",
                "require_once_expression",
                "include_expression",
                "include_once_expression",
            ):
                for child in node.children:
                    if child.type == "string":
                        imports.append(self._node_text(child, code_bytes).strip("\"'`"))
            elif node.type == "namespace_use_declaration":
                # Convert namespace to path-like string; may only resolve when a matching file exists.
                name_parts = []
                for child in node.children:
                    if child.type == "namespace_use_clause":
                        name_parts.append(self._node_text(child, code_bytes))
                if name_parts:
                    imports.extend(name_parts)
            for child in node.children:
                walk(child)

        walk(root)
        return imports

    def _resolve_target(self, importer: WorkingFile, target: str, known_paths: Set[str]) -> Optional[str]:
        if not target:
            return None
        if target.startswith(("http://", "https://")):
            return None

        # strip surrounding quotes left behind by parsing fallbacks
        cleaned = target.strip().strip("\"'")
        if not cleaned:
            return None

        if importer.ext == ".php" and "\\" in cleaned and not cleaned.startswith(("./", "/")):
            php_guess = Path(cleaned.replace("\\", "/"))
            candidates = [php_guess.with_suffix(".php"), php_guess / "index.php"]
            for cand in candidates:
                cand_posix = cand.as_posix()
                if cand_posix in known_paths:
                    return cand_posix

        if cleaned.startswith("/"):
            rel_candidate = cleaned[1:]
        else:
            rel_candidate = cleaned

        importer_dir = Path(importer.rel_path).parent
        if importer_dir.as_posix() == ".":
            importer_dir = Path("")

        base_path = (importer_dir / rel_candidate).as_posix()
        candidates = self._candidate_paths(base_path)
        for cand in candidates:
            if cand in known_paths:
                return cand
        return None

    def _candidate_paths(self, rel_path: str) -> List[str]:
        # honor explicit extension first
        candidates = []
        path_obj = Path(rel_path)
        if path_obj.suffix:
            candidates.append(path_obj.as_posix())
        else:
            for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".php"):
                candidates.append(f"{rel_path}{ext}")
            # directory index files
            for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".php"):
                candidates.append(Path(rel_path, f"index{ext}").as_posix())
        return candidates

    def _extract_keywords(self, file: WorkingFile) -> List[str]:
        keywords: List[str] = []

        path_tokens = re.split(r"[\\/._-]+", file.rel_path)
        for tok in path_tokens:
            lower = tok.lower()
            if lower in STOPWORDS or not lower:
                continue
            if lower not in keywords:
                keywords.append(lower)

        try:
            text = file.abs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        snippet = text[:5000]
        ident_patterns = [
            r"class\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"function\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"def\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=",
        ]
        for pat in ident_patterns:
            for match in re.findall(pat, snippet):
                lower = match.lower()
                if lower in STOPWORDS or not lower:
                    continue
                if lower not in keywords:
                    keywords.append(lower)
        return keywords[:KEYWORD_LIMIT]

    def run(self) -> Dict[str, int]:
        """
        Extract static imports and keywords, then index them in Neo4j.
        :returns: summary counts
        """
        known_files = self._load_known_files()
        if not known_files:
            LG.log("No File nodes found for project_id {}".format(self.project_id))
            return {"imports": 0, "keyworded_files": 0}

        working_files = list(self._iter_working_files())
        if not working_files:
            LG.log("No working tree files found under {}".format(self.repo_root))
            return {"imports": 0, "keyworded_files": 0}

        working_paths = {wf.rel_path for wf in working_files}
        file_hashes: Dict[str, Dict[str, str]] = {}
        for wf in working_files:
            file_hashes[wf.rel_path] = utl.get_path_hashes(wf.rel_path, self.project_id)

        imports: Set[Tuple[str, str]] = set()
        relations = []
        keywords_rows = []

        for wf in working_files:
            hashes = file_hashes[wf.rel_path]
            merge_hash = hashes["merge_hash"]
            if merge_hash not in known_files:
                continue

            # Keywords
            kw = self._extract_keywords(wf)
            keywords_rows.append(
                {"merge_hash": merge_hash, "hash": hashes["hash"], "keywords": kw}
            )

            # Imports
            targets = self._extract_imports(wf)
            for target in targets:
                resolved = self._resolve_target(wf, target, working_paths)
                if not resolved:
                    continue
                tgt_hashes = file_hashes.get(resolved)
                if not tgt_hashes:
                    tgt_hashes = utl.get_path_hashes(resolved, self.project_id)
                    file_hashes[resolved] = tgt_hashes
                tgt_merge_hash = tgt_hashes["merge_hash"]
                if tgt_merge_hash not in known_files:
                    continue

                edge_key = (merge_hash, tgt_merge_hash)
                if edge_key in imports:
                    continue
                imports.add(edge_key)
                relations.append(
                    {
                        "src_merge_hash": merge_hash,
                        "src_hash": hashes["hash"],
                        "dst_merge_hash": tgt_merge_hash,
                        "dst_hash": tgt_hashes["hash"],
                    }
                )

        if keywords_rows:
            b_utl.set_file_keywords(self.graph, keywords_rows, self.project_id, self.config.ct.batch_size)
        if relations:
            b_utl.index_imports(self.graph, relations, self.project_id, self.config.ct.batch_size)

        print(
            f"Deps run complete: IMPORTS added={len(relations)}, files keyworded={len(keywords_rows)}"
        )
        return {"imports": len(relations), "keyworded_files": len(keywords_rows)}
