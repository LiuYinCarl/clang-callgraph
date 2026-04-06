# AGENTS.md

## Project overview
- `clang-callgraph` is a small Python CLI package for generating call graphs from C/C++ codebases via Clang AST parsing.
- The package entry point is `clang_callgraph:main`, exposed as the `clang-callgraph` console script in `pyproject.toml`.
- Almost all implementation currently lives in a single module: `clang_callgraph/__init__.py`.

## Repository layout
- `pyproject.toml` — Poetry package metadata, dependencies, and console script entry point.
- `README.md` — user-facing installation, usage, and configuration examples.
- `clang_callgraph/__init__.py` — CLI argument parsing, compile database loading, Clang traversal, graph building, and interactive query loop.
- `LICENSE` — project license.

## Tooling and environment
### Observed dependencies
From `pyproject.toml`:
- Python `>=3.6`
- Runtime: `clang>=14.0.0`, `pygments>=2.0.0`, `pyyaml>=5.4.1`
- Dev: `pytest>=8.0.0`

### External system requirements
From `README.md` and code:
- A Clang/libclang 14 installation is expected.
- The code checks for `libclang-14.so` when `--library_path` is provided.
- Typical setup mentioned in `README.md`:
  - `clang-14`
  - `libclang-14-dev`
- For analyzing Make-based C/C++ projects, the README recommends generating `compile_commands.json` with Bear.

## Commands
Only include commands observed in repository files.

### Install
```bash
pip install .
```

### Run the CLI
Via console script after installation:
```bash
clang-callgraph file.cpp
clang-callgraph compile_commands.json
```

Via module from the repo checkout:
```bash
python -m clang_callgraph file.cpp
python -m clang_callgraph compile_commands.json
```

### Tests
Declared dev dependency:
```bash
pytest
```
Current environment note: `pytest` was not installed in the working environment when this file was generated.

### Generate compilation database for Make projects
Documented in `README.md`:
```bash
bear -- make -j4
```

## CLI behavior
Observed in `clang_callgraph/__init__.py`:
- Positional input is either a source file or a `compile_commands.json` file.
- If no database/file argument is passed and `compile_commands.json` exists in the current directory, the tool uses it automatically.
- Supported explicit options:
  - `-x name1,name2` — exclude symbol prefixes
  - `-p path1,path2` — exclude file path prefixes
  - `--cfg <file>` — load YAML config
  - `--lookup <function_name>` — print one call graph without entering REPL
  - `--library_path <dir>` — directory expected to contain `libclang-14.so`
- Any other `-...` arguments are forwarded as Clang args, but only `-I...`, `-std=...`, and `-D...` survive filtering before parsing.
- Default excluded path is `/usr` if `-p` / config excluded paths are not provided.

## YAML config file
Observed in `README.md` and `load_config_file()`:
- Config format is YAML.
- Recognized keys:
  - `clang_args`
  - `excluded_prefixes`
  - `excluded_paths`
  - `library_path`
- `load_config_file()` appends values from YAML onto CLI-derived config lists.
- Because the code does `cfg[k] += data.get(k, [])`, `library_path` is effectively treated like a list in config loading even though `read_args()` initializes it as a string. Be careful when changing this area.

## Code organization and flow
### Main flow
`clang_callgraph/__init__.py` is structured around a few global graphs and a top-level CLI flow:
1. `read_args()` parses CLI input.
2. `load_config_file()` merges YAML config.
3. `analyze_source_files()` loads compilation commands and builds graphs.
4. `main()` either:
   - prints one graph with `--lookup`, or
   - enters an interactive prompt via `ask_and_print_callgraph()`.

### Core global data structures
- `CALLGRAPH = defaultdict(list)` — maps caller names to referenced callees.
- `FULLNAMES = defaultdict(set)` — maps fully-qualified names to display-name variants.
- `REFGRAPH = defaultdict(list)` — reverse references: callee to callers.
- Several UI/query globals are also used (`g_filter_set`, `g_ignore_set`, `g_buffer`, depth globals).

### AST traversal
- `show_info()` recursively walks Clang cursors and populates `CALLGRAPH`, `REFGRAPH`, and `FULLNAMES`.
- `fully_qualified()` and `fully_qualified_pretty()` build symbol names from `semantic_parent` chains.
- Functions/methods/templates are tracked only for selected cursor kinds.
- Call edges are captured from `CursorKind.CALL_EXPR` nodes.

### Output/query modes
Interactive prompt commands from `usage_message` and `ask_and_print_callgraph()`:
- Plain input: search/match or print call graph.
- `? function` — print only branches containing filter keywords.
- `! function` — print graph while skipping ignored keywords.
- `& function` — print reverse-reference graph.
- `@ ...` commands mutate query state (`filter`, `ignore`, `del_fi`, `del_ig`, `depth`, `show`, `reset`).

## Coding patterns and conventions
Observed patterns only:
- Single-module implementation with heavy reliance on module-level mutable globals.
- Recursive tree walking and recursive graph printing functions.
- Type hints are present in many places, but usage is inconsistent:
  - built-in generics like `list[str]`
  - untyped parameters in many helper functions
  - `dict`-shaped configs instead of dataclasses/TypedDicts
- Console output is user-facing and ANSI-colorized using manual escape sequences plus Pygments terminal formatting.
- Error handling is permissive:
  - parse failures are caught broadly and logged with tracebacks
  - diagnostics are printed, but processing continues
- The package script entry point and the `if __name__ == '__main__':` block both call `main()` from the same module.

## Testing status and guidance
- There is currently no `tests/` directory or visible test suite in the repository.
- `pytest` is declared as a dev dependency, so new tests should likely use pytest.
- If adding tests, prefer focused unit tests around:
  - `read_args()` parsing behavior
  - config merging in `load_config_file()`
  - filtering logic in `keep_arg()`
  - graph/query helpers that can run without a real Clang installation
- Integration tests that import `clang.cindex` may require libclang and are likely environment-sensitive.

## Gotchas
- `python -m clang_callgraph --help` does not provide argparse-style help; importing the module immediately requires runtime dependencies such as `pyyaml` and `clang`.
- The current environment used to inspect the repo did not have `pyyaml` installed, so module execution failed before CLI behavior could be exercised.
- `pytest` was also missing from the current environment.
- `analyze_source_files()` reparses each compile command entry with a new `Index.create()` call inside the loop.
- `read_args()` treats unknown dashed arguments as Clang args, but `keep_arg()` later drops anything except `-I`, `-std=`, and `-D` prefixes.
- `load_config_file()` concatenates config values into `cfg[k]`; this is natural for list fields but awkward for `library_path` because the initial type from `read_args()` is a string.
- Excluded paths default to `/usr`, which hides many system headers unless explicitly overridden.
- The readline completion state is global (`complete_list`) and updated only when a match list is produced.

## Guidance for future changes
- Read `clang_callgraph/__init__.py` end to end before changing behavior; most features interact through globals.
- Preserve the CLI options and interactive command syntax documented in `README.md` unless intentionally updating both code and docs.
- If refactoring globals into objects, audit all query/printing helpers together because they share state through module globals.
- Keep README and this file aligned when changing dependencies, command examples, or config keys.
