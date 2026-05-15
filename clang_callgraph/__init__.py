#!/usr/bin/env python3
"""Generate an interactive call graph from C/C++ codebases using libclang."""

import atexit
import bisect
import hashlib
import json
import os
import pickle
import shutil
import signal
import sys
import time
import traceback
from collections import defaultdict
from pprint import pprint

from clang.cindex import Config, CursorKind, Index, TranslationUnit
from pygments import highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import CLexer

try:
    import readline
except ImportError:
    try:
        import pyreadline3 as readline
    except ImportError:
        readline = None

try:
    import yaml
except ImportError:
    yaml = None


# ── constants ────────────────────────────────────────────────────────────────

C_YELLOW = '\033[033m'
C_GREEN  = '\033[032m'
C_RED    = '\033[031m'
C_RESET  = '\033[0m'

HISTORY_FILE = os.path.expanduser('~/.clang_callgraph_history')
HISTORY_LEN  = 1000
MAX_DEPTH    = 15

# pygments singletons
_FORMATTER = TerminalFormatter()
_CLEXER    = CLexer()

# libclang cursor kinds that introduce a function
_FUNC_KINDS = {CursorKind.FUNCTION_DECL, CursorKind.CXX_METHOD,
               CursorKind.FUNCTION_TEMPLATE}


# ── data model ───────────────────────────────────────────────────────────────

CALLGRAPH = defaultdict(list)       # caller_display_name → [CallTarget, …]
REFGRAPH  = defaultdict(list)       # callee_display_name → [caller_display_name, …]
FULLNAMES = defaultdict(set)        # spelling_fq  → {display_name, …}

g_depth      = MAX_DEPTH
g_filter_set = set()
g_ignore_set = set()
g_buffer     = []

# progress bar state
_PROGRESS_TTY   = sys.stderr.isatty()
_PROGRESS_ACTIVE = False
_PROGRESS_LAST   = 0.0


class CallTarget:
    """Serializable value-object replacing a libclang Cursor in the call graph.

    Captures the subset of cursor fields needed for traversal and display.
    Implements ``__eq__`` / ``__hash__`` on *fq_pretty* so deduplication
    and cycle detection work correctly (raw Cursors are not hashable).
    """
    __slots__ = ('displayname', 'virtual', 'pure_virtual', 'fq_pretty', 'fq')

    def __init__(self, cursor) -> None:
        self.displayname  = cursor.displayname
        self.virtual      = cursor.is_virtual_method()
        self.pure_virtual = cursor.is_pure_virtual_method()
        self.fq_pretty    = _fq_name(cursor, 'displayname')
        self.fq           = _fq_name(cursor, 'spelling')

    def __eq__(self, other):
        if isinstance(other, CallTarget):
            return self.fq_pretty == other.fq_pretty
        return NotImplemented

    def __hash__(self):
        return hash(self.fq_pretty)

    def display(self) -> str:
        """Pretty-print form used in tree output."""
        s = self.fq_pretty
        if self.virtual:
            s += ' virtual'
        if self.pure_virtual:
            s += ' = 0'
        return s


# ── progress bar ─────────────────────────────────────────────────────────────

def _progress(msg: str, force: bool = False) -> None:
    global _PROGRESS_ACTIVE, _PROGRESS_LAST
    if not _PROGRESS_TTY:
        return
    now = time.monotonic()
    if not force and now - _PROGRESS_LAST < 0.2:
        return
    width = max(20, shutil.get_terminal_size(fallback=(120, 20)).columns - 1)
    sys.stderr.write(f'\r\x1b[2K{msg[:width]}')
    sys.stderr.flush()
    _PROGRESS_ACTIVE = True
    _PROGRESS_LAST = now


def _progress_finish() -> None:
    global _PROGRESS_ACTIVE
    if _PROGRESS_TTY and _PROGRESS_ACTIVE:
        sys.stderr.write('\r\x1b[2K\n')
        sys.stderr.flush()
        _PROGRESS_ACTIVE = False


