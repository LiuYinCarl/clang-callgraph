#!/usr/bin/env python3

import hashlib
import json
import os
import readline
import shlex
import shutil
import signal
import sys
import time
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from pprint import pprint

import yaml
from clang.cindex import Config, CursorKind, Index, TranslationUnit
from pygments import highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import CLexer


"""
Dumps a callgraph of a function in a codebase
usage: callgraph.py file.cpp|compile_commands.json [-x exclude-list] [extra clang args...]
The easiest way to generate the file compile_commands.json for any make based
compilation chain is to use Bear and recompile with `bear make`.

When running the python script, after parsing all the codebase, you are
prompted to type in the function's name for which you wan to obtain the
callgraph
"""

CALLGRAPH = defaultdict(list)
FULLNAMES = defaultdict(set)
REFGRAPH  = defaultdict(list) # after_main: [main, exit, ...]
CACHE_DIR = Path.home() / '.cache' / 'clang-callgraph'

g_max_print_depth: int = 15
g_print_depth: int = 15

g_filter_set: set = set()
g_ignore_set: set = set()
g_buffer: list = []

ctrl_yellow: str = '\033[033m'
ctrl_green : str = '\033[032m'
ctrl_red   : str = '\033[031m'
ctrl_reset : str = '\033[0m'

INTERESTING_CURSOR_KINDS = {
    CursorKind.FUNCTION_TEMPLATE,
    CursorKind.CXX_METHOD,
    CursorKind.FUNCTION_DECL,
    CursorKind.CALL_EXPR,
}


# signal

