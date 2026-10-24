"""Order-preserving key encoding, and the row format.

encode_key() turns SQL values into bytes whose plain byte order is the SQL
order: NULL < numbers < text, numbers by value, text by UTF-8 bytes (the
same as SQLite's BINARY collation). That lets one B+tree implementation
serve both tables (key = rowid) and indexes (key = column value + rowid).

    NULL     01
    number   02 + IEEE double with its bits adjusted to sort (8 bytes)
                + the exact int64 as a tiebreaker (8 bytes, 0 for reals)
    text     03 + UTF-8 with 00 escaped as 00 FF, then 00 00

Integers and reals share a tag and compare by value. The tiebreaker keeps
integers above 2**53, where doubles get gaps, distinct and in order.
"""

import math
import struct

NULL_TAG = b"\x01"
NUM_TAG = b"\x02"
TEXT_TAG = b"\x03"

_SIGN = 1 << 63
_MASK = (1 << 64) - 1
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def encode_int(n):
    return struct.pack(">Q", (n + _SIGN) & _MASK)


def decode_int(b):
    return struct.unpack(">Q", b)[0] - _SIGN


def _float_bits(x):
    bits = struct.unpack(">Q", struct.pack(">d", x + 0.0))[0]  # + 0.0 turns -0.0 into 0.0
    # positive: set the sign bit so they sort above negatives.
    # negative: flip everything so bigger magnitudes sort lower.
    return bits | _SIGN if not bits & _SIGN else ~bits & _MASK


def _encode_number(v):
    # two numbers only reach the tiebreaker when they round to the same double.
    # an integral real uses its own exact value there, so 1 and 1.0 encode the
    # same and 2**53 + 3 still sorts below the double 2**53 + 4
    if isinstance(v, float):
        exact = int(v) if v.is_integer() and INT64_MIN <= v <= INT64_MAX else 0
    else:
        exact = v
    return NUM_TAG + struct.pack(">Q", _float_bits(float(v))) + encode_int(exact)


def encode_value(v):
    if v is None:
        return NULL_TAG
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return NULL_TAG  # SQL has no NaN; SQLite stores it as NULL too
        return _encode_number(v)
    if isinstance(v, str):
        return TEXT_TAG + v.encode().replace(b"\x00", b"\x00\xff") + b"\x00\x00"
    raise TypeError(f"can't encode {type(v).__name__}")


def encode_key(*values):
    return b"".join(encode_value(v) for v in values)


def rowid_key(rowid):
    return encode_int(rowid)


def rowid_from_key(key):
    return decode_int(key)


def rowid_from_index_key(key):
    # index keys end in the rowid's 8-byte integer encoding
    return decode_int(key[-8:])


# --- rows -------------------------------------------------------------------
#
#   count u16, then per value: tag u8 + payload
#     0 NULL | 1 int64 (8 bytes) | 3 real (8 bytes) | 4 text (len u32 + utf-8)

_I64 = struct.Struct(">q")
_F64 = struct.Struct(">d")


def encode_row(values):
    out = [struct.pack(">H", len(values))]
    for v in values:
        if v is None:
            out.append(b"\x00")
        elif isinstance(v, int):
            out.append(b"\x01" + _I64.pack(v))
        elif isinstance(v, float):
            out.append(b"\x03" + _F64.pack(v))
        elif isinstance(v, str):
            data = v.encode()
            out.append(b"\x04" + struct.pack(">I", len(data)) + data)
        else:
            raise TypeError(f"can't store {type(v).__name__}")
    return b"".join(out)


def decode_row(data):
    (count,) = struct.unpack_from(">H", data, 0)
    pos = 2
    values = []
    for _ in range(count):
        tag = data[pos]
        pos += 1
        if tag == 0:
            values.append(None)
        elif tag == 1:
            values.append(_I64.unpack_from(data, pos)[0])
            pos += 8
        elif tag == 3:
            values.append(_F64.unpack_from(data, pos)[0])
            pos += 8
        elif tag == 4:
            (n,) = struct.unpack_from(">I", data, pos)
            values.append(data[pos + 4:pos + 4 + n].decode())
            pos += 4 + n
        else:
            raise ValueError(f"bad value tag {tag}")
    return values