# ── cursor helpers ───────────────────────────────────────────────────────────

def _fq_name(cursor, attr: str) -> str:
    if cursor is None:
        return ''
    if cursor.kind == CursorKind.TRANSLATION_UNIT:
        return ''
    parent = _fq_name(cursor.semantic_parent, attr)
    leaf   = getattr(cursor, attr)
    return f'{parent}::{leaf}' if parent else leaf


def fully_qualified(c) -> str:
    return _fq_name(c, 'spelling')


def fully_qualified_pretty(c) -> str:
    return _fq_name(c, 'displayname')


def is_excluded(node, xfiles, xprefs) -> bool:
    if node.extent is None or not node.extent.start.file:
        return False
    fname = node.extent.start.file.name
    for xf in xfiles:
        if fname.startswith(xf):
            return True
    fqp = fully_qualified_pretty(node)
    for xp in xprefs:
        if fqp.startswith(xp):
            return True
    return False


def _color(code: str) -> str:
    return highlight(code, _CLEXER, _FORMATTER).rstrip()


# ── AST traversal ────────────────────────────────────────────────────────────

def show_info(node, xfiles, xprefs, cur_fun=None) -> None:
    if node.kind in _FUNC_KINDS:
        if not is_excluded(node, xfiles, xprefs):
            cur_fun = node
            FULLNAMES[fully_qualified(cur_fun)].add(fully_qualified_pretty(cur_fun))

    if node.kind == CursorKind.CALL_EXPR:
        if cur_fun is not None and node.referenced \
                and not is_excluded(node.referenced, xfiles, xprefs):
            callee_pretty = fully_qualified_pretty(node.referenced)
            caller_pretty = fully_qualified_pretty(cur_fun)
            if caller_pretty not in REFGRAPH[callee_pretty]:
                REFGRAPH[callee_pretty].append(caller_pretty)
            CALLGRAPH[caller_pretty].append(CallTarget(node.referenced))

    for child in node.get_children():
        show_info(child, xfiles, xprefs, cur_fun)


# ── graph maintenance ────────────────────────────────────────────────────────

g_fullname_keys = []


def _build_index():
    global g_fullname_keys
    g_fullname_keys = sorted(FULLNAMES.keys())


def _dedup_graphs():
    for key in CALLGRAPH:
        seen = set()
        vals = []
        for v in CALLGRAPH[key]:
            if v not in seen:
                seen.add(v); vals.append(v)
        CALLGRAPH[key] = vals
    for key in REFGRAPH:
        seen = set(); vals = []
        for v in REFGRAPH[key]:
            if v not in seen:
                seen.add(v); vals.append(v)
        REFGRAPH[key] = vals


def _fullname_matches(prefix: str):
    lo = bisect.bisect_left(g_fullname_keys, prefix)
    hi = bisect.bisect_left(g_fullname_keys, prefix + '\uffff')
    for key in g_fullname_keys[lo:hi]:
        for dn in sorted(FULLNAMES[key]):
            yield dn


# ── persistent cache ─────────────────────────────────────────────────────────

CACHE_DIR = os.path.expanduser('~/.cache/clang-callgraph')


def _cache_key(cfg):
    h = hashlib.sha256()
    if os.path.isfile(cfg['db']):
        with open(cfg['db'], 'rb') as f:
            h.update(f.read())
    else:
        h.update(cfg['db'].encode())
    for k in ('clang_args', 'excluded_prefixes', 'excluded_paths'):
        h.update(json.dumps(cfg[k], sort_keys=True).encode())
    return h.hexdigest()


def _cache_path(cfg):
    return os.path.join(CACHE_DIR, _cache_key(cfg) + '.pickle')


def _load_cache(cfg):
    path = _cache_path(cfg)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as f:
            data = pickle.load(f)
        CALLGRAPH.clear(); REFGRAPH.clear(); FULLNAMES.clear()
        CALLGRAPH.update(data['callgraph'])
        REFGRAPH.update(data['refgraph'])
        FULLNAMES.update(data['fullnames'])
        _build_index()
        return True
    except Exception:
        return False


