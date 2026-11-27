"""compressed_depth_image_transport 的 RVL 负载解析与解码（numpy 向量化）。

负载布局（规范第一篇 §3.2：ConfigHeader + 宽高 + RVL 数据，非裸 RVL）：

  ConfigHeader { int32 format; float32 depthParam[2]; }   12 字节
  uint32 cols; uint32 rows                                   8 字节
  RVL 数据：int32 字序列，每字从高位到低位依次 8 个 nibble

字节序按小端（采集主机为 x86/ARM 小端）。RVL 编码见 Wilson 2017 与 rvl_codec.cpp：
每个游程为 VLE(zeros)、VLE(nonzeros)，随后 nonzeros 个 zigzag 编码的差分值；
VLE 每 nibble 低 3 位为负载、最高位为续位，低位组在前。
"""
from __future__ import annotations

import struct

import numpy as np

CONFIG_HEADER_SIZE = 12
DIMS_SIZE = 8
FORMAT_PREFIX = "16UC1; compressedDepth rvl"
_SHIFTS = np.array([28, 24, 20, 16, 12, 8, 4, 0], dtype=np.uint32)


class RvlError(ValueError):
    pass


def parse_payload(data: bytes) -> dict:
    """解析 ConfigHeader 与宽高；不解码 RVL。长度不足时抛 RvlError。"""
    if len(data) < CONFIG_HEADER_SIZE + DIMS_SIZE:
        raise RvlError(f"负载只有 {len(data)} 字节，不足 ConfigHeader + 宽高")
    fmt, p0, p1 = struct.unpack_from("<iff", data, 0)
    cols, rows = struct.unpack_from("<II", data, CONFIG_HEADER_SIZE)
    bare_cols, bare_rows = struct.unpack_from("<II", data, 0)
    return {
        "config_format": fmt,
        "depth_params": (p0, p1),
        "cols": cols,
        "rows": rows,
        "rvl_bytes": len(data) - CONFIG_HEADER_SIZE - DIMS_SIZE,
        # 若按裸 RVL（无 ConfigHeader）解读得到的宽高，用于识别缺少 ConfigHeader 的负载
        "bare_cols": bare_cols,
        "bare_rows": bare_rows,
    }


def decode_rvl(buf: bytes, num_pixels: int) -> np.ndarray:
    """解码 RVL 数据为 uint16 一维数组；格式不合法时抛 RvlError。"""
    if num_pixels <= 0:
        raise RvlError("像素数为 0")
    if len(buf) % 4:
        raise RvlError(f"RVL 数据长度 {len(buf)} 不是 4 的倍数")
    if not buf:
        raise RvlError("RVL 数据为空")
    words = np.frombuffer(buf, dtype="<u4")
    nib = ((words[:, None] >> _SHIFTS) & 0xF).astype(np.uint8).ravel()
    ends = (nib & 8) == 0
    end_idx = np.flatnonzero(ends)
    if end_idx.size < 2:
        raise RvlError("RVL 数据中没有完整的游程")
    m = int(end_idx[-1]) + 1
    payload = (nib[:m] & 7).astype(np.int64)
    starts = np.empty_like(end_idx)
    starts[0] = 0
    starts[1:] = end_idx[:-1] + 1
    vid = np.zeros(m, dtype=np.int64)
    vid[1:] = np.cumsum(ends[: m - 1])
    pos = np.arange(m, dtype=np.int64) - starts[vid]
    if int(pos.max()) > 10:
        raise RvlError("VLE 值过长")
    vals = np.add.reduceat(payload << (3 * pos), starts)
    v = vals.tolist()
    n_vals = len(v)

    spans: list[tuple[int, int, int]] = []  # (像素起点, 个数, 差分值起点)
    i = filled = 0
    while filled < num_pixels:
        if i + 1 >= n_vals:
            raise RvlError(f"RVL 数据提前结束（已填充 {filled}/{num_pixels} 像素）")
        zeros, nonzeros = v[i], v[i + 1]
        i += 2
        if zeros == 0 and nonzeros == 0:
            raise RvlError("出现零长度游程")
        filled += zeros
        if nonzeros:
            if i + nonzeros > n_vals:
                raise RvlError("差分值数量不足")
            spans.append((filled, nonzeros, i))
            i += nonzeros
            filled += nonzeros
        if filled > num_pixels:
            raise RvlError(f"游程越界（{filled} > {num_pixels}）")

    consumed_words = int(end_idx[i - 1]) // 8 + 1
    if consumed_words != len(words):
        raise RvlError(f"RVL 长度与解码消耗不一致（消耗 {consumed_words} 字，实际 {len(words)} 字）")

    out = np.zeros(num_pixels, dtype=np.uint16)
    if spans:
        sp = np.asarray(spans, dtype=np.int64)
        counts = sp[:, 1]
        offs = np.repeat(np.cumsum(counts) - counts, counts)
        within = np.arange(int(counts.sum()), dtype=np.int64) - offs
        pix = np.repeat(sp[:, 0], counts) + within
        zz = vals[np.repeat(sp[:, 2], counts) + within]
        delta = (zz >> 1) ^ -(zz & 1)
        cur = np.cumsum(delta)
        if cur.size and (int(cur.min()) <= 0 or int(cur.max()) > 0xFFFF):
            raise RvlError("非零游程解出越界或为 0 的深度值")
        out[pix] = cur.astype(np.uint16)
    return out


def decode_payload(data: bytes) -> tuple[dict, np.ndarray]:
    """解析并解码完整 compressedDepth RVL 负载，返回 (头信息, rows×cols uint16 数组)。"""
    head = parse_payload(data)
    n = head["cols"] * head["rows"]
    if n <= 0 or n > 1 << 26:
        raise RvlError(f"宽高非法：{head['cols']}×{head['rows']}")
    img = decode_rvl(data[CONFIG_HEADER_SIZE + DIMS_SIZE :], n)
    return head, img.reshape(head["rows"], head["cols"])