def signal_handler(sig, frame) -> None:
    print("user exit.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


# readline helper

complete_list: list[str] = []

def complete(text: str, state: int) -> str|None:
    options: list = [c for c in complete_list if c.startswith(text)]
    return options[state] if state < len(options) else None

readline.parse_and_bind("tab: complete")
readline.set_completer(complete)

def set_complete_list(l: list[str]):
    global complete_list
    complete_list = l


# buffer

def buffer_append(msg: str):
    g_buffer.append(msg)


def buffer_flush(need_len_info: bool=False):
    if need_len_info:
        msg: str = f"{ctrl_green}[total lines: {len(g_buffer)}]{ctrl_reset}"
        buffer_append(msg)
    for line in g_buffer:
        print(line)
    g_buffer.clear()


def buffer_clear():
    g_buffer.clear()


def clear_cache() -> int:
    if not CACHE_DIR.exists():
        return 0

    count = 0
    for cache_file in CACHE_DIR.glob('*.json'):
        if cache_file.is_file():
            cache_file.unlink()
            count += 1

    for child in CACHE_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
    return count


def get_diag_info(diag):
    location = diag.location
    file_name = location.file.name if location.file is not None else None
    return {
        'severity': diag.severity,
        'location': {
            'file': file_name,
            'line': location.line,
            'column': location.column,
            'offset': location.offset,
        },
        'spelling': diag.spelling,
        'ranges': [
            {
                'start': {
                    'line': item.start.line,
                    'column': item.start.column,
                    'offset': item.start.offset,
                },
                'end': {
                    'line': item.end.line,
                    'column': item.end.column,
                    'offset': item.end.offset,
                },
            }
            for item in diag.ranges
        ],
        'fixits': [
            {
                'value': item.value,
                'range': {
                    'start': {
                        'line': item.range.start.line,
                        'column': item.range.start.column,
                        'offset': item.range.start.offset,
                    },
                    'end': {
                        'line': item.range.end.line,
                        'column': item.range.end.column,
                        'offset': item.range.end.offset,
                    },
                },
            }
            for item in diag.fixits
        ],
    }


def fully_qualified(c) -> str:
    if c is None:
        return ''
    elif c.kind == CursorKind.TRANSLATION_UNIT:
        return ''
    else:
        res = fully_qualified(c.semantic_parent)
        if res != '':
            return res + '::' + c.spelling
        return c.spelling


def fully_qualified_pretty(c) -> str:
    if c is None:
        return ''
    elif c.kind == CursorKind.TRANSLATION_UNIT:
        return ''
    else:
        res = fully_qualified(c.semantic_parent)
        if res != '':
            return res + '::' + c.displayname
        return c.displayname


def cursor_file_name(node) -> str|None:
    start = node.extent.start
    file_obj = start.file
    if file_obj is None:
        return None
    return file_obj.name


def is_excluded(node, xfiles, xprefs) -> bool:
    node_file: str | None = cursor_file_name(node)
    if node_file is None:
        return False

    for xf in xfiles:
        if node_file.startswith(xf):
            return True

    fqp: str = fully_qualified_pretty(node)

    for xp in xprefs:
        if fqp.startswith(xp):
            return True

    return False


def append_refgraph_value(refgraph: dict, ref_pretty: str, cur_pretty: str) -> None:
    values = refgraph[ref_pretty]
    if cur_pretty not in values:
        values.append(cur_pretty)


def show_info(node, xfiles, xprefs, cur_fun=None) -> None:
    node_kind = node.kind

    if node_kind == CursorKind.FUNCTION_TEMPLATE:
        if not is_excluded(node, xfiles, xprefs):
            cur_fun = node
            fullname = fully_qualified(cur_fun)
            FULLNAMES[fullname].add(fully_qualified_pretty(cur_fun))

    elif node_kind == CursorKind.CXX_METHOD or node_kind == CursorKind.FUNCTION_DECL:
        if not is_excluded(node, xfiles, xprefs):
            cur_fun = node
            fullname = fully_qualified(cur_fun)
            FULLNAMES[fullname].add(fully_qualified_pretty(cur_fun))

    elif node_kind == CursorKind.CALL_EXPR and cur_fun is not None:
        referenced = node.referenced
        if referenced and not is_excluded(referenced, xfiles, xprefs):
            ref_pretty = fully_qualified_pretty(referenced)
            cur_pretty = fully_qualified_pretty(cur_fun)
            append_refgraph_value(REFGRAPH, ref_pretty, cur_pretty)
            qualified_name = fully_qualified(referenced)
            match_name = referenced.displayname or qualified_name or ref_pretty
            CALLGRAPH[cur_pretty].append({
                'pretty': pretty_print(referenced),
                'match_name': match_name,
                'qualified_name': qualified_name,
                'next': ref_pretty,
            })

    for c in node.get_children():
        show_info(c, xfiles, xprefs, cur_fun)


def pretty_print(n) -> str:
    v = ''
    if n.is_virtual_method():
        v = ' virtual'
    if n.is_pure_virtual_method():
        v = ' = 0'
    return fully_qualified_pretty(n) + v


def code_color_pretty(code: str) -> str:
    formatter: TerminalFormatter = TerminalFormatter()
    highlight_code = highlight(code, CLexer(), formatter)
    return highlight_code.rstrip()


def print_refs(fun_name: str, so_far: list, depth: int=0) -> None:
    if depth >= g_print_depth:
        return
    if depth >= g_max_print_depth:
        buffer_append('...<too deep>...')
        return
    if fun_name in REFGRAPH:
        for f in REFGRAPH[fun_name]:
            color_code = code_color_pretty(f)
            buffer_append(f'{ctrl_red}|{ctrl_reset}  ' * depth + f'{ctrl_red}|--{ctrl_reset}' + color_code)
            if f in so_far:
                continue
            so_far.append(f)
            if f in REFGRAPH:
                print_refs(f, so_far, depth+1)

def print_calls(fun_name: str, so_far: list, depth: int=0) -> None:
    if depth >= g_print_depth:
        return
    if depth >= g_max_print_depth:
        buffer_append('...<too deep>...')
        return
    if fun_name in CALLGRAPH:
        for f in CALLGRAPH[fun_name]:
            color_code = code_color_pretty(f['pretty'])
            buffer_append(f'{ctrl_green}|{ctrl_reset}  ' * (depth) + f'{ctrl_green}|--{ctrl_reset}' + color_code)

            next_fun = f['next']
            fallback_fun = f.get('qualified_name', next_fun)
            recurse_key = next_fun if next_fun in CALLGRAPH else fallback_fun
            if recurse_key in so_far:
                continue
            so_far.append(recurse_key)
            if recurse_key in CALLGRAPH:
                print_calls(recurse_key, so_far, depth + 1)


def filter_calls(func_name: str, call_stack: list, so_far: list, depth: int=0) -> None:
    if depth >= g_print_depth:
        return
    if depth >= g_max_print_depth:
        buffer_append('...<too deep>...')
        return

    if func_name in CALLGRAPH:
        for f in CALLGRAPH[func_name]:
            color_code: str = code_color_pretty(f['pretty'])
            line: str = f'{ctrl_green}|{ctrl_reset}  ' * (depth) + f'{ctrl_green}|--{ctrl_reset}' + color_code
            call_stack.append(line)

            for kw in g_filter_set:
                if kw in f.get('match_name', '') or kw in f.get('qualified_name', '') or kw in f['pretty']:
                    for line in call_stack:
                        buffer_append(line)
                    break

            next_fun = f['next']
            fallback_fun = f.get('qualified_name', next_fun)
            recurse_key = next_fun if next_fun in CALLGRAPH else fallback_fun
            if recurse_key in so_far:
                call_stack.pop()
                continue
            so_far.append(recurse_key)
            if recurse_key in CALLGRAPH:
                filter_calls(recurse_key, call_stack, so_far, depth+1)
            call_stack.pop()


def ignore_calls(func_name: str, so_far: list, depth: int=0) -> None:
    if depth >= g_print_depth:
        return
    if depth >= g_max_print_depth:
        buffer_append('...<too deep>...')
        return

    if func_name in CALLGRAPH:
        for f in CALLGRAPH[func_name]:
            hit_ignore: bool = False
            for kw in g_ignore_set:
                if kw in f.get('match_name', '') or kw in f.get('qualified_name', '') or kw in f['pretty']:
                    hit_ignore = True
                    break
            if hit_ignore:
                continue

            color_code: str = code_color_pretty(f['pretty'])
            line: str = f'{ctrl_green}|{ctrl_reset}  ' * (depth) + f'{ctrl_green}|--{ctrl_reset}' + color_code
            buffer_append(line)

            next_fun = f['next']
            fallback_fun = f.get('qualified_name', next_fun)
            recurse_key = next_fun if next_fun in CALLGRAPH else fallback_fun
            if recurse_key in so_far:
                continue
            so_far.append(recurse_key)
            if recurse_key in CALLGRAPH:
                ignore_calls(recurse_key, so_far, depth + 1)


def check_libclang_exists(directory: str) -> bool:
    """ Find if libclang-14.so exists in directory.
    """
    if not os.path.exists(directory):
        return False

    lib_path: str = os.path.join(directory, "libclang-14.so")
    return os.path.isfile(lib_path)


def read_compile_commands(filename: str) -> list:
    if os.path.isdir(filename):
        compdb_path = os.path.join(filename, 'compile_commands.json')
        if os.path.exists(compdb_path):
            filename = compdb_path
        else:
            raise FileNotFoundError(f'compile_commands.json not found in directory: {filename}')

    if filename.endswith('.json'):
        with open(filename) as compdb:
            return json.load(compdb)
    else:
        return [{'command': '', 'file': filename}]


def read_args(args: list) -> dict:
    db = None
    clang_args: list = []
    excluded_prefixes: list = []
    excluded_paths: list = []
    config_filename: str = ""
    lookup: str = ""
    library_path: str = ""
    jobs: int = max(1, min(16, (os.cpu_count() or 1)))
    quiet: bool = True
    clear_cache_first: bool = False
    i: int = 0
    while i < len(args):
        if args[i] == '-x':
            i += 1
            excluded_prefixes += args[i].split(',')
        elif args[i] == '-p':
            i += 1
            excluded_paths += args[i].split(',')
        elif args[i] == '--cfg':
            i += 1
            config_filename = args[i]
        elif args[i] == '--lookup':
            i += 1
            lookup = args[i]
        elif args[i] == '--library_path':
            i += 1
            library_path = args[i]
        elif args[i] == '-j' or args[i] == '--jobs':
            i += 1
            jobs = max(1, int(args[i]))
        elif args[i] == '--quiet':
            quiet = True
        elif args[i] == '--verbose':
            quiet = False
        elif args[i] == '--clear-cache':
            clear_cache_first = True
        elif args[i][0] == '-':
            clang_args.append(args[i])
        else:
            db = args[i]
        i += 1

    if len(excluded_paths) == 0:
        excluded_paths.append('/usr')

    # try to use compile_commands.json as default db
    if not db and os.path.exists('compile_commands.json'):
        db = 'compile_commands.json'

    return {
        'db': db,
        'clang_args': clang_args,
        'excluded_prefixes': excluded_prefixes,
        'excluded_paths': excluded_paths,
        'config_filename': config_filename,
        'lookup': lookup,
        'ask': (not lookup),
        'library_path': library_path,
        'jobs': jobs,
        'quiet': quiet,
        'clear_cache_first': clear_cache_first,
    }


def load_config_file(cfg: dict) -> None:
    if cfg['config_filename']:
        with open(cfg['config_filename'], 'r') as yamlfile:
            data = yaml.load(yamlfile, Loader=yaml.FullLoader)
            keys = ('clang_args', 'excluded_prefixes', 'excluded_paths', 'library_path')
            for k in keys:
                cfg[k] += data.get(k, [])


def keep_arg(x: str) -> bool:
    return x.startswith('-I') or x.startswith('-std=') or x.startswith('-D')


def normalize_compile_command(cmd: dict, extra_clang_args: list[str], db_fingerprint: str='') -> dict:
    file_path = cmd['file']
    directory = cmd.get('directory', '')
    if 'arguments' in cmd:
        arguments = cmd['arguments']
    else:
        arguments = shlex.split(cmd['command'])

    clang_args = [x for x in arguments if keep_arg(x)] + extra_clang_args
    raw_command = json.dumps(cmd, sort_keys=True, separators=(',', ':'))
    return {
        'file': file_path,
        'directory': directory,
        'clang_args': clang_args,
        'command_fingerprint': hashlib.sha256(raw_command.encode()).hexdigest(),
        'db_fingerprint': db_fingerprint,
    }


def merge_partial_graphs(results: list[dict]) -> None:
    for result in results:
        for key, values in result['callgraph'].items():
            CALLGRAPH[key].extend(values)
        for key, values in result['fullnames'].items():
            FULLNAMES[key].update(values)
        for key, values in result['refgraph'].items():
            REFGRAPH[key].extend(values)


def normalize_graph_lists(result: dict) -> dict:
    result['callgraph'] = {key: list(values) for key, values in result['callgraph'].items()}
    result['fullnames'] = {key: sorted(values) for key, values in result['fullnames'].items()}
    result['refgraph'] = {key: list(values) for key, values in result['refgraph'].items()}
    return result


def file_signature(file_path: str) -> dict:
    stat = os.stat(file_path)
    return {
        'path': file_path,
        'mtime_ns': stat.st_mtime_ns,
        'size': stat.st_size,
    }


def read_db_fingerprint(db_path: str) -> str:
    if not db_path.endswith('.json'):
        return ''
    stat = os.stat(db_path)
    payload = {
        'path': db_path,
        'mtime_ns': stat.st_mtime_ns,
        'size': stat.st_size,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def dependency_signatures_from_result(result: dict) -> list[dict]:
    return result.get('dependencies', [])


def cache_key_for_task(task: dict) -> str:
    payload = {
        'file': file_signature(task['file']),
        'directory': task['directory'],
        'clang_args': task['clang_args'],
        'excluded_paths': task['excluded_paths'],
        'excluded_prefixes': task['excluded_prefixes'],
        'command_fingerprint': task.get('command_fingerprint', ''),
        'db_fingerprint': task.get('db_fingerprint', ''),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def cache_path_for_task(task: dict) -> Path:
    return CACHE_DIR / f"{cache_key_for_task(task)}.json"


def load_cached_result(task: dict) -> dict | None:
    cache_path = cache_path_for_task(task)
    if not cache_path.exists():
        return None
    try:
        with cache_path.open() as cache_file:
            cached = json.load(cache_file)
    except Exception:
        return None

    try:
        for dependency in dependency_signatures_from_result(cached):
            if file_signature(dependency['path']) != dependency:
                return None
    except Exception:
        return None

    return cached


def save_cached_result(task: dict, result: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = cache_path_for_task(task)
    normalized = normalize_graph_lists(dict(result))
    with cache_path.open('w') as cache_file:
        json.dump(normalized, cache_file, separators=(',', ':'))


def parse_translation_unit(task: dict) -> dict:
    index: Index = Index.create()
    file_path = task['file']
    clang_args = task['clang_args']
    xfiles = task['excluded_paths']
    xprefs = task['excluded_prefixes']
    working_directory = task['directory'] or None
    original_cwd = os.getcwd()

    partial_callgraph = defaultdict(list)
    partial_fullnames = defaultdict(set)
    partial_refgraph = defaultdict(list)

    def append_partial_ref(ref_pretty: str, cur_pretty: str) -> None:
        values = partial_refgraph[ref_pretty]
        if cur_pretty not in values:
            values.append(cur_pretty)

    def walk_function(node) -> None:
        if is_excluded(node, xfiles, xprefs):
            return

        fullname = fully_qualified(node)
        current_pretty = fully_qualified_pretty(node)
        partial_fullnames[fullname].add(current_pretty)

        stack: list = [node]
        while stack:
            current = stack.pop()
            for child in current.get_children():
                child_kind = child.kind
                if child_kind == CursorKind.CALL_EXPR:
                    referenced = child.referenced
                    if referenced and not is_excluded(referenced, xfiles, xprefs):
                        ref_pretty = fully_qualified_pretty(referenced)
                        append_partial_ref(ref_pretty, current_pretty)
                        qualified_name = fully_qualified(referenced)
                        match_name = referenced.displayname or qualified_name or ref_pretty
                        partial_callgraph[current_pretty].append({
                            'pretty': pretty_print(referenced),
                            'match_name': match_name,
                            'qualified_name': qualified_name,
                            'next': ref_pretty,
                        })
                elif child_kind == CursorKind.FUNCTION_TEMPLATE or child_kind == CursorKind.CXX_METHOD or child_kind == CursorKind.FUNCTION_DECL:
                    continue
                stack.append(child)

    def walk_root(node) -> None:
        node_kind = node.kind
        if node_kind == CursorKind.FUNCTION_TEMPLATE or node_kind == CursorKind.CXX_METHOD or node_kind == CursorKind.FUNCTION_DECL:
            walk_function(node)
            return

        for child in node.get_children():
            walk_root(child)

    try:
        if working_directory:
            os.chdir(working_directory)
        tu: TranslationUnit = index.parse(file_path, clang_args, options=TranslationUnit.PARSE_INCOMPLETE, unsaved_files=None)
        diagnostics = []
        for d in tu.diagnostics:
            if d.severity == d.Error or d.severity == d.Fatal:
                diagnostics.append(get_diag_info(d))
        dependencies = []
        seen_dependency_paths = set()
        for include in tu.get_includes():
            include_file = include.include
            if include_file is None:
                continue
            dependency_path = include_file.name
            if dependency_path in seen_dependency_paths:
                continue
            seen_dependency_paths.add(dependency_path)
            if os.path.exists(dependency_path):
                dependencies.append(file_signature(dependency_path))
        walk_root(tu.cursor)
        return normalize_graph_lists({
            'file': file_path,
            'callgraph': dict(partial_callgraph),
            'fullnames': dict(partial_fullnames),
            'refgraph': dict(partial_refgraph),
            'diagnostics': diagnostics,
            'dependencies': dependencies,
            'error': None,
        })
    except Exception:
        return {
            'file': file_path,
            'callgraph': {},
            'fullnames': {},
            'refgraph': {},
            'diagnostics': [],
            'error': traceback.format_exc(),
        }
    finally:
        if working_directory:
            os.chdir(original_cwd)


def analyze_source_files(cfg: dict) -> dict:
    start_time = time.perf_counter()
    CALLGRAPH.clear()
    FULLNAMES.clear()
    REFGRAPH.clear()
    buffer_clear()
    set_complete_list([])
    if cfg['library_path']:
        if check_libclang_exists(cfg['library_path']):
            Config.set_library_path(cfg['library_path'])
        else:
            print(f"{ctrl_red}cannot find libclang-14.so in {cfg['library_path']}, ignore library_path argument.{ctrl_reset}")

    compile_commands = read_compile_commands(cfg['db'])
    db_fingerprint = read_db_fingerprint(cfg['db'])
    results = []
    pending_tasks = []
    for cmd in compile_commands:
        normalized = normalize_compile_command(cmd, cfg['clang_args'], db_fingerprint)
        normalized['excluded_paths'] = cfg['excluded_paths']
        normalized['excluded_prefixes'] = cfg['excluded_prefixes']
        cached = load_cached_result(normalized)
        if cached is not None:
            results.append(cached)
        else:
            pending_tasks.append(normalized)

    actual_workers = 1
    if pending_tasks:
        actual_workers = 1 if cfg['jobs'] <= 1 else min(cfg['jobs'], len(pending_tasks))
        if actual_workers == 1:
            fresh_results = [parse_translation_unit(task) for task in pending_tasks]
        else:
            with ProcessPoolExecutor(max_workers=actual_workers) as executor:
                fresh_results = list(executor.map(parse_translation_unit, pending_tasks, chunksize=4))
        for task, result in zip(pending_tasks, fresh_results):
            if result['error'] is None:
                save_cached_result(task, result)
        results.extend(fresh_results)

    merge_partial_graphs(results)

    summary = {
        'file_count': len(results),
        'function_count': len(FULLNAMES),
        'jobs': actual_workers,
        'load_seconds': time.perf_counter() - start_time,
    }

    if cfg['quiet']:
        return summary

    for result in results:
        print(result['file'])
        if result['diagnostics']:
            pprint(('diags', result['diagnostics']))
        if result['error']:
            print(f"failed parse file: {result['file']}")
            print(result['error'])

    return summary


def print_refgraph(fun: str) -> None:
    print('')
    if fun in REFGRAPH:
        buffer_append(fun)
        print_refs(fun, list())
    buffer_flush(True)


def print_callgraph(fun: str) -> None:
    print('')
    if fun in CALLGRAPH:
        buffer_append(code_color_pretty(fun))
        print_calls(fun, list())
    else:
        match_list: list = []
        print(f'{ctrl_yellow}matching list:{ctrl_reset}')
        for f, ff in FULLNAMES.items():
            if f.startswith(fun):
                for fff in ff:
                    match_list.append(fff)
                    buffer_append(code_color_pretty(fff))
        if len(match_list) > 0:
            set_complete_list(match_list)
    buffer_flush(True)


def print_filter_callgraph(fun: str, call_stack: list) -> None:
    print('')
    if fun in CALLGRAPH:
        buffer_append(code_color_pretty(fun))
        filter_calls(fun, call_stack, list())
    buffer_flush(True)


def print_ignore_callgraph(fun: str) -> None:
    print('')
    if fun in CALLGRAPH:
        buffer_append(code_color_pretty(fun))
        ignore_calls(fun, list())
    buffer_flush(True)


usage_message: str = """
Usage:
    @ ignore keyword1 [keyword2] ...    add ignore keywords
    @ filter keyword1 [keyword2] ...    add filter keywords
    @ del_ig keyword1 [keyword2] ...    del ignore keywords
    @ del_fi keyword1 [keyword2] ...    del filter keywords
    @ depth  n                          set max print depth
    @ show                              show query config
    @ reset                             reset query config
    ? complete_function_name            show call graph to function contain 'filter' keywords
    ! complete_function_name            show call graph without 'ignore' keywords
    & complete_function_name            show reference of function
"""

def ask_and_print_callgraph() -> None:
    try:
        fun: str = input(f'>>> ')
        if not fun or len(fun.strip()) <= 0:
            return

        fun = fun.strip()
        # special commmad
        if fun.startswith('@'):
            global g_print_depth
            args: list[str] = fun.split(' ')
            if len(args) <= 1:
                print(f'{ctrl_green}{usage_message}{ctrl_reset}')
                return
            if args[1] == 'reset':
                g_filter_set.clear()
                g_ignore_set.clear()
                g_print_depth = g_max_print_depth
                print("reset finish")
                return
            if args[1] == 'show':
                print(f'{ctrl_green}filter set: {g_filter_set}{ctrl_reset}')
                print(f'{ctrl_green}ignore set: {g_ignore_set}{ctrl_reset}')
                print(f'{ctrl_green}print depth: {g_print_depth}{ctrl_reset}')
                print(f'{ctrl_green}max print depth: {g_max_print_depth}{ctrl_reset}')
                return
            if args[1] == 'filter':
                for keyword in args[2:]:
                    g_filter_set.add(keyword)
                print(f'update filter set:{ctrl_green} {g_filter_set}{ctrl_reset}')
                return
            if args[1] == 'ignore':
                for keyword in args[2:]:
                    g_ignore_set.add(keyword)
                print(f'update ignore set:{ctrl_green} {g_ignore_set}{ctrl_reset}')
                return
            if args[1] == 'depth':
                depth: int = int(args[2])
                if depth <= 0 or depth >= g_max_print_depth:
                    print(usage_message)
                    return
                g_print_depth = depth
                return
            if args[1] == 'del_ig':
                for kw in args[2:]:
                    g_ignore_set.remove(kw)
                return
            if args[1] == 'del_fi':
                for kw in args[2:]:
                    g_filter_set.remove(kw)
                return

        if fun.startswith('?'):
            args = fun.split(' ', 1)
            start_func: str = args[1]
            call_stack: list = []
            print_filter_callgraph(start_func, call_stack)
            return

        if fun.startswith("!"):
            args = fun.split(' ', 1)
            start_func: str = args[1]
            print_ignore_callgraph(start_func)
            return

        if fun.startswith("&"):
            args = fun.split(' ', 1)
            start_func: str = args[1]
            print_refgraph(start_func)
            return

        # just find all function with keyword or print call graph
        print_callgraph(fun)

    except Exception as _:
        buffer_clear()
        traceback.print_exc()


def main() -> None:
    cfg: dict = read_args(sys.argv[1:])
    if cfg['db'] is None:
        print('usage: ' + sys.argv[0] + ' file.cpp|compile_database.json '
              '[extra clang args...]')
        return

    load_config_file(cfg)

    if cfg['clear_cache_first']:
        cleared = clear_cache()
        print(f"cleared {cleared} cache files")

    summary = analyze_source_files(cfg)
    print(f"loaded {summary['file_count']} files, {summary['function_count']} functions in {summary['load_seconds']:.2f}s with {summary['jobs']} workers")

    if cfg['lookup']:
        print_callgraph(cfg['lookup'])
    if cfg['ask']:
        while True:
            ask_and_print_callgraph()


if __name__ == '__main__':
    main()
