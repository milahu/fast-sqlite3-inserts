#!/usr/bin/env python3

# codegen: parse from data to code
# https://github.com/kaitai-io/kaitai_struct/issues/1244

good_database_path = "test_kaitai.py.good.db"
codegen_database_path = "test_kaitai.py.codegen.db"

# this is useful for large databases (larger than RAM)
# parse_page_by_page = True
parse_page_by_page = False

import io
import re
import os
import sys
import shlex
import inspect
import subprocess

# https://github.com/kaitai-io/kaitai_struct_python_runtime
import kaitaistruct

import kaitaistruct_sqlite3
import vlq_base128_be
import pyvlq # https://github.com/osoken/pyvlq/blob/main/src/pyvlq/core.py

# create a lazy list class
# accessing root.pages[i] will call pages_list.__getitem__(i)
class PagesList:
    def __init__(self, root):
        self.root = root
    def __len__(self):
        return self.root.header.num_pages
    def __getitem__(self, i):  # i is 0-based
        root = self.root
        header = root.header
        if i < 0:  # -1 means last page, etc
            i = header.num_pages + i
        assert (
            0 <= i and i < header.num_pages
        ), f"page index is out of range: {i} is not in (0, {header.num_pages - 1})"
        # todo: maybe cache page
        # equality test: page_a.page_number == page_b.page_number
        _pos = root._io.pos()
        if i == 0:
            # The first 100 bytes of the database file comprise the database file header
            root._io.seek(100)
        else:
            root._io.seek(i * header.page_size)
        if 1:
            # use same _io
            _io = root._io
        else:
            # FIXME this is a waste of memory. we need a slice of root._io (aka child_stream?)
            # use a copy of _io
            page_size = header.page_size if i > 0 else (header.page_size - 100)
            # print(dir(root._io))
            _io = kaitaistruct.KaitaiStream(io.BytesIO(root._io.read_bytes(page_size)))
        n = i + 1 # page number
        if i == header.idx_lock_byte_page:
            page = kaitaistruct_sqlite3.Sqlite3.LockBytePage(n, _io, root, root._root)
        elif (
            i >= header.idx_first_ptrmap_page and
            i <= header.idx_last_ptrmap_page
        ):
            page = kaitaistruct_sqlite3.Sqlite3.PtrmapPage(n, _io, root, root._root)
        else:
            page = kaitaistruct_sqlite3.Sqlite3.BtreePage(n, _io, root, root._root)
        # FIXME this fails on i == 0
        # kaitaistruct.ValidationNotEqualError: /types/database_header/seq/0: at pos 116:
        # validation failed: not equal, expected b'SQLite format 3\x00',
        # but got b'\r\x00\x00\x00\x01\x0f\xcc\x00\x0f\xcc\x00\x00\x00\x00\x00\x00'
        # page._read()
        root._io.seek(_pos)
        return page

def print_value(on_kn):
    print(f"{on_kn} = ", end="")
    try:
      val = eval(on_kn)
      print(repr(val))
    except Exception as exc: print("error:", exc)

def get_keys(obj):
  # wontfix? this should only return "seq" keys, not "instances" keys
  keys = dir(obj)
  def f(k):
    if k[0] == "_": return False
    if "A" <= k[0] <= "Z": return False
    if k in ("close", "from_bytes", "from_file", "from_io", "pages__to_write"): return False
    return True
  keys = list(filter(f, keys))
  return keys

