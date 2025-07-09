#!/usr/bin/env python3

from pprint import pprint
from clang.cindex import CursorKind, Index
from collections import defaultdict
import readline
import os
import sys
import json
from sched import scheduler
import time
import yaml
import threading
import traceback
import signal
from pygments import highlight
from pygments.lexers import CLexer
from pygments.formatters import TerminalFormatter

"""
Dumps a callgraph of a function in a codebase
usage: callgraph.py file.cpp|compile_commands.json [-x exclude-list] [extra clang args...]
The easiest way to generate the file compile_commands.json for any make based
compilation chain is to use Bear and recompile with `bear make`.

When running the python script, after parsing all the codebase, you are
prompted to type in the function's name for which you wan to obtain the
callgraph
"""

# signal

def signal_handler(sig, frame):
    print("user exit.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


# readline helper

complete_list = []

def complete(text, state):
    options = [c for c in complete_list if c.startswith(text)]
    return options[state] if state < len(options) else None

readline.parse_and_bind("tab: complete")
readline.set_completer(complete)

def set_complete_list(l):
    global complete_list
    complete_list = l


CALLGRAPH = defaultdict(list)
FULLNAMES = defaultdict(set)

g_max_print_depth: int = 15
g_print_depth: int = 15

g_filter_set: set = set()
g_ignore_set: set = set()

ctrl_green = '\033[032m'
ctrl_reset = '\033[0m'

def get_diag_info(diag):
    return {
        'severity': diag.severity,
        'location': diag.location,
        'spelling': diag.spelling,
        'ranges': list(diag.ranges),
        'fixits': list(diag.fixits)
    }


def fully_qualified(c):
    if c is None:
        return ''
    elif c.kind == CursorKind.TRANSLATION_UNIT:
        return ''
    else:
        res = fully_qualified(c.semantic_parent)
        if res != '':
            return res + '::' + c.spelling
        return c.spelling


def fully_qualified_pretty(c):
    if c is None:
        return ''
    elif c.kind == CursorKind.TRANSLATION_UNIT:
        return ''
    else:
        res = fully_qualified(c.semantic_parent)
        if res != '':
            return res + '::' + c.displayname
        return c.displayname


def is_excluded(node, xfiles, xprefs):
    if not node.extent.start.file:
        return False

    for xf in xfiles:
        if node.extent.start.file.name.startswith(xf):
            return True

    fqp = fully_qualified_pretty(node)

    for xp in xprefs:
        if fqp.startswith(xp):
            return True

    return False


def show_info(node, xfiles, xprefs, cur_fun=None):
    if node.kind == CursorKind.FUNCTION_TEMPLATE:
        if not is_excluded(node, xfiles, xprefs):
            cur_fun = node
            FULLNAMES[fully_qualified(cur_fun)].add(
                fully_qualified_pretty(cur_fun))

    if node.kind == CursorKind.CXX_METHOD or \
            node.kind == CursorKind.FUNCTION_DECL:
        if not is_excluded(node, xfiles, xprefs):
            cur_fun = node
            FULLNAMES[fully_qualified(cur_fun)].add(
                fully_qualified_pretty(cur_fun))

    if node.kind == CursorKind.CALL_EXPR:
        if node.referenced and not is_excluded(node.referenced, xfiles, xprefs):
            CALLGRAPH[fully_qualified_pretty(cur_fun)].append(node.referenced)

    for c in node.get_children():
        show_info(c, xfiles, xprefs, cur_fun)


def pretty_print(n):
    v = ''
    if n.is_virtual_method():
        v = ' virtual'
    if n.is_pure_virtual_method():
        v = ' = 0'
    return fully_qualified_pretty(n) + v

def code_color_pretty(code):
    formatter = TerminalFormatter()
    highlight_code = highlight(code, CLexer(), formatter)
    return highlight_code.rstrip()

def print_calls(fun_name, so_far, depth=0):
    if depth > g_print_depth:
        return
    if depth >= g_max_print_depth:
        print('...<too deep>...')
        return
    if fun_name in CALLGRAPH:
        for f in CALLGRAPH[fun_name]:
            color_code = code_color_pretty(pretty_print(f))
            print(f'{ctrl_green}|{ctrl_reset}  ' * (depth) + f'{ctrl_green}|--{ctrl_reset}' + color_code)

            if f in so_far:
                continue
            so_far.append(f)
            if fully_qualified_pretty(f) in CALLGRAPH:
                print_calls(fully_qualified_pretty(f), so_far, depth + 1)
            else:
                print_calls(fully_qualified(f), so_far, depth + 1)

# func_name1: start func
# func_name2: target func
# call_stack: call_stach
def filter_calls(fun_name1, call_stack, so_far, depth=0):
    if depth > g_print_depth:
        return
    if depth >= g_max_print_depth:
        print('...<too deep>...')
        return

    if fun_name1 in CALLGRAPH:
        for f in CALLGRAPH[fun_name1]:
            color_code = code_color_pretty(pretty_print(f))
            line = f'{ctrl_green}|{ctrl_reset}  ' * (depth) + f'{ctrl_green}|--{ctrl_reset}' + color_code
            call_stack.append(line)

            for kw in g_filter_set:
                if kw in f.displayname:
                    for line in call_stack:
                        print(line)
                    break

            if f in so_far:
                call_stack.pop()
                continue
            so_far.append(f)
            if fully_qualified_pretty(f) in CALLGRAPH:
                filter_calls(fully_qualified_pretty(f), call_stack, so_far, depth+1)
            else:
                filter_calls(fully_qualified(f), call_stack, so_far, depth+1)
            call_stack.pop()

def ignore_calls(fun_name1, so_far, depth=0):
    if depth > g_print_depth:
        return
    if depth >= g_max_print_depth:
        print('...<too deep>...')
        return

    if fun_name1 in CALLGRAPH:
        for f in CALLGRAPH[fun_name1]:
            hit_ignore = False
            for kw in g_ignore_set:
                if kw in f.displayname:
                    hit_ignore = True
                    break
            if hit_ignore:
                continue

            color_code = code_color_pretty(pretty_print(f))
            line = f'{ctrl_green}|{ctrl_reset}  ' * (depth) + f'{ctrl_green}|--{ctrl_reset}' + color_code
            print(line)

            if f in so_far:
                continue
            so_far.append(f)
            if fully_qualified_pretty(f) in CALLGRAPH:
                ignore_calls(fully_qualified_pretty(f), so_far, depth + 1)
            else:
                ignore_calls(fully_qualified(f), so_far, depth + 1)


def read_compile_commands(filename):
    if filename.endswith('.json'):
        with open(filename) as compdb:
            return json.load(compdb)
    else:
        return [{'command': '', 'file': filename}]


def read_args(args):
    db = None
    clang_args = []
    excluded_prefixes = []
    excluded_paths = []
    config_filename = None
    lookup = None
    i = 0
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
        elif args[i][0] == '-':
            clang_args.append(args[i])
        else:
            db = args[i]
        i += 1

    if len(excluded_paths) == 0:
        excluded_paths.append('/usr')

    return {
        'db': db,
        'clang_args': clang_args,
        'excluded_prefixes': excluded_prefixes,
        'excluded_paths': excluded_paths,
        'config_filename': config_filename,
        'lookup': lookup,
        'ask': (lookup is None)
    }


def load_config_file(cfg):
    if cfg['config_filename']:
        with open(cfg['config_filename'], 'r') as yamlfile:
            data = yaml.load(yamlfile, Loader=yaml.FullLoader)
            keys = ('clang_args', 'excluded_prefixes', 'excluded_paths')
            for k in keys:
                cfg[k] += data.get(k, [])


def keep_arg(x) -> bool:
    keep_this = x.startswith('-I') or x.startswith('-std=') or x.startswith('-D')
    return keep_this


g_index = Index.create()

# k:v = file:{'mtime':xx, args:[xxx,], 'excluded_paths':[], 'excluded_prefixed':[]]}
g_monitor_file_map = {}

def add_monitor_file(file: str, data:dict):
    if not os.path.exists(file):
        return
    mtime = os.path.getmtime(file)
    data['mtime'] = mtime
    g_monitor_file_map[file] = data

def clear_file_nodes(node, modify_file):
    file = node.extent.start.file.name
    if file != modify_file:
        return
    FULLNAMES[fully_qualified(node)].clear()
    CALLGRAPH[fully_qualified_pretty(node)].clear()
    for c in node.get_children():
        clear_file_nodes(c, modify_file)

def monitor_file_change(file: str):
    args: list = g_monitor_file_map[file]['args']
    tu = g_index.parse(file, args)
    print(f'*** update {file}')
    if not tu:
        print("unable to update file")

    for d in tu.diagnostics:
        if d.severity == d.Error or d.severity == d.Fatal:
            print(' '.join(args))
            pprint(('diags', list(map(get_diag_info, tu.diagnostics))))
            return

    clear_file_nodes(tu.cursor, file)

    show_info(
        tu.cursor,
        g_monitor_file_map[file]['excluded_paths'],
        g_monitor_file_map[file]['excluded_prefixes'],
    )
    add_monitor_file(file, g_monitor_file_map[file])

g_sched = None

# TODO not thread safe now.
def background_task():
    # print('bg task run...')
    for file, data in g_monitor_file_map.items():
        if not os.path.exists(file):
            continue
        mtime = os.path.getmtime(file)
        if mtime > data['mtime']:
            monitor_file_change(file)

    g_sched.enter(2, 1, background_task)

def sched_start():
    global g_sched
    g_sched = scheduler(time.time, time.sleep)
    g_sched.enter(2, 1, background_task)
    g_sched.run()

def start_monitor():
    bg_thread = threading.Thread(target=sched_start, daemon=True)
    bg_thread.start()


def analyze_source_files(cfg):
    print('reading source files...')
    for cmd in read_compile_commands(cfg['db']):
        # https://clang.llvm.org/docs/JSONCompilationDatabase.html#format
        # either "arguments" or "command" is required.
        if 'arguments' in cmd:
            arguments = cmd['arguments']
        else:
            arguments = cmd['command'].split()
        c = [x for x in arguments if keep_arg(x)] + cfg['clang_args']
        tu = g_index.parse(cmd['file'], c)
        print(cmd['file'])
        if not tu:
            print("unable to load input")

        for d in tu.diagnostics:
            if d.severity == d.Error or d.severity == d.Fatal:
                print(' '.join(c))
                pprint(('diags', list(map(get_diag_info, tu.diagnostics))))
                return
        show_info(tu.cursor, cfg['excluded_paths'], cfg['excluded_prefixes'])

        data = {
            'args': c,
            'excluded_paths': cfg['excluded_paths'],
            'excluded_prefixes': cfg['excluded_prefixes']
        }
        add_monitor_file(cmd['file'], data)


def print_callgraph(fun):
    if fun in CALLGRAPH:
        print(fun)
        print_calls(fun, list())
    else:
        match_list = []
        print('matching:')
        for f, ff in FULLNAMES.items():
            if f.startswith(fun):
                for fff in ff:
                    match_list.append(fff)
                    print(code_color_pretty(fff))
        if len(match_list) > 0:
            set_complete_list(match_list)


def print_filter_callgraph(fun, call_stack):
    if fun in CALLGRAPH:
        print(fun)
        filter_calls(fun, call_stack, list())


def print_ignore_callgraph(fun):
    if fun in CALLGRAPH:
        print(fun)
        ignore_calls(fun, list())

usage_message = """
Usage:
    @ ignore keyword1 [keyword2] ...    add ignore keywords
    @ filter keyword1 [keyword2] ...    add filter keywords
    @ del_ig keyword1 [keyword2] ...    del ignore keywords
    @ del_fi keyword1 [keyword2] ...    del filter keywords
    @ depth  n                          set max print depth
    @ show                              show query config
    @ reset                             reset query config
"""

def ask_and_print_callgraph():
    try:
        fun = input('>>> ')
        if not fun or len(fun.strip()) <= 0:
            return

        fun = fun.lstrip()
        # special commmad
        if fun.startswith('@'):
            global g_print_depth
            args = fun.split(' ')
            if len(args) <= 1:
                print(usage_message)
                return
            if args[1] == 'reset':
                g_filter_set.clear()
                g_ignore_set.clear()
                g_print_depth = g_max_print_depth
                print("reset finish")
                return
            if args[1] == 'show':
                print(f'filter set: {g_filter_set}')
                print(f'ignore set: {g_ignore_set}')
                print(f'print depth: {g_print_depth}')
                print(f'max print depth: {g_max_print_depth}')
                return
            if args[1] == 'filter':
                for keyword in args[2:]:
                    g_filter_set.add(keyword)
                print(f'update filter set: {g_filter_set}')
                return
            if args[1] == 'ignore':
                for keyword in args[2:]:
                    g_ignore_set.add(keyword)
                print(f'update ignore set: {g_ignore_set}')
                return
            if args[1] == 'depth':
                depth = int(args[2])
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
            start_func = args[1]
            call_stack = []
            print_filter_callgraph(start_func, call_stack)
            return

        if fun.startswith("!"):
            args = fun.split(' ', 1)
            start_func = args[1]
            print_ignore_callgraph(start_func)
            return

        # just find all function with keyword or print call graph
        print_callgraph(fun)

    except Exception as _:
        traceback.print_exc()


def main():
    if len(sys.argv) < 2:
        print('usage: ' + sys.argv[0] + ' file.cpp|compile_database.json '
              '[extra clang args...]')
        return

    cfg = read_args(sys.argv)
    load_config_file(cfg)

    analyze_source_files(cfg)

    start_monitor()

    if cfg['lookup']:
        print_callgraph(cfg['lookup'])
    if cfg['ask']:
        while True:
            ask_and_print_callgraph()


if __name__ == '__main__':
    main()