def _save_cache(cfg):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(cfg), 'wb') as f:
            pickle.dump({'callgraph': dict(CALLGRAPH),
                         'refgraph':  dict(REFGRAPH),
                         'fullnames': dict(FULLNAMES)}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass


def _clear_cache_dir() -> int:
    """Remove all cached pickle files.  Returns count of removed files."""
    if not os.path.isdir(CACHE_DIR):
        return 0
    removed = 0
    for fn in os.listdir(CACHE_DIR):
        if fn.endswith('.pickle'):
            os.unlink(os.path.join(CACHE_DIR, fn))
            removed += 1
    return removed


# ── tree walkers (output to g_buffer) ────────────────────────────────────────

def _depth_guard(depth):
    if depth >= g_depth:
        return True
    if depth >= MAX_DEPTH:
        g_buffer.append('...<too deep>...')
        return True
    return False


def print_refs(fun_name, so_far, depth=0):
    if _depth_guard(depth):
        return
    if fun_name not in REFGRAPH:
        return
    for caller in REFGRAPH[fun_name]:
        g_buffer.append(f'{C_RED}|{C_RESET}  ' * depth
                        + f'{C_RED}|--{C_RESET}' + _color(caller))
        if caller in so_far:
            continue
        so_far.append(caller)
        if caller in REFGRAPH:
            print_refs(caller, so_far, depth + 1)


def _recurse_ct(ct, so_far, depth, walker):
    key = ct.fq_pretty if ct.fq_pretty in CALLGRAPH else ct.fq
    if key in CALLGRAPH:
        walker(key, so_far, depth + 1)


def print_calls(fun_name, so_far, depth=0):
    if _depth_guard(depth):
        return
    if fun_name not in CALLGRAPH:
        return
    for ct in CALLGRAPH[fun_name]:
        g_buffer.append(f'{C_GREEN}|{C_RESET}  ' * depth
                        + f'{C_GREEN}|--{C_RESET}' + _color(ct.display()))
        if ct in so_far:
            continue
        so_far.append(ct)
        _recurse_ct(ct, so_far, depth, print_calls)


def filter_calls(fun_name, call_stack, so_far, depth=0):
    if _depth_guard(depth):
        return
    if fun_name not in CALLGRAPH:
        return
    for ct in CALLGRAPH[fun_name]:
        line = (f'{C_GREEN}|{C_RESET}  ' * depth
                + f'{C_GREEN}|--{C_RESET}' + _color(ct.display()))
        call_stack.append(line)
        if any(kw in ct.displayname for kw in g_filter_set):
            for stacked in call_stack:
                g_buffer.append(stacked)
        if ct in so_far:
            call_stack.pop()
            continue
        so_far.append(ct)
        key = ct.fq_pretty if ct.fq_pretty in CALLGRAPH else ct.fq
        if key in CALLGRAPH:
            filter_calls(key, call_stack, so_far, depth + 1)
        call_stack.pop()


def ignore_calls(fun_name, so_far, depth=0):
    if _depth_guard(depth):
        return
    if fun_name not in CALLGRAPH:
        return
    for ct in CALLGRAPH[fun_name]:
        if any(kw in ct.displayname for kw in g_ignore_set):
            continue
        g_buffer.append(f'{C_GREEN}|{C_RESET}  ' * depth
                        + f'{C_GREEN}|--{C_RESET}' + _color(ct.display()))
        if ct in so_far:
            continue
        so_far.append(ct)
        _recurse_ct(ct, so_far, depth, ignore_calls)


# ── output entry points ──────────────────────────────────────────────────────

def _flush(show_count=False):
    if show_count:
        n = len(g_buffer)
        g_buffer.append(f'{C_GREEN}[total lines: {n}]{C_RESET}')
    for line in g_buffer:
        print(line)
    g_buffer.clear()


def _print_matching(fun):
    print(f'{C_YELLOW}matching list:{C_RESET}')
    matches = []
    for dn in _fullname_matches(fun):
        matches.append(dn)
        g_buffer.append(_color(dn))
    if matches:
        set_complete_list(matches)
    _flush(True)


def print_callgraph(fun):
    print()
    if fun in CALLGRAPH:
        g_buffer.append(_color(fun))
        print_calls(fun, [])
        _seed_completer(fun)
        _flush(True)
    else:
        _print_matching(fun)


def print_refgraph(fun):
    print()
    if fun in REFGRAPH:
        g_buffer.append(fun)
        print_refs(fun, [])
    else:
        found = False
        for dn in _fullname_matches(fun):
            if dn in REFGRAPH:
                found = True
                g_buffer.append(dn)
                print_refs(dn, [])
        if not found:
            print(f'{C_YELLOW}no references found for: {fun}{C_RESET}')
    _flush(True)


def print_filter_callgraph(fun, call_stack):
    print()
    if fun in CALLGRAPH:
        g_buffer.append(_color(fun))
        filter_calls(fun, call_stack, [])
    _flush(True)


def print_ignore_callgraph(fun):
    print()
    if fun in CALLGRAPH:
        g_buffer.append(_color(fun))
        ignore_calls(fun, [])
    _flush(True)


# ── readline ─────────────────────────────────────────────────────────────────

complete_list    = []
_complete_cache  = []


def complete(text, state):
    global _complete_cache
    if state == 0:
        _complete_cache = [c for c in complete_list if c.startswith(text)]
    try:
        return _complete_cache[state]
    except IndexError:
        return None


def set_complete_list(lst):
    global complete_list
    complete_list = lst


def _seed_completer(prefix):
    matches = list(_fullname_matches(prefix))
    if matches:
        set_complete_list(matches)


def _setup_readline():
    if readline is None:
        return
    try:
        readline.parse_and_bind('tab: complete')
    except Exception:
        pass
    readline.set_completer(complete)
    try:
        readline.read_history_file(HISTORY_FILE)
    except (FileNotFoundError, PermissionError):
        pass
    readline.set_history_length(HISTORY_LEN)
    atexit.register(_save_history)


def _save_history():
    if readline is None:
        return
    try:
        readline.write_history_file(HISTORY_FILE)
    except (OSError, PermissionError):
        pass


# ── CLI ──────────────────────────────────────────────────────────────────────

def read_compile_commands(filename):
    if filename.endswith('.json'):
        try:
            with open(filename) as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f'{C_RED}error reading {filename}: {e}{C_RESET}')
            return []
    if os.path.isdir(filename):
        print(f'{C_YELLOW}warning: {filename} is a directory, skipping{C_RESET}')
        return []
    return [{'command': '', 'file': filename}]


