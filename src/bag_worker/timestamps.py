"""Shared piecewise time mapping and schema-aware, byte-preserving CDR edits."""
from __future__ import annotations

import bisect
import struct
from pathlib import Path

from rosbags.typesys import Stores, get_typestore, get_types_from_idl, get_types_from_msg
from rosbags.interfaces import Nodetype


class TimeMap:
    def __init__(self, start_ns, ranges_ns):
        self.ranges = [(start_ns+a, start_ns+b) for a,b in ranges_ns]
        self.starts = [a for a,_ in self.ranges]
        self.totals = [0]
        for a,b in self.ranges:
            self.totals.append(self.totals[-1]+b-a)

    def __call__(self, stamp):
        index = bisect.bisect_right(self.starts, stamp)-1
        if index < 0:
            return stamp
        a,b = self.ranges[index]
        return stamp-self.totals[index]-min(stamp-a,b-a)


def load_definitions(text, name, encoding='ros2msg'):
    if encoding == 'ros2idl' or text.startswith('='*80+'\nIDL: '):
        if text.startswith('='*80+'\nIDL: '):
            types = {}
            for part in text.split('='*80+'\n')[1:]:
                types.update(get_types_from_idl(part.split('\n',1)[1]))
            return types
        return get_types_from_idl(text)
    return get_types_from_msg(text, name)


def make_typestore(definitions=(), custom_dir=None, distro='humble'):
    store_name = 'ROS2_'+distro.upper()
    store = get_typestore(getattr(Stores, store_name, Stores.ROS2_HUMBLE))
    extra = {}
    for name, text, encoding in definitions:
        if text.strip():
            extra.update(load_definitions(text, name, encoding))
    if custom_dir:
        root = Path(custom_dir).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('自定义消息定义目录不存在')
        found = list(root.rglob('*.msg')) + list(root.rglob('*.idl'))
        if not found:
            raise ValueError('自定义目录中没有 .msg 或 .idl 文件')
        for path in found:
            if path.suffix == '.idl':
                extra.update(get_types_from_idl(path.read_text()))
            elif path.parent.name == 'msg':
                name = f'{path.parent.parent.name}/msg/{path.stem}'
                extra.update(get_types_from_msg(path.read_text(), name))
    # Definitions embedded in the recording take precedence over distro defaults.
    if extra:
        combined = dict(store.fielddefs)
        combined.update(extra)
        store = get_typestore(Stores.EMPTY)
        store.register(combined)
    return store


class TimestampRewriter:
    """Only builtin_interfaces/Time is changed; durations and payload bytes stay intact."""
    SIZES = {'bool':1,'byte':1,'char':1,'int8':1,'uint8':1,'int16':2,'uint16':2,
             'int32':4,'uint32':4,'float32':4,'int64':8,'uint64':8,'float64':8}

    def __init__(self, store, mapping):
        self.store, self.mapping = store, mapping
        self._support = {}
        self.changed = 0
        self.clamped = 0

    def supported(self, msgtype, seen=None):
        if msgtype in self._support:
            return self._support[msgtype]
        seen = set() if seen is None else seen
        if msgtype in seen:
            return True
        seen.add(msgtype)
        if msgtype not in self.store.fielddefs:
            return False
        def check(desc):
            kind, value = desc
            if kind == Nodetype.NAME:
                return self.supported(value, seen)
            if kind in (Nodetype.ARRAY, Nodetype.SEQUENCE):
                return check(value[0])
            return value[0] == 'string' or value[0] in self.SIZES
        ok = all(check(desc) for _,desc in self.store.fielddefs[msgtype][1])
        self._support[msgtype] = ok
        return ok

    def rewrite(self, data, msgtype):
        if not self.supported(msgtype):
            raise ValueError(f'缺少完整消息定义：{msgtype}')
        if bytes(data[:2]) not in (b'\x00\x00',b'\x00\x01'):
            raise ValueError(f'{msgtype} 不是支持的 CDR1 编码，无法可靠修改内部时间戳')
        endian = '<' if data[1] else '>'
        # All offsets are relative to the 4-byte CDR encapsulation header.
        raw = memoryview(data)[4:]
        changes = []
        def number(pos, fmt, size):
            pos = (pos+size-1)//size*size
            return struct.unpack_from(endian+fmt,raw,pos)[0],pos+size
        def walk_type(name,pos):
            if name == 'builtin_interfaces/msg/Time':
                at = (pos+3)//4*4
                sec,ns = struct.unpack_from(endian+'iI',raw,at)
                if ns >= 10**9:
                    raise ValueError(f'{msgtype} 含无效纳秒字段')
                old = sec*10**9+ns
                if old != 0:  # ROS uses all-zero stamps as an unspecified-time sentinel.
                    new = self.mapping(old)
                    if new != old:
                        s,n=divmod(new,10**9)
                        if not -(2**31) <= s < 2**31:
                            raise ValueError('压缩后的 ROS Time 超出 int32 秒范围')
                        changes.append((at,struct.pack(endian+'iI',s,n)))
                        idx=bisect.bisect_right(self.mapping.starts,old)-1
                        if idx>=0 and old<self.mapping.ranges[idx][1]:
                            self.clamped+=1
                return at+8
            for _,desc in self.store.fielddefs[name][1]:
                pos=walk(desc,pos)
            return pos
        def walk(desc,pos):
            kind,value=desc
            if kind == Nodetype.NAME:
                return walk_type(value,pos)
            if kind == Nodetype.BASE:
                typename=value[0]
                if typename == 'string':
                    n,pos=number(pos,'I',4)
                    return pos+n
                size=self.SIZES[typename]
                return (pos+size-1)//size*size+size
            child,n=value
            if kind == Nodetype.SEQUENCE:
                n,pos=number(pos,'I',4)
            if child[0] == Nodetype.BASE and child[1][0] in self.SIZES:
                if n:
                    size=self.SIZES[child[1][0]]
                    pos=(pos+size-1)//size*size+n*size
                return pos
            for _ in range(n):
                pos=walk(child,pos)
                if pos>len(raw):
                    raise ValueError(f'{msgtype} 消息长度与定义不一致')
            return pos
        end=walk_type(msgtype,0)
        if end>len(raw) or len(raw)-end>3:
            raise ValueError(f'{msgtype} 消息长度与定义不一致，请提供对应录制版本的定义')
        if not changes:
            return data
        edited=bytearray(data)
        for offset,packed in changes:
            edited[offset+4:offset+12]=packed
        self.changed+=len(changes)
        return bytes(edited)
