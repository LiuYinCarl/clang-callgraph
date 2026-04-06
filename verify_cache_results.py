import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET_REPO = Path(os.environ.get('CLANG_CALLGRAPH_TARGET_REPO', str(ROOT / 'cpython'))).resolve()
DB = str(Path(os.environ.get('CLANG_CALLGRAPH_DB', str(TARGET_REPO / 'compile_commands.json'))).resolve())
LIB = str(Path(os.environ.get('CLANG_CALLGRAPH_LIBRARY_PATH', str(ROOT / '.local-libclang'))).resolve())
PY = str(ROOT / '.venv/bin/python')
CACHE_DIR = TARGET_REPO / '.clang-callgraph-cache'
TIMEOUT = int(os.environ.get('CLANG_CALLGRAPH_VERIFY_TIMEOUT', '120'))

DRIVER = """
import sys
import clang_callgraph as cc

cfg = cc.read_args(sys.argv[1:])
cc.load_config_file(cfg)
cc.analyze_source_files(cfg)
for command in sys.stdin.read().splitlines():
    if not command.strip():
        continue
    if command.startswith('LOOKUP '):
        cc.print_callgraph(command[len('LOOKUP '):])
    elif command.startswith('FILTER '):
        cc.print_filter_callgraph(command[len('FILTER '):], [])
    elif command.startswith('IGNORE '):
        cc.print_ignore_callgraph(command[len('IGNORE '):])
    elif command.startswith('REF '):
        cc.print_refgraph(command[len('REF '):])
    elif command.startswith('CMD '):
        fun = command[len('CMD '):]
        if fun.startswith('?'):
            cc.print_filter_callgraph(fun.split(' ', 1)[1], [])
        elif fun.startswith('!'):
            cc.print_ignore_callgraph(fun.split(' ', 1)[1])
        elif fun.startswith('&'):
            cc.print_refgraph(fun.split(' ', 1)[1])
        else:
            cc.print_callgraph(fun)
"""


def clear_cache() -> None:
    if not CACHE_DIR.exists():
        return
    for path in CACHE_DIR.glob('*.json'):
        path.unlink()


def run_driver(commands: list[str], warm: bool) -> str:
    if not warm:
        clear_cache()
    result = subprocess.run(
        [PY, '-c', DRIVER, DB, '--library_path', LIB],
        cwd=ROOT,
        input='\n'.join(commands) + '\n',
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    return result.stdout


def extract_sections(text: str) -> list[str]:
    parts = []
    current = []
    for line in text.splitlines():
        if line == '' and current:
            parts.append('\n'.join(current).strip())
            current = []
            continue
        if line.startswith('reading source files...'):
            continue
        target_prefix = str(TARGET_REPO) + '/'
        if line.startswith(target_prefix):
            continue
        if line.startswith('-std=') or line.startswith('-I') or line.startswith('-D'):
            continue
        if line.startswith("('diags',"):
            continue
        if line.startswith(' [{') or line.startswith("[{") or "'severity':" in line or "'spelling':" in line or "'location':" in line or "'ranges':" in line or "'fixits':" in line:
            continue
        if line.startswith('failed parse file:'):
            continue
        current.append(line)
    if current:
        parts.append('\n'.join(current).strip())
    return [p for p in parts if p]


def assert_same(name: str, cold: str, warm: str) -> None:
    if cold != warm:
        cold_path = Path(f'/tmp/{name}_cold.txt')
        warm_path = Path(f'/tmp/{name}_warm.txt')
        cold_path.write_text(cold)
        warm_path.write_text(warm)
        raise SystemExit(f'{name} mismatch')


def verify(name: str, command: str) -> None:
    cold = extract_sections(run_driver([command], warm=False))
    warm = extract_sections(run_driver([command], warm=True))
    if len(cold) != 1 or len(warm) != 1:
        raise SystemExit(f'{name} unexpected section count cold={len(cold)} warm={len(warm)}')
    assert_same(name, cold[0], warm[0])


def main() -> None:
    verify('lookup', 'LOOKUP list_iter(int *)')
    verify('filter', 'FILTER list_iter(int *)')
    verify('ignore', 'IGNORE list_iter(int *)')
    verify('ref', 'REF list_iter(int *)')
    verify('lookup_token', 'LOOKUP _PyToken_OneChar(int)')
    print('ok lookup,filter,ignore,ref,lookup_token')


if __name__ == '__main__':
    main()