def get_seq(obj):
    # TODO upstream: this should be simpler
    if not hasattr(obj, "_read"):
        return []
    _read = getattr(obj, "_read")
    lines, firstlineno = inspect.getsourcelines(_read)
    lines.pop(0) # "def _read(self):"
    seq = []
    for line in lines:
        line = line.rstrip()
        # print("line", line)
        # builtin types
        # self.magic = self._io.read_bytes(16)
        m = re.match(r"\s+self\.(\w+) = self\._io\.read_(\w+)\((.*)\)", line)
        if m:
            # key, _type, args = m.groups()
            seq.append(m[1])
            continue
        # enum types
        # self.read_version = KaitaiStream.resolve_enum(Sqlite3.FormatVersion, self._io.read_u1())
        m = re.match(r"\s+self\.(\w+) = KaitaiStream\.resolve_enum\((\w+)\.(\w+), self\._io\.read_(\w+)\((.*)\)\)", line)
        if m:
            # key, enum_mod, enum_name, _type, args = m.groups()
            seq.append(m[1])
            continue
        # list type
        m = re.match(r"\s+self\.(\w+) = \[\]", line)
        if m:
            seq.append(m[1])
            continue
        # user-defined types
        # self.header = Sqlite3.DatabaseHeader(self._io, self, self._root)
        m = re.match(r"\s+self\.(\w+) = (\w+)\.(\w+)\(self\._io, self, self\._root\)", line)
        if m:
            # print("m", m.groups())
            # key, mod, member = m.groups()
            seq.append(m[1])
            continue
    return seq

def parse_enum_map(lines):
    enum_map = dict()
    line0 = lines.pop(0)
    for line in lines:
        line = line.rstrip()
        m = re.match(r"\s+(\w+) = (\d+)", line)
        if m:
            key, val = m.groups()
            # TODO are there non-int enum types?
            val = int(val)
            # enum_map[key] = val
            enum_map[val] = key
            continue
        print(f"FIXME parse_enum_map failed to parse line {line!r}")
    return enum_map

def get_local_key(key, global_names):
    num = 1
    local_key = key
    while local_key in global_names:
        if num == 1:
            local_key = f"local_{key}"
        else:
            local_key = f"local_{key}_{num}"
        num += 1
    return local_key

def get_class_qualname(mod_name, class_name):
    mod_class_qualname_list = get_mod_class_qualname_list(mod_name)
    # TODO
    raise 123

import functools

@functools.lru_cache(maxsize=100)
def get_mod_class_qualname_list(mod_name):
    """
    example source:

    class A # depth = 0 # A
        class B # 1 # A.B
            class C # 2 # A.B.C
            class D # 2 # A.B.D
    class E # 0 # E
    """
    # print("get_qualname", mod_name, class_name)
    mod = sys.modules[mod_name]
    lines, firstlineno = inspect.getsourcelines(mod)
    lines.pop(0) # "def _read(self):"
    seq = []
    # class_name_tree = dict()
    class_qualname_list = list()
    prev_depth = 0
    prev_class_qualname = []
    for line in lines:
        m = re.match(r"(\s*)class (\w+)\(([\w, ]+)\):", line)
        if not m: continue
        indent, name, args = m.groups()
        depth = len(indent) // 4
        """
        if depth == 0:
            # root class
            # class_name_tree[name] = dict()
            class_qualname = [name]
            class_qualname_list.append(class_qualname)
            prev_class_qualname = class_qualname
            continue
        assert depth > 0
        """
        if depth > prev_depth:
            assert depth == prev_depth + 1
        """
        if depth > prev_depth:
            assert depth == prev_depth + 1
            class_qualname = prev_class_qualname + [name]
        elif depth == prev_depth:
            class_qualname = prev_class_qualname[:-1] + [name]
        elif depth < prev_depth:
            class_qualname = prev_class_qualname[:depth] + [name]
        """
        class_qualname = prev_class_qualname[:depth] + [name]
        class_qualname_list.append(class_qualname)
        prev_class_qualname = class_qualname

        # class Sqlite3(ReadWriteKaitaiStruct):
        # class FormatVersion(IntEnum):


