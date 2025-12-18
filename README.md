# GraphRepo ![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square) [![BCH compliance](https://bettercodehub.com/edge/badge/NullConvergence/GraphRepo?branch=develop)](https://bettercodehub.com/)

GraphRepo is a tool for mining software repositories in real time. It indexes Git repositories in Neo4j and implements multiple queries to select and process the repository data.

For a complete description, see the [online documentation](https://graphrepo.readthedocs.io/en/latest/).
<!-- For a [demo](https://github.com/NullConvergence/GraphRepo-Demo) using Jupyter notebooks follow this [link](https://github.com/NullConvergence/GraphRepo-Demo) or see the [video demo](https://www.youtube.com/watch?v=x1ha0fRltGI). -->

<p align="center">
  <img src="https://raw.githubusercontent.com/NullConvergence/GraphRepo/develop/docs/source/GraphRepoSchema.svg">
</p>x

### Working tree dependencies, keywords, and categories

- Static imports/includes are parsed from the working tree (TS/JS/TSX/JSX, PHP) and stored as `(:File)-[:IMPORTS]->(:File)`.
- Files gain a `keywords` list built from path tokens and top-level identifiers.
- Categories are now namespaced under `(:RepoCategory {project_id, name, description, url})` with `(:File)-[:BELONGS_TO_REPO_CATEGORY]->(:RepoCategory)`; an “Other” category per project is always ensured. This avoids collisions with any existing `Category` label you may have.
- Helpers live in `graphrepo/drillers/deps.py` and `graphrepo/drillers/categories.py`. Batch MERGE utilities for the new nodes/edges are in `graphrepo/drillers/batch_utils.py`.

CLI (python -m graphrepo.cli):

```
# Git history drill (existing behaviour)
python -m graphrepo.cli --config examples/configs/graphrepo.yml --run-history

# Static deps + keywords
python -m graphrepo.cli --config examples/configs/graphrepo.yml --run-deps

# Merge categories and file->category links from JSON payloads
python -m graphrepo.cli --config examples/configs/graphrepo.yml \
  --categorize \
  --categories-json categories.json \
  --assignments-json assignments.json

# Generate categories for new routes (requires a category_generator when used programmatically)
python -m graphrepo.cli --config examples/configs/graphrepo.yml \
  --auto-categories --routes routes.json
```

JSON formats:
- categories.json: `[{"name": "Tasks", "description": "...", "url": "/tasks"}, ...]`
- assignments.json: `[{"path": "src/tasks/index.tsx", "category": "Tasks", "confidence": 0.8}, ...]` (you can also pass `merge_hash`/`hash` instead of `path`).
Routes JSON should be an array of route strings (e.g., `["/files", "/tasks"]`). When calling `CategoryManager.auto_categories` directly, provide a `category_generator(routes) -> List[CategorySpec]` that uses your GPT integration.

###  1. Installation & First run

#### 1.1 Prereq
The only requirement is to have Python >=3.5 and Docker installed on your system.

#### 1.2 Install using pip

The production release can be installed using pip:

```
$ pip install graphrepo
```

#### Alternative: Install the development version

Note that the development version may have new, but unreliable or poorly documented features.

Clone the repository and install it in editable mode so GraphRepo can be imported from your environment:

```
$ git clone --recurse-submodules https://github.com/NullConvergence/GraphRepo.git
$ cd GraphRepo/
$ pip install -e .
```


#### 1.3 Run and configure Neo4j

The following instructions assume the Docker daemon is running on your machine:

```
$ docker run -p 7474:7474 -p 7687:7687 -v $HOME/neo4j/data:/data -v $HOME/neo4j/plugins:/plugins  -e NEO4JLABS_PLUGINS=\[\"apoc\"\]   -e NEO4J_AUTH=neo4j/neo4jj neo4j:5
```

Open a browser window and go to [http://localhost:7474](http://localhost:7474). Here you can configure the neo4j password.
The default one is *neo4jj*.

##### Optionally, configure Neo4j to allow larger heap size using the following attributes with the command above:

```
--env NEO4J_dbms_memory_pagecache_size=4g
--env NEO4J_dbms_memory_heap_max__size=4g
```

#### 1.4. Index and vizualize a repo

In order to index a repository, you must clone it on localhost, and point GraphRepo to it. For example:
```
$ mkdir repos
$ cd repos
$ git clone https://github.com/ishepard/pydriller
```

Now enter the [examples](/examples) folder from this repository, and edit the configuration file for PyDriller to reflect the database URL and desired batch size:
```
$ cd ../examples/
$ nano configs/pydriller.yml
```

Afterwards, we can run the script from the examples folder which indexes the repository in Neo4j:

```
$ python -m examples.index_all --config=examples/configs/pydriller.yml
```

Go to [http://localhost:7474](http://localhost:7474) and use the query from 3.1


#### 1.5. Retrieve all data from Neo4j using GraphRepo

Assuming you succeded in step 1.4, use the follwing command to retrieve all indexed data:

```
$ python -m examples.mine_all --config=examples/configs/pydriller.yml
```


### 2. Examples

For a comprehensive introduction and more examples, see the [documentation](https://graphrepo.readthedocs.io/en/latest/examples.html).



### 3. Useful Neo4j queries for the web interface

#### 3.1 Match all nodes in a graph
```
MATCH (n) RETURN n
```


#### 3.2 Delete all nodes and relationships in a graph

```
MATCH (n) DETACH DELETE n;
```

#### 3.2 Delete a limited number commits and relationship

```
MATCH (n:Commit)
// Take the first 100 commits nodes and their rels
WITH n LIMIT 100
DETACH DELETE n
RETURN count(*);
```



This project is enabled by [Pydriller](https://github.com/ishepard/pydriller).
