"""Symlink targets of an EROFS image or RAFS v6 bootstrap, read byte-exact.

A strict reader of the on-disk layout in Linux fs/erofs/erofs_fs.h: block 0
(superblock and compression configs), inode records with their inline
xattrs, tail-packed inline data and compressed or chunk index arrays, shared
xattrs, and directory and symlink blocks. Regular-file data blocks are never
read. A feature or layout that has not been qualified is refused with
``UnsupportedErofs`` rather than guessed.
"""
import stat
import struct

SUPER_OFFSET = 1024
MAGIC = 0xE0F5E1E2
_SUPER = struct.Struct("<IIIBBHQQIIII")
_COMPAT_KNOWN = 0x1 | 0x2 | 0x4  # SB_CHKSUM, MTIME, XATTR_FILTER: no metadata moves.
_ZERO_PADDING, _COMPR_CFGS, _CHUNKED_FILE = 0x1, 0x2, 0x4  # COMPR_CFGS is also BIG_PCLUSTER.
# DEVICE_TABLE/COMPR_HEAD2, ZTAILPACKING, FRAGMENTS/DEDUPE, XATTR_PREFIXES,
# 48BIT and METABOX add or move metadata that no qualified image exercises.
_INCOMPAT_KNOWN = _ZERO_PADDING | _COMPR_CFGS | _CHUNKED_FILE
_DEVICE_TABLE, _COMPAT_RAFS_V6 = 0x8, 0x40000000  # RAFS v6 blob devices and marker: symlink_targets only.
_FT_DIR, _FT_SYMLINK = 2, 7
FLAT_PLAIN, COMPRESSED_FULL, FLAT_INLINE, COMPRESSED_COMPACT, CHUNK_BASED = range(5)
_COMPACTED_2B, _BIG_PCLUSTER = 0x1, 0x2 | 0x4
_CHUNK_FORMAT_KNOWN, _CHUNK_INDEXES = 0x3F, 0x20
_SPECIAL = {stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK}


class UnsupportedErofs(ValueError):
    """The image is not EROFS or uses a layout this walker has not qualified."""


def _align(value, unit):
    return (value + unit - 1) // unit * unit


def _compact_index_end(base, total, advise):
    """End of the compact index pack holding the last logical cluster.

    z_erofs_load_compact_lcluster: 4-byte entries up to 32-byte alignment,
    then 2-byte packs of 16 (COMPACTED_2B), then 4-byte packs of 2. The kernel
    reads the whole pack containing the logical cluster it maps.
    """
    initial = (32 - base % 32) // 4 % 8
    packed = (total - initial) // 16 * 16 if advise & _COMPACTED_2B and initial < total else 0
    lcn, position, shift = total - 1, base, 2
    if lcn >= initial:
        position, lcn = position + 4 * initial, lcn - initial
        if lcn < packed:
            shift = 1
        else:
            position, lcn = position + 2 * packed, lcn - packed
    position += lcn << shift
    pack = (16 if shift == 1 else 2) << shift
    return position // pack * pack + pack