def codegen(
    obj,
    out,
    on="root",
    on_parent=None,
    # FIXME rename root to codegen_root
    # this can be different than the actual parser "root" class
    root=None,
    root_name="root",
    indent_step=4*" ",
    indent_level=0,
    enum_map_map={},
    module_map={},
    global_names=[],
):
    global val # fix print_value
    mod = obj.__class__.__module__
    # member = obj.__class__.__name__ # DatabaseHeader
    member = obj.__class__.__qualname__ # Sqlite3.DatabaseHeader
    """
    print("obj.__class__.__module__", obj.__class__.__module__)
    print("obj.__class__.__name__", obj.__class__.__name__)
    print("obj.__class__.__qualname__", obj.__class__.__qualname__)
    """
    is_root = True if on_parent == None else False
    if is_root:
        root = obj
        global_names.append("root")
        if 0:
            # test
            key = "header"
            global_names.append(key)
            global_names.append(f"local_{key}")
            global_names.append(f"local_{key}_2")
        global_names.append(mod)
        # TODO add imports of dependencies. example: vlq_base128_be for sqlite3
    # root_cln = root.__class__.__qualname__
    ind = indent_level * indent_step
    ids = indent_step
    if is_root:
        print(f"{ind}import io", file=out)
        print(f"{ind}import kaitaistruct", file=out)
        print(f"{ind}import {mod}", file=out)
        # TODO add imports of dependencies. example: vlq_base128_be for sqlite3
        # print(f"{ind}# root init", file=out)
        print("", file=out)
        print(f"{ind}root_size = {root._io._size}", file=out)
        print("", file=out)
        print(f"{ind}def get_{root_name}(_io=None, check=True):", file=out)
        print(f"{ind}{ids}if not _io:", file=out)
        print(f"{ind}{ids}{ids}_io = kaitaistruct.KaitaiStream(io.BytesIO(bytearray(root_size)))", file=out)
        print(f"{ind}{ids}{on} = {mod}.{member}(_io)", file=out)
        # TODO remove. this works only for sqlite3.ksy
        print(f"{ind}{ids}# try to fix root._write", file=out)
        print(f"{ind}{ids}# https://github.com/kaitai-io/kaitai_struct/issues/1245", file=out)
        print(f"{ind}{ids}{on}.pages__to_write = False", file=out)
        # root.pages__to_write = True
    # else:
    #     print(f"{ind}{ids}# non-root init", file=out)
    #     print(f"{ind}{ids}{on} = {mod}.{member}(_io, {on_parent}, {on_parent}._root)", file=out)
    for key in get_seq(obj):
        # print(f"{ind}{ids}# key {key}", file=out)
        val = getattr(obj, key)
        """
        print("key", repr(key))
        print("val", repr(val), dir(val))
        print_value("val.__class__.__module__")
        print_value("val.__class__.__qualname__")
        """
        # obj.__class__.__module__ == 'builtins'
        # TODO rename to "mod_name"
        mod = val.__class__.__module__
        # TODO rename to "member_name"
        member = val.__class__.__qualname__

        # builtin types: int, bytes, ...
        if mod == "builtins":
            if isinstance(val, int) and val > 10:
                print(f"{ind}{ids}{on}.{key} = {val!r} # {hex(val)}", file=out)
                continue
            if isinstance(val, bytes) and val == len(val) * b"\x00":
                # compress null bytes
                # TODO partial compression of bytestrings
                print(f"{ind}{ids}{on}.{key} = {len(val)} * b'\\x00'", file=out)
                continue
            # bytes, ...
            print(f"{ind}{ids}{on}.{key} = {val!r}", file=out)
            continue

        # enum types
        # class FormatVersion(IntEnum):
        # if mod != "builtins":
        # classtree = inspect.getclasstree(val)
        # print("classtree", val, val.__class__, classtree)
        # print("sourcelines", val, val.__class__)
        lines, firstlineno = inspect.getsourcelines(val.__class__)
        # for line in lines:
        #     print("line", line.rstrip())
        # lines.pop(0) # "def _read(self):"
        # print("line0", lines[0].rstrip())
        # class FreelistTrunkPagePointer(ReadWriteKaitaiStruct):
        # class FormatVersion(IntEnum):
        # m = re.match(r"\s*class (\w+)\((IntEnum)\):", lines[0].rstrip())
        m = re.match(r"\s*class (\w+)\(([A-Z][A-Za-z0-9]*Enum)\):", lines[0].rstrip())
        if m:
            enum_name, enum_type = m.groups()
            enum_map = enum_map_map.get(enum_name) # read cache
            if not enum_map:
                enum_map = parse_enum_map(lines)
                enum_map_map[enum_name] = enum_map # write cache
            enum_key = enum_map.get(val)
            # print("# enum type", enum_name, enum_type, file=out)
            val_str = str(val)
            if val > 10:
                val_str += f" = {hex(val)}"
            # enum_qualname = get_class_qualname(mod, enum_name)
            # this assumes that all enum classes are direct children of the parser_root class
            # FIXME rename root to codegen_root != parser_root
            parser_root_class_name = root.__class__.__qualname__.split(".")[0]
            enum_qualname = f"{parser_root_class_name}.{enum_name}"
            # print(f"{ind}{ids}{on}.{key} = {mod}.{root_cln}.{enum_name}.{enum_key} # {val_str}", file=out)
            # print(f"{ind}{ids}{on}.{key} = {mod}.{enum_name}.{enum_key} # {val_str}", file=out)
            print(f"{ind}{ids}{on}.{key} = {mod}.{enum_qualname}.{enum_key} # {val_str}", file=out)
            continue

        # TODO handle list types
        # m = ...
        # if m:
        #     ...
        #     continue

        # user-defined types
        # https://doc.kaitai.io/serialization.html#_user_defined_types
        # print(f"{ind}{ids}{on}.{key} = root.{member}(root._io, {on}, {on}._root)", file=out) # short
        # print(f"{ind}{ids}{on}.{key} = {mod}.{root_cln}.{member}(root._io, {on}, {on}._root)", file=out) # long
        print(f"{ind}{ids}{on}.{key} = {mod}.{member}(root._io, {on}, {on}._root)", file=out) # long
        # avoid shadowing global variables
        local_key = get_local_key(key, global_names)
        # print(f"{ind}{ids}if 1:", file=out) # no block scope
        # print(f"{ind}{ids}if {local_key} := {on}.{key}:", file=out) # no block scope
        # TypeError: 'int' object does not support the context manager protocol
        # print(f"{ind}{ids}with {on}.{key} as {local_key}:", file=out) # context # no block scope?
        # create block scope
        # this is required to avoid name collisions between scopes
        # https://stackoverflow.com/a/45210833/10440128
        print(f"{ind}{ids}def init_{key}({local_key}):", file=out) # "init_" prefix
        # print(f"{ind}{ids}def {key}_init({local_key}):", file=out) # "_init" suffix
        # recursion
        codegen(
            val,
            out,
            local_key,
            on,
            root,
            root_name,
            indent_step,
            (indent_level + 1),
            enum_map_map,
            module_map,
            global_names,
        )
        print(f"{ind}{ids}init_{key}({on}.{key})", file=out) # "init_" prefix
        # print(f"{ind}{ids}{key}_init({local_key})", file=out) # "_init" suffix

    # some user-defined types need this
    # example: AttributeError: 'VlqBase128Be' object has no attribute 'groups'
    # but this breaks other cases...
    # kaitaistruct.ValidationNotEqualError: /types/database_header/seq/0: at pos 20: validation failed: not equal,
    # expected b'SQLite format 3\x00', but got b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    # print(f"{ind}{ids}{on}._read()", file=out)

    if is_root:
        print(f"{ind}{ids}if check:", file=out)
        print(f"{ind}{ids}{ids}{on}._check()", file=out)
        print(f"{ind}{ids}return {on}", file=out)
    if is_root:
        print("", file=out)
        print(f"{ind}def get_io():", file=out)
        print(f"{ind}{ids}{root_name} = get_{root_name}()", file=out)
        print(f"{ind}{ids}_io = {root_name}._io", file=out)
        # print(f"{ind}{ids}_io.seek(0)", file=out)
        print(f"{ind}{ids}if 1:", file=out)
        print(f"{ind}{ids}{ids}# no. _write calls _fetch_instances which throws", file=out)
        print(f"{ind}{ids}{ids}{root_name}._write(_io)", file=out)
        print(f"{ind}{ids}else:", file=out)
        print(f"{ind}{ids}{ids}{root_name}._write__seq(_io)", file=out)
        print(f"{ind}{ids}{ids}# {root_name}._fetch_instances() # this would throw", file=out)
        print(f"{ind}{ids}{ids}{root_name}._io.write_back_child_streams()", file=out)
        print(f"{ind}{ids}return _io", file=out)
        print("", file=out)
        print(f"{ind}def get_bytes():", file=out)
        print(f"{ind}{ids}_io = get_io()", file=out)
        print(f"{ind}{ids}_io.seek(0)", file=out)
        print(f"{ind}{ids}return _io.read_bytes_full()", file=out)

