import io
import kaitaistruct
import kaitaistruct_sqlite3

root_size = 100

def get_root(_io=None, check=True):
    if not _io:
        _io = kaitaistruct.KaitaiStream(io.BytesIO(bytearray(root_size)))
    root = kaitaistruct_sqlite3.Sqlite3(_io)
    root.header = kaitaistruct_sqlite3.Sqlite3.DatabaseHeader(root._io, root, root._root)
    header = root.header
    def init_header(header):
        header.magic = b'SQLite format 3\x00'
        header.page_size_raw = 4096 # 0x1000
        header.write_version = kaitaistruct_sqlite3.Sqlite3.FormatVersion.legacy # 1
        header.read_version = kaitaistruct_sqlite3.Sqlite3.FormatVersion.legacy # 1
        header.page_reserved_space_size = 0
        header.max_payload_fraction = 64 # 0x40
        header.min_payload_fraction = 32 # 0x20
        header.leaf_payload_fraction = 32 # 0x20
        header.file_change_counter = 1
        header.num_pages = 2
        header.first_freelist_trunk_page = kaitaistruct_sqlite3.Sqlite3.FreelistTrunkPagePointer(root._io, header, header._root)
        first_freelist_trunk_page = header.first_freelist_trunk_page
        def init_first_freelist_trunk_page(first_freelist_trunk_page):
            first_freelist_trunk_page.page_number = 0
        init_first_freelist_trunk_page(first_freelist_trunk_page)
        header.num_freelist_pages = 0
        header.schema_cookie = 1
        header.schema_format = 4
        header.default_page_cache_size = 0
        header.largest_root_page = 0
        header.text_encoding = 1
        header.user_version = 0
        header.is_incremental_vacuum = 0
        header.application_id = 0
        header.reserved_header_bytes = 20 * b'\x00'
        header.version_valid_for = 1
        header.sqlite_version_number = 3050001 # 0x2e8a11
    init_header(header)
    if check:
        root._check()
    return root

def get_io():
    root = get_root()
    _io = root._io
    # no. _write calls _fetch_instances which throws
    # root._write(_io)
    root._write__seq(_io)
    # root._fetch_instances() # this would throw
    root._io.write_back_child_streams()
    return _io

def get_bytes():
    _io = get_io()
    _io.seek(0)
    return _io.read_bytes_full()