class _Walker:
    def __init__(self, view, size):
        self.view, self.size = view, size
        self.shared = set()

    def need(self, start, length, what):
        if start < 0 or length < 0 or start + length > self.size:
            raise ValueError(f"EROFS {what} lies outside the image")

    add = need  # Metadata the kernel reads: it must lie inside the image.

    def unpack(self, layout, offset):
        self.need(offset, struct.calcsize(layout), "field")
        return struct.unpack_from(layout, self.view, offset)

    def superblock(self, devices=False):
        self.need(SUPER_OFFSET, 128, "superblock")
        (magic, _checksum, compat, blkszbits, extslots, root_nid, inos, _epoch, _nsec, blocks,
         meta_blkaddr, xattr_blkaddr) = _SUPER.unpack_from(self.view, SUPER_OFFSET)
        incompat, algorithms, extra_devices = self.unpack("<IHH", SUPER_OFFSET + 80)
        dirblkbits, prefix_count = self.unpack("<BB", SUPER_OFFSET + 90)
        (packed_nid,) = self.unpack("<Q", SUPER_OFFSET + 96)
        if magic != MAGIC:
            raise UnsupportedErofs("not an EROFS image")
        known = _INCOMPAT_KNOWN | (_DEVICE_TABLE if devices else 0)
        if compat & ~(_COMPAT_KNOWN | (_COMPAT_RAFS_V6 if devices else 0)) or incompat & ~known:
            raise UnsupportedErofs(f"EROFS features compat={compat:#x} incompat={incompat:#x} are not qualified")
        if blkszbits != 12 or (extra_devices and not devices) or dirblkbits or prefix_count or packed_nid:
            raise UnsupportedErofs("EROFS block size, extra devices or packed metadata are not qualified")
        self.blkszbits, self.blksz = blkszbits, 1 << blkszbits
        if devices:
            pass  # A RAFS bootstrap counts its blob devices' blocks too; reads stay within ``size``.
        elif not 0 < blocks * self.blksz <= self.size:
            raise ValueError("EROFS block count exceeds the image")
        else:
            self.size = blocks * self.blksz
        self.meta_base, self.xattr_base = meta_blkaddr * self.blksz, xattr_blkaddr * self.blksz
        self.chunked, self.big_pcluster = bool(incompat & _CHUNKED_FILE), bool(incompat & _COMPR_CFGS)
        self.inos = inos
        # The kernel reads all of block 0; compression configs follow the
        # superblock and its extension slots, one record per algorithm.
        end = SUPER_OFFSET + 128 + 16 * extslots
        if incompat & _COMPR_CFGS:
            for bit in range(16):
                if algorithms >> bit & 1:
                    end = _align(end, 4)
                    end += 2 + (self.unpack("<H", end)[0] or 65536)
        self.add(0, max(end, self.blksz), "superblock")
        return root_nid

    def entry(self, offset, end, what):
        name_len, index, value_size = self.unpack("<BBH", offset)
        if index & 0x80:
            raise UnsupportedErofs("EROFS long xattr name prefixes are not qualified")
        length = _align(4 + name_len + value_size, 4)
        if offset + length > end:
            raise ValueError(f"EROFS {what} exceeds its bound")
        return length

    def xattrs(self, base, length):
        if not length:
            return
        self.need(base, length, "inline xattrs")
        shared = self.view[base + 4]
        if 12 + 4 * shared > length:
            raise ValueError("EROFS shared xattr count exceeds the inline area")
        for index in range(shared):
            (xattr_id,) = self.unpack("<I", base + 12 + 4 * index)
            if xattr_id not in self.shared:
                self.shared.add(xattr_id)
                offset = self.xattr_base + 4 * xattr_id
                self.add(offset, self.entry(offset, self.size, "shared xattr"), "shared xattr")
        offset, end = base + 12 + 4 * shared, base + length
        while offset < end:
            offset += self.entry(offset, end, "inline xattr")

    def compressed_end(self, tail, size, layout):
        if not size:
            return tail  # The map header is read lazily, on the first data map.
        header = _align(tail, 8)
        advise, _algorithms, cluster_bits = self.unpack("<HBB", header + 4)
        if cluster_bits & 0xF8 or advise & ~(_COMPACTED_2B | _BIG_PCLUSTER):
            # Fragments, tail-packed (inline) or interlaced pclusters.
            raise UnsupportedErofs(f"EROFS compression advise {advise:#x}/{cluster_bits:#x} is not qualified")
        if advise & _BIG_PCLUSTER and not self.big_pcluster:
            raise ValueError("EROFS big pcluster without its feature")
        lcluster_bits = self.blkszbits + (cluster_bits & 7)
        if lcluster_bits > 14:
            raise UnsupportedErofs("EROFS logical cluster size is not qualified")
        total = (size + (1 << lcluster_bits) - 1) >> lcluster_bits
        if layout == COMPRESSED_FULL:
            # Z_EROFS_FULL_INDEX_ALIGN: 8-byte header, 8 reserved, 8-byte indexes.
            return header + 16 + 8 * total
        return _compact_index_end(header + 8, total, advise)

    def chunk_index_end(self, tail, size, i_u):
        if not self.chunked:
            raise ValueError("EROFS chunk-based inode without the chunked-file feature")
        chunk_format = i_u & 0xFFFF
        if chunk_format & ~_CHUNK_FORMAT_KNOWN:
            raise UnsupportedErofs(f"EROFS chunk format {chunk_format:#x} is not qualified")
        unit = 8 if chunk_format & _CHUNK_INDEXES else 4
        bits = self.blkszbits + (chunk_format & 0x1F)
        count = (size + (1 << bits) - 1) >> bits
        start = _align(tail, unit)
        self.need(start, count * unit, "chunk index")
        if unit == 8 and any(self.unpack("<H", start + 8 * index + 2)[0] for index in range(count)):
            raise UnsupportedErofs("EROFS chunks on extra devices are not qualified")
        return start + count * unit

    def inode(self, nid):
        """Record one inode; return (type, layout, size, tail, i_u, blocks, end)."""
        offset = self.meta_base + 32 * nid
        i_format, xattr_count, mode = self.unpack("<HHH", offset)
        if i_format & ~0xF:
            raise UnsupportedErofs(f"EROFS inode format {i_format:#x} is not qualified")
        layout, kind = i_format >> 1 & 7, stat.S_IFMT(mode)
        if i_format & 1:
            inode_bytes, (size,) = 64, self.unpack("<Q", offset + 8)
        else:
            inode_bytes, (size,) = 32, self.unpack("<I", offset + 8)
        self.need(offset, inode_bytes, "inode")
        (i_u,) = self.unpack("<I", offset + 16)
        xattr_bytes = 12 + 4 * (xattr_count - 1) if xattr_count else 0
        self.xattrs(offset + inode_bytes, xattr_bytes)
        tail = end = offset + inode_bytes + xattr_bytes
        blocks = (size + self.blksz - 1) >> self.blkszbits
        if kind in _SPECIAL:
            if layout not in (FLAT_PLAIN, FLAT_INLINE) or size:
                raise ValueError("EROFS special inode has data")
        elif kind not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK):
            raise ValueError("EROFS inode has an unknown file type")
        elif layout == FLAT_INLINE and size:
            inline = size - ((blocks - 1) << self.blkszbits)
            if tail % self.blksz + inline > self.blksz:
                raise ValueError("EROFS inline data crosses a block boundary")
            end = tail + inline
        elif layout == CHUNK_BASED and kind == stat.S_IFREG:
            end = self.chunk_index_end(tail, size, i_u)
        elif layout in (COMPRESSED_FULL, COMPRESSED_COMPACT) and kind == stat.S_IFREG:
            end = self.compressed_end(tail, size, layout)
        elif layout not in (FLAT_PLAIN, FLAT_INLINE):
            # mkfs.erofs never chunks or compresses directories and symlinks.
            raise UnsupportedErofs(f"EROFS layout {layout} for file type {kind:#o} is not qualified")
        self.add(offset, end - offset, "inode")
        if kind in (stat.S_IFDIR, stat.S_IFLNK):
            full = blocks if layout == FLAT_PLAIN else max(blocks - 1, 0)
            if full:
                self.add(i_u << self.blkszbits, full << self.blkszbits, "metadata block")
        return kind, layout, size, tail, i_u, blocks, end