if not os.path.exists(good_database_path):
  args = [
    "sqlite3",
    good_database_path,
    "create table test (id INTEGER)",
  ]
  print(">", shlex.join(args))
  subprocess.run(args)

database_header_size = 100

database_size = os.path.getsize(good_database_path)
print(f"database_size {database_size}")

# database_page_size = 4096 # default
args = [
  "sqlite3",
  good_database_path,
  "pragma page_size",
]
database_page_size = int(subprocess.check_output(args, text=True))
print(f"database_page_size {database_page_size}")

with open(good_database_path, "rb") as f:
  good_database_header_bytes = f.read(database_header_size)

assert database_size % database_page_size == 0

database_num_pages = database_size // database_page_size
print(f"database_num_pages {database_num_pages}")




# print(cell.content, dir(cell.content))

r"""
res = io.StringIO()
on_root = "root"
on = on_root
on_parent = on_root
root_class_name = root.__class__.__qualname__
"""


r"""
mod = root.__class__.__module__ # "kaitaistruct_sqlite3"
mod = root_class_name # "Sqlite3"
mod = module_map.get(mod, mod)
print(f"{on_root} = {mod}.{root_class_name}()", file=res)
print_value("inspect.getsourcelines(root._read)")
print_value("inspect.getclosurevars(root._read)")
print_value("inspect.unwrap(root._read)")
print_value("inspect.get_annotations(root._read)")
"""