def read_args(args):
    db = None
    clang_args, excluded_prefixes, excluded_paths = [], [], []
    config_filename = lookup = library_path = ''
    clear_cache = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-h', '--help'):
            return {'help': True}
        if a == '-x':
            i += 1
            if i < len(args):
                excluded_prefixes += [p for p in args[i].split(',') if p]
        elif a == '-p':
            i += 1
            if i < len(args):
                excluded_paths += [p for p in args[i].split(',') if p]
        elif a == '--cfg':
            i += 1
            if i < len(args):
                config_filename = args[i]
        elif a == '--lookup':
            i += 1
            if i < len(args):
                lookup = args[i]
        elif a == '--library_path':
            i += 1
            if i < len(args):
                library_path = args[i]
        elif a == '--clear-cache':
            clear_cache = True
        elif a and a[0] == '-':
            clang_args.append(a)
        else:
            db = a
        i += 1

    if not excluded_paths:
        excluded_paths.append('/usr')
    if not db and os.path.exists('compile_commands.json'):
        db = 'compile_commands.json'
    if db and os.path.isdir(db):
        p = os.path.join(db, 'compile_commands.json')
        if os.path.isfile(p):
            db = p

    return {'db': db, 'clang_args': clang_args,
            'excluded_prefixes': excluded_prefixes,
            'excluded_paths': excluded_paths,
            'config_filename': config_filename,
            'lookup': lookup, 'ask': not lookup,
            'library_path': library_path,
            'clear_cache': clear_cache}