def _inline_data(walker, size, tail, i_u, blocks, layout):
    """A directory's or symlink's bytes: whole blocks at ``i_u``, then the tail."""
    full = blocks if layout == FLAT_PLAIN else max(blocks - 1, 0)
    head, data = min(full << walker.blkszbits, size), b""
    if head:  # Fully inline data leaves i_u unused.
        walker.need(i_u << walker.blkszbits, head, "metadata block")
        data = bytes(walker.view[i_u << walker.blkszbits:(i_u << walker.blkszbits) + head])
    if layout == FLAT_INLINE and size > head:
        walker.need(tail, size - head, "inline data")
        data += bytes(walker.view[tail:tail + size - head])
    return data


def symlink_targets(view):
    """{path: target} for every symlink, byte-exact, by a name walk from the
    root. It reads only directory and symlink inodes, so a RAFS v6 bootstrap's
    blob devices are allowed: rollback restores the targets ``nydus-image
    unpack``'s tar writer normalizes (``.././x`` becomes ``../x``)."""
    walker = _Walker(view, len(view))
    pending, targets, seen = [(walker.superblock(devices=True), "")], {}, set()
    while pending:
        nid, path = pending.pop()
        kind, layout, size, tail, i_u, blocks, _ = walker.inode(nid)
        if layout not in (FLAT_PLAIN, FLAT_INLINE):
            raise UnsupportedErofs(f"EROFS layout {layout} for a directory or symlink is not qualified")
        data = _inline_data(walker, size, tail, i_u, blocks, layout)
        if kind == stat.S_IFLNK:
            targets[path] = data.decode("utf-8", "surrogateescape")
            continue
        for start in range(0, len(data), walker.blksz):
            block = data[start:start + walker.blksz]
            (first,) = struct.unpack_from("<H", block, 8)
            entries = [struct.unpack_from("<QHB", block, 12 * index) for index in range(first // 12)]
            for index, (child, offset, file_type) in enumerate(entries):
                end = entries[index + 1][1] if index + 1 < len(entries) else len(block)
                name = bytes(block[offset:end]).split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
                if name not in (".", "..") and (file_type == _FT_SYMLINK or file_type == _FT_DIR and child not in seen):
                    seen.add(child)  # A symlink's hardlinks each need their path.
                    pending.append((child, f"{path}/{name}" if path else name))
    return targets