r'''
lines, firstlineno = inspect.getsourcelines(root._read)
lines.pop(0) # "def _read(self):"
for line in lines:
    line = line.rstrip()
    print("line", line)
    m = re.match(r"\s+self\.(\w+) = (\w+)\.(\w+)\(self\._io, self, self\._root\)", line)
    if m:
        print("m", m.groups())
        key, mod, member = m.groups()
        mod = module_map.get(mod, mod)
        # https://doc.kaitai.io/serialization.html#_user_defined_types
        """
        if on == "root":
            print(f"{on}.{key} = {mod}.{member}()", file=res)
        else:
            print(f"{on}.{key} = {mod}.{member}(None, {on_parent}, {on_parent}._root)", file=res)
        """
        print(f"{on}.{key} = {mod}.{member}(None, {on_parent}, {on_parent}._root)", file=res)
'''



if not parse_page_by_page:
  parse_chunk_list = [(
    0,
    database_size,
    kaitaistruct_sqlite3.Sqlite3,
    None,
    "root",
  )]
else:
  parse_chunk_list = [(
    0,
    100,
    kaitaistruct_sqlite3.Sqlite3.DatabaseHeader,
    None,
    "header",
  )]
  for page_idx in range(database_num_pages):
    page_num = page_idx + 1
    if page_idx == 0:
      parse_chunk_list += [(
        100,
        (database_page_size - 100),
        kaitaistruct_sqlite3.Sqlite3.BtreePage,
        (page_num,),
        f"page_{page_num}",
      )]
    else:
      parse_chunk_list += [(
        page_idx * database_page_size,
        (page_idx + 1) * database_page_size,
        kaitaistruct_sqlite3.Sqlite3.BtreePage,
        (page_num,),
        f"page_{page_num}",
      )]

# TODO?
output_file = open("test_kaitai.py.codegen.db", "wb")
# TODO output_file.write(...)

# rename imports
module_map = {
    "Sqlite3": "kaitaistruct_sqlite3",
}

# out = io.StringIO()
# codegen(root, out, module_map=module_map)