def load_config_file(cfg):
    if not cfg['config_filename']:
        return
    if yaml is None:
        print(f'{C_RED}warning: pyyaml not installed, ignoring --cfg{C_RESET}')
        return
    with open(cfg['config_filename'], 'r') as f:
        data = yaml.load(f, Loader=yaml.FullLoader)
    for k in ('clang_args', 'excluded_prefixes', 'excluded_paths', 'library_path'):
        val = data.get(k)
        if val is None:
            continue
        if not isinstance(val, list):
            print(f'{C_YELLOW}warning: config key {k!r} should be a list, '
                  f'got {type(val).__name__}{C_RESET}')
            val = [val]
        cfg[k] += val


def keep_arg(x):
    return x.startswith(('-I', '-std=', '-D'))


# ── main pipeline ────────────────────────────────────────────────────────────

def _print_stats(nfiles, from_cache, elapsed):
    nfuncs = sum(len(v) for v in FULLNAMES.values())
    nedges = sum(len(v) for v in CALLGRAPH.values())
    src = f'{C_GREEN}cache{C_RESET}' if from_cache else f'{C_YELLOW}parsed{C_RESET}'
    print(f'{src} {nfiles} files, {nfuncs} functions, '
          f'{nedges} call edges ({elapsed:.1f}s)')


def analyze_source_files(cfg):
    t0 = time.monotonic()

    if _load_cache(cfg):
        _print_stats(len(read_compile_commands(cfg['db'])), True,
                     time.monotonic() - t0)
        return

    # clean slate before fresh parse
    CALLGRAPH.clear()
    FULLNAMES.clear()
    REFGRAPH.clear()

    if cfg['library_path'] and os.path.isfile(
            os.path.join(cfg['library_path'], 'libclang-14.so')):
        Config.set_library_path(cfg['library_path'])

    cmds = read_compile_commands(cfg['db'])
    index = Index.create()  # single Index for all files
    for idx, cmd in enumerate(cmds, start=1):
        wd = cmd.get('directory', '')
        src = os.path.join(wd, cmd['file']) if wd else cmd['file']
        argv = (cmd['arguments'] if 'arguments' in cmd
                else cmd['command'].split())
        cargs = [x for x in argv if keep_arg(x)] + cfg['clang_args']

        try:
            tu = index.parse(src, cargs)
            _progress(f'{idx}/{len(cmds)}  {src}',
                      force=(idx == 1 or idx == len(cmds)))

            for d in tu.diagnostics:
                if d.severity in (d.Error, d.Fatal):
                    print(' '.join(cargs))
                    pprint(('diags', [{'severity': d.severity,
                                       'location': d.location,
                                       'spelling': d.spelling,
                                       'ranges': list(d.ranges),
                                       'fixits': list(d.fixits)}
                                      for d in tu.diagnostics]))
            show_info(tu.cursor, cfg['excluded_paths'], cfg['excluded_prefixes'])
        except Exception:
            print(f'failed parse file: {src}')
            traceback.print_exc()

    _progress_finish()
    _dedup_graphs()
    _build_index()
    _save_cache(cfg)
    _print_stats(len(cmds), False, time.monotonic() - t0)


# ── REPL ─────────────────────────────────────────────────────────────────────

def _repl_reset(_words):
    g_filter_set.clear()
    g_ignore_set.clear()
    global g_depth
    g_depth = MAX_DEPTH
    print('reset finish')


