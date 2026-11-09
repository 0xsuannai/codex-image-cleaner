"""Bounded, strict JSON reads for files that may be damaged or externally edited."""

import json
import math
import re
from pathlib import Path

MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 192 * 1024 * 1024
MAX_CACHE_EDIT_BYTES = 128 * 1024 * 1024
MAX_RECORDS = 1_000_000
SURROGATE = re.compile(r"[\ud800-\udfff]")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段，已停止以避免歧义")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("JSON 包含非有限数值")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON 数值超过有限范围")
    return result


def loads(value):
    result = json.loads(
        value, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
    )
    pending = [result]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and SURROGATE.search(item):
            raise ValueError("JSON 包含无效 Unicode 字符")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return result


def read_record(stream):
    raw = stream.readline(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("单条会话记录超过 64 MiB 安全上限，未修改原会话")
    return raw


def records(stream):
    count = 0
    while raw := read_record(stream):
        count += 1
        if count > MAX_RECORDS:
            raise ValueError("会话记录数超过安全上限，未修改原会话")
        yield raw


def read_file(path: Path, limit=MAX_MANIFEST_BYTES):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("JSON 文件超过安全读取上限")
    return loads(raw)
