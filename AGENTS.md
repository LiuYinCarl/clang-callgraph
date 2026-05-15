# AGENTS.md

## Project Overview

`clang-callgraph` is a Python CLI tool that uses libclang to generate interactive call graphs from C/C++ codebases. It parses source files through a `compile_commands.json` compilation database, builds a call graph in memory, then drops into an interactive REPL for querying.

## Build & Run

- **Install**: `pip install .` (uses Poetry build system via `pyproject.toml`)
- **Run**: `clang-callgraph <file.cpp | compile_commands.json | directory> [options] [clang args...]`
- **Run (without install)**: `python -m clang_callgraph <file.cpp | compile_commands.json | directory>`
- **Help**: `clang-callgraph -h` / `clang-callgraph --help`
- **Clear cache**: `clang-callgraph --clear-cache`
- **Syntax check**: `python3 -m py_compile clang_callgraph/__init__.py`
- **Dev dependency**: `pytest>=8.0.0` declared in `pyproject.toml`; no test suite exists yet.

## Architecture

The entire application lives in a single file: `clang_callgraph/__init__.py` (~730 lines).

### Data Flow

1. `main()` → `read_args()` parses CLI args
2. `load_config_file()` merges YAML config if `--cfg` provided
3. `analyze_source_files()` iterates compilation units, spawns a libclang `TranslationUnit` per file, recursively walks the AST via `show_info()`
4. `show_info()` populates three global dicts: `CALLGRAPH`, `REFGRAPH`, `FULLNAMES`
5. REPL loop (`ask_and_print_callgraph()`) accepts queries and renders subgraphs

### Key Global State

| Variable | Purpose |
|---|---|
| `CALLGRAPH` | `defaultdict(list)`: caller → list of `CallTarget` objects |
| `REFGRAPH` | `defaultdict(list)`: callee → list of caller display names |
| `FULLNAMES` | `defaultdict(set)`: spelling-qualified name → set of display names |
| `CallTarget` | Serializable value-object. Fields: `displayname`, `virtual`, `pure_virtual`, `fq_pretty`, `fq`. `__eq__`/`__hash__` on `fq_pretty`. |
| `g_fullname_keys` | Sorted FULLNAMES keys for `bisect` prefix lookup |
| `g_buffer` | Accumulates output lines before flushing to stdout |
| `g_filter_set` / `g_ignore_set` | Keywords for inclusive/exclusive filtering |
| `g_depth` / `MAX_DEPTH` | Recursion depth limit (default 15) |
| `_FORMATTER` / `_CLEXER` | Module-level pygments singletons |
| `complete_list` | Readline tab-completion candidates |
| `_PROGRESS_*` | Progress bar state for stderr during parsing |

## Dependencies

- **Hard requirement**: `libclang-14` (`apt install libclang-14-dev` on Ubuntu)
- **Python**: `clang>=14.0.0`, `pygments>=2.0.0`, `pyyaml>=5.4.1`
- Python ≥ 3.6

## CLI & REPL Reference

### CLI Options

| Flag | Purpose |
|---|---|
| `-x prefix1,prefix2` | Exclude symbols by name prefix |
| `-p path1,path2` | Exclude symbols by file path prefix (default: `/usr`) |
| `--cfg config.yml` | YAML config file |
| `--lookup func_name` | Non-interactive: print callgraph and exit |
| `--library_path /path` | Path to `libclang-14.so` |
| `--clear-cache` | Remove all cached parse results |
| `-h, --help` | Show help |

### REPL Commands

| Input | Action |
|---|---|
| `func_name` | Fuzzy match or print call graph |
| `! func_name` | Call graph minus ignored keywords |
| `? func_name` | Call graph filtered to matching keywords |
| `& func_name` | Reverse reference graph (who calls this) |
| `@ ignore kw1 kw2` | Add ignore keywords |
| `@ filter kw1 kw2` | Add filter keywords |
| `@ del_ig kw1 kw2` | Remove ignore keywords |
| `@ del_fi kw1 kw2` | Remove filter keywords |
| `@ depth N` | Set print depth (1–14) |
| `@ show` | Display current config |
| `@ reset` | Clear filters, reset depth |

## Gotchas

- **Default db**: If no db given and `compile_commands.json` exists in CWD, it's used automatically. Directories look for `compile_commands.json` inside.
- **Parse errors are non-fatal**: Continues parsing remaining files.
- **`fully_qualified` vs `fully_qualified_pretty`**: `spelling` vs `displayname`. CALLGRAPH keys are display names; FULLNAMES maps spelling→display names.
- **CALLGRAPH lookup fallback**: `print_calls()` tries `ct.fq_pretty` first, then `ct.fq`. Both are needed.
- **Cursor objects are NOT hashable**: `Cursor.__hash__` is `None`. `CallTarget` wraps cursors with proper `__hash__`/`__eq__`.
- **Parse cache**: `~/.cache/clang-callgraph/<sha256>.pickle`. Use `--clear-cache` to force re-parse.
- **Single Index**: One `Index` object reused for all translation units.
- **Progress bar**: Stderr shows `N/M  filepath` during fresh parse.
- **Readline**: Guarded import with `pyreadline3` fallback. History at `~/.clang_callgraph_history`.
- **SIGINT**: Caught — prints "user exit." and terminates.
- **Colors**: Raw ANSI escape sequences — `C_GREEN`, `C_RED`, `C_YELLOW`, `C_RESET`.

## Performance Optimizations

| Optimization | Impact |
|---|---|
| Pygments singletons | No per-line object allocation |
| CALLGRAPH/REFGRAPH dedup | Single edge per function pair |
| FULLNAMES prefix index | O(log n) fuzzy lookup |
| Readline completer caching | O(n) per tab-press |
| Readline import guarding | Cross-platform |
| Parse cache (pickle) | Near-instant subsequent runs |
| Single Index reuse | Less libclang overhead |
| Progress bar | Live feedback during parse |
| Shared traversal helpers | `_recurse_ct`, `_depth_guard` |
