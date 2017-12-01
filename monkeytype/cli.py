# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
import argparse
import importlib
import inspect
import os.path
import subprocess
import sys
import tempfile

from typing import (
    IO,
    List,
    Optional,
    Tuple,
)

from monkeytype.config import Config
from monkeytype.exceptions import MonkeyTypeError
from monkeytype.stubs import (
    Stub,
    build_module_stubs_from_traces,
)
from monkeytype.typing import NoOpRewriter


from monkeytype.util import get_name_in_module


def module_path(path: str) -> Tuple[str, Optional[str]]:
    """Parse <module>[:<qualname>] into its constituent parts."""
    parts = path.split(':', 1)
    module = parts.pop(0)
    qualname = parts[0] if parts else None
    return module, qualname


def module_path_with_qualname(path: str) -> Tuple[str, str]:
    """Require that path be of the form <module>:<qualname>."""
    module, qualname = module_path(path)
    if qualname is None:
        raise argparse.ArgumentTypeError('must be of the form <module>:<qualname>')
    return module, qualname


def monkeytype_config(class_path: str) -> Config:
    """Constructs an instance of the config class specified by class_path"""
    module, qualname = module_path_with_qualname(class_path)
    try:
        config = get_name_in_module(module, qualname)
    except MonkeyTypeError as mte:
        raise argparse.ArgumentTypeError(f'cannot import {class_path}: {mte}')
    return config


def get_stub(args: argparse.Namespace, stdout: IO, stderr: IO) -> Optional[Stub]:
    module, qualname = args.module_path
    thunks = args.config.trace_store().filter(module, qualname, args.limit)
    traces = []
    for thunk in thunks:
        try:
            traces.append(thunk.to_trace())
        except MonkeyTypeError as mte:
            print(f'ERROR: Failed decoding trace: {mte}', file=stderr)
    if not traces:
        return None
    rewriter = args.config.type_rewriter()
    if args.disable_type_rewriting:
        rewriter = NoOpRewriter()
    stubs = build_module_stubs_from_traces(traces, args.include_unparsable_defaults, rewriter)
    return stubs.get(module, None)


def apply_stub_handler(args: argparse.Namespace, stdout: IO, stderr: IO) -> None:
    stub = get_stub(args, stdout, stderr)
    if stub is None:
        print(f'No traces found', file=stderr)
        return
    module = args.module_path[0]
    mod = importlib.import_module(module)
    src_path = inspect.getfile(mod)
    src_dir = os.path.dirname(src_path)
    pyi_name = module.split('.')[-1] + '.pyi'
    with tempfile.TemporaryDirectory(prefix='monkeytype') as pyi_dir:
        pyi_path = os.path.join(pyi_dir, pyi_name)
        with open(pyi_path, 'w+') as f:
            f.write(stub.render())
        cmd = ' '.join([
            'retype',
            '--pyi-dir ' + pyi_dir,
            '--target-dir ' + src_dir,
            src_path
        ])
        subprocess.run(cmd, shell=True, check=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE)


def print_stub_handler(args: argparse.Namespace, stdout: IO, stderr: IO) -> None:
    stub = get_stub(args, stdout, stderr)
    if stub is None:
        print(f'No traces found', file=stderr)
        return
    print(stub.render(), file=stdout)


def main(argv: List[str], stdout: IO, stderr: IO) -> int:
    parser = argparse.ArgumentParser(
        description='Generate and apply stub files from collected type information.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--disable-type-rewriting',
        action='store_true', default=False,
        help='Show types without rewrite rules applied')
    parser.add_argument(
        '--include-unparsable-defaults',
        action='store_true', default=False,
        help="Include functions whose default values aren't valid Python expressions")
    parser.add_argument(
        '--limit',
        type=int, default=2000,
        help='How many traces to return from storage')
    parser.add_argument(
        '--config',
        type=monkeytype_config,
        default='monkeytype.config:DEFAULT_CONFIG',
        help='The <module>:<qualname> of the config to use.')
    subparsers = parser.add_subparsers()

    apply_parser = subparsers.add_parser(
        'apply',
        help='Generate and apply a stub',
        description='Generate and apply a stub')
    apply_parser.add_argument(
        'module_path',
        type=module_path,
        help="""A string of the form <module>[:<qualname>] (e.g.
my.module:Class.method). This specifies the set of functions/methods for which
we want to generate stubs.  For example, 'foo.bar' will generate stubs for
anything in the module 'foo.bar', while 'foo.bar:Baz' will only generate stubs
for methods attached to the class 'Baz' in module 'foo.bar'. See
https://www.python.org/dev/peps/pep-3155/ for a detailed description of the
qualname format.""")
    apply_parser.set_defaults(handler=apply_stub_handler)

    stub_parser = subparsers.add_parser(
        'stub',
        help='Generate a stub',
        description='Generate a stub')
    stub_parser.add_argument(
        'module_path',
        type=module_path,
        help="""A string of the form <module>[:<qualname>] (e.g.
my.module:Class.method). This specifies the set of functions/methods for which
we want to generate stubs.  For example, 'foo.bar' will generate stubs for
anything in the module 'foo.bar', while 'foo.bar:Baz' will only generate stubs
for methods attached to the class 'Baz' in module 'foo.bar'. See
https://www.python.org/dev/peps/pep-3155/ for a detailed description of the
qualname format.""")
    stub_parser.set_defaults(handler=print_stub_handler)

    args = parser.parse_args(argv)
    handler = getattr(args, 'handler', None)
    if handler is None:
        parser.print_help(file=stderr)
        return 1
    handler(args, stdout, stderr)
    return 0


def entry_point_main():
    """Wrapper for main() for setuptools console_script entry point."""
    sys.exit(main(sys.argv[1:], sys.stdout, sys.stderr))