for parse_chunk in parse_chunk_list:

  (
    parse_chunk_offset,
    parse_chunk_size,
    parse_chunk_parser,
    parse_chunk_parser_args,
    parse_chunk_root_name,
  ) = parse_chunk

  if not parse_chunk_parser_args:
      parse_chunk_parser_args = tuple()

  # create a database parser
  # FIXME pass parse_chunk_parser_args
  """
  if parse_page_by_page:
    with open(good_database_path, "rb") as f:
      f.seek(parse_chunk_offset)
      _bytes = f.read(parse_chunk_size)
    # root = kaitaistruct_sqlite3.Sqlite3.from_bytes(good_database_header_bytes)
    root = parse_chunk_parser.from_bytes(_bytes)
  else:
    # root = kaitaistruct_sqlite3.Sqlite3.from_file(good_database_path)
    root = parse_chunk_parser.from_file(good_database_path)
  """

  bytes_io = io.BytesIO() # store all input bytes in RAM
  with open(good_database_path, "rb") as f:
    f.seek(parse_chunk_offset)
    # _bytes = f.read(parse_chunk_size)
    bytes_io.write(f.read(parse_chunk_size))
  bytes_io.seek(0)
  _io = kaitaistruct.KaitaiStream(bytes_io)

  print(f"calling parser {parse_chunk_parser.__module__}.{parse_chunk_parser.__qualname__}")
  # FIXME rename to codegen_root
  root = parse_chunk_parser(*parse_chunk_parser_args, _io=_io)

  # TODO remove?
  if parse_page_by_page:
    # patch the internal cache attribute of root.pages
    root._m_pages = PagesList(root)

  root._read()

  # debug: print some values
  if 0:
      # print("root.header.magic", root.header.magic)
      # now, this will parse **only** the first page
      # fix: 'BtreePage' object has no attribute 'cell_pointers'
      if not parse_page_by_page:
        for page_idx in range(database_num_pages):
          # print(f"root.pages[{page_idx}] keys:", get_keys(root.pages[page_idx]))
          # print(f"root.pages[{page_idx}] seq:", get_seq(root.pages[page_idx]))
          # FIXME kaitaistruct.ValidationNotEqualError: /types/database_header/seq/0: at pos 116: validation failed: not equal,
          # expected b'SQLite format 3\x00', but got b'\r\x00\x00\x00\x01\x0f\xcc\x00\x0f\xcc\x00\x00\x00\x00\x00\x00'
          print(f"root.pages[{page_idx}]._read()"); root.pages[page_idx]._read()
          # print(f"root.pages[{page_idx}] keys:", get_keys(root.pages[page_idx]))
          # print(f"root.pages[{page_idx}] seq:", get_seq(root.pages[page_idx]))
          print_value(f"root.pages[{page_idx}]")
          # FIXME root.pages[{page_idx}].page_type = error: 'BtreePage' object has no attribute 'page_type'
          print_value(f"root.pages[{page_idx}].page_type")
          print_value(f"root.pages[{page_idx}].num_cells")
          for cell_idx in range(1):
            print_value(f"root.pages[{page_idx}].cell_pointers[{cell_idx}]")
            print_value(f"root.pages[{page_idx}].cell_pointers[{cell_idx}].ofs_content")

  out = io.StringIO() # store all output code in RAM
  codegen(root, out, module_map=module_map, root_name=parse_chunk_root_name)

  print("codegen result:")
  print(out.getvalue())
  with open("codegen_result.py", "w") as f:
      f.write(out.getvalue())

  import codegen_result
  codegen_bytes = codegen_result.get_bytes()
  if codegen_bytes == len(codegen_bytes) * b"\x00":
      raise Exception("codegen_bytes are only null bytes")
  with open(codegen_database_path, "wb") as f:
      f.write(codegen_bytes)

  # TODO rewrite diff in python
  args = [
    "diff", "--color=always", "-u",
    # TODO seek?
    "<(", "xxd", codegen_database_path, ")", # red
    f"<( head -c{parse_chunk_offset + parse_chunk_size} {good_database_path} | tail -c{parse_chunk_size} | xxd )", # green
    "|", "head", "-n100",
  ]
  args = ["bash", "-c", " ".join(args)]
  print(">", shlex.join(args))
  subprocess.run(args)