_REPL_CMDS = {
    'show':   lambda _: (print(f'{C_GREEN}filter set: {g_filter_set}{C_RESET}'),
                         print(f'{C_GREEN}ignore set: {g_ignore_set}{C_RESET}'),
                         print(f'{C_GREEN}print depth: {g_depth}{C_RESET}'),
                         print(f'{C_GREEN}max print depth: {MAX_DEPTH}{C_RESET}')),
    'reset':  _repl_reset,
    'filter': lambda ws: (g_filter_set.update(ws), print(
        f'update filter set:{C_GREEN} {g_filter_set}{C_RESET}')),
    'ignore': lambda ws: (g_ignore_set.update(ws), print(
        f'update ignore set:{C_GREEN} {g_ignore_set}{C_RESET}')),
    'depth':  lambda ws: _repl_depth(ws),
    'del_ig': lambda ws: [g_ignore_set.discard(w) for w in ws],
    'del_fi': lambda ws: [g_filter_set.discard(w) for w in ws],
}

_REPL_USAGE = f"""{C_GREEN}
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
{C_RESET}"""


def _repl_depth(words):
    global g_depth
    try:
        n = int(words[0])
    except (ValueError, IndexError):
        print(_REPL_USAGE)
        return
    if 1 <= n < MAX_DEPTH:
        g_depth = n
    else:
        print(_REPL_USAGE)


def ask_and_print_callgraph():
    try:
        line = input('>>> ').strip()
        if not line:
            return
    except (EOFError, KeyboardInterrupt):
        print()
        raise

    try:
        if line.startswith('@'):
            parts = line.split()
            if len(parts) <= 1:
                print(_REPL_USAGE)
                return
            handler = _REPL_CMDS.get(parts[1])
            if handler:
                handler(parts[2:])
            else:
                print(_REPL_USAGE)
            return

        prefix, _, rest = line.partition(' ')
        prefix_map = {
            '?': lambda r: print_filter_callgraph(r, []),
            '!': print_ignore_callgraph,
            '&': print_refgraph,
        }
        if prefix in prefix_map:
            prefix_map[prefix](rest)
            return

        print_callgraph(line)
    except Exception:
        g_buffer.clear()
        traceback.print_exc()


# ── entry point ──────────────────────────────────────────────────────────────

CLI_USAGE = f"""Usage: clang-callgraph <file.cpp|compile_commands.json|directory> [options] [clang args...]

Generate an interactive call graph from C/C++ source code.

Arguments:
  <input>       Source file (.cpp/.c), compilation database (.json),
                or directory (looks for compile_commands.json inside).
                If omitted, defaults to ./compile_commands.json

Options:
  -x P1,P2      Exclude symbols by name prefix (e.g. std::,boost::)
  -p P1,P2      Exclude symbols defined in these path prefixes
  --cfg FILE    YAML config file for excluded_prefixes, excluded_paths,
                clang_args, library_path
  --lookup FUNC Non-interactive mode: print callgraph and exit
  --library_path PATH  Path to directory containing libclang-14.so
  --clear-cache Remove all cached parse results
  -h, --help    Show this help message and exit
"""


def main():
    _setup_readline()

    cfg = read_args(sys.argv[1:])
    if cfg.get('help'):
        print(CLI_USAGE)
        return
    if cfg['db'] is None:
        print(f'usage: {sys.argv[0]} file.cpp|compile_database.json '
              '[extra clang args...]')
        return

    load_config_file(cfg)

    if cfg.get('clear_cache'):
        n = _clear_cache_dir()
        print(f'cleared {n} cache file(s)')
        return

    analyze_source_files(cfg)

    if cfg['lookup']:
        print_callgraph(cfg['lookup'])
    if cfg['ask']:
        while True:
            try:
                ask_and_print_callgraph()
            except (EOFError, KeyboardInterrupt):
                print()
                break


if __name__ == '__main__':
    signal.signal(signal.SIGINT, lambda *_: (print('user exit.'), sys.exit(0)))
    main()
