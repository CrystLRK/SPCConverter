"""Read the verified Addressables binary catalog without loading the game.

The version-2 layouts and string traversal are taken from the matching native
BinaryStorageBuffer.Reader and ContentCatalogData.ResourceLocator methods.
This reads data only; it never evaluates provider names or internal-id macros.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct


class CatalogError(ValueError):
    pass


class NativeCatalog:
    MAGIC = 0x0DE38942
    NONE = 0xFFFFFFFF
    OFFSET_MASK = 0x3FFFFFFF

    def __init__(self, data: bytes):
        if not 32 <= len(data) <= 64 * 1024 * 1024:
            raise CatalogError("Addressables目录大小越界")
        self.data = data
        self.sha256 = hashlib.sha256(data).hexdigest().upper()
        magic, version, self.keys_offset, *_ = self.words(0, 8)
        if magic != self.MAGIC or version != 2:
            raise CatalogError("仅支持已验证的version 2二进制目录")
        self._strings: dict[tuple[int, str], str | None] = {}
        self._types: dict[int, tuple[str | None, str | None]] = {}
        self._locations: dict[int, dict] = {}
        self.keys: dict[str | int | bool, int] = {}
        self.non_string_keys: list[dict] = []
        for key_id, location_set in self.value_array(self.keys_offset, 2):
            type_id, object_id = self.words(key_id, 2)
            assembly, type_name = self.type_name(type_id)
            if type_name == "System.String":
                string_id, separator = self.words(object_id, 2)
                key = self.string(string_id, chr(separator & 0xFFFF) if separator & 0xFFFF else "")
                if key is None:
                    raise CatalogError("目录键不能为空")
            elif type_name == "System.Int32":
                key = struct.unpack("<i", self.span(object_id, 4))[0]
            else:
                # Other key types are retained diagnostically; they are not
                # silently interpreted as an asset-path string.
                self.non_string_keys.append({"type": type_name, "assembly": assembly,
                                             "objectOffset": object_id, "locationsOffset": location_set})
                continue
            if key in self.keys:
                raise CatalogError(f"目录重复键 {key}")
            self.keys[key] = location_set

    @classmethod
    def from_file(cls, path: Path) -> "NativeCatalog":
        return cls(path.read_bytes())

    def span(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise CatalogError(f"目录读越界 offset={offset} bytes={size}")
        return self.data[offset:offset + size]

    def words(self, offset: int, count: int) -> tuple[int, ...]:
        return struct.unpack("<" + "I" * count, self.span(offset, count * 4))

    def value_array(self, offset: int, width: int = 1) -> list[tuple[int, ...]]:
        if offset == self.NONE:
            return []
        byte_size, = self.words(offset - 4, 1)
        stride = width * 4
        if byte_size % stride:
            raise CatalogError("目录数组长度不是元素宽度的倍数")
        raw = self.span(offset, byte_size)
        return list(struct.iter_unpack("<" + "I" * width, raw))

    def plain_string(self, string_id: int) -> str | None:
        if string_id == self.NONE:
            return None
        if string_id & 0x40000000:
            raise CatalogError("动态片段需要明确的分隔符")
        offset = string_id & self.OFFSET_MASK
        size, = self.words(offset - 4, 1)
        raw = self.span(offset, size)
        encoding = "utf-16-le" if string_id & 0x80000000 else "ascii"
        try:
            return raw.decode(encoding)
        except UnicodeError as exc:
            raise CatalogError(f"目录字符串编码错误 {string_id:#x}") from exc

    def string(self, string_id: int, separator: str = "") -> str | None:
        cache_key = string_id, separator
        if cache_key in self._strings:
            return self._strings[cache_key]
        if string_id == self.NONE:
            return None
        if not separator or not string_id & 0x40000000:
            result = self.plain_string(string_id)
        else:
            visited: set[int] = set()
            pieces: list[str] = []
            node_id = string_id
            while node_id != self.NONE:
                offset = node_id & self.OFFSET_MASK
                if offset in visited or len(visited) >= len(self.data) // 8:
                    raise CatalogError("目录动态字符串链有环/超限")
                visited.add(offset)
                part_id, node_id = self.words(offset, 2)
                part = self.plain_string(part_id)
                if part is None:
                    raise CatalogError("动态字符串片段为空")
                pieces.append(part)
            # Native copy callback fills the target from the end backwards.
            result = separator.join(reversed(pieces))
        self._strings[cache_key] = result
        return result

    def type_name(self, offset: int) -> tuple[str | None, str | None]:
        if offset not in self._types:
            assembly_id, class_id = self.words(offset, 2)
            self._types[offset] = self.string(assembly_id, "."), self.string(class_id, ".")
        return self._types[offset]

    def location(self, offset: int) -> dict:
        if offset not in self._locations:
            primary, internal, provider, dependencies, dependency_hash, extra, type_id = self.words(offset, 7)
            self._locations[offset] = {
                "dataOffset": offset, "primaryKey": self.string(primary, "/"),
                "internalId": self.string(internal, "/"), "providerId": self.string(provider, "."),
                "dependencyOffsets": [value for value, in self.value_array(dependencies)],
                "dependencyHash": dependency_hash, "extraDataOffset": extra,
                "resourceAssembly": self.type_name(type_id)[0], "resourceType": self.type_name(type_id)[1],
            }
        return self._locations[offset]

    def locate(self, key: str) -> list[dict]:
        if key not in self.keys:
            return []
        # ProcessObjectArray<IResourceLocation> invokes the typed serializer
        # directly at each array offset, rather than reading ObjectTypeData.
        return [self.location(offset) for offset, in self.value_array(self.keys[key])]

    def dependencies(self, location: dict) -> list[dict]:
        seen = {location["dataOffset"]}
        result: list[dict] = []
        pending = list(location["dependencyOffsets"])
        while pending:
            offset = pending.pop()
            if offset in seen:
                continue
            if len(seen) > len(self.data) // 28:
                raise CatalogError("资源依赖图超限")
            seen.add(offset)
            item = self.location(offset)
            result.append(item)
            pending.extend(item["dependencyOffsets"])
        return result

    def main_bundle(self, location: dict) -> dict:
        """Match InternalOp.LoadBundleFromDependecies: first bundle result."""
        for offset in location["dependencyOffsets"]:
            dependency = self.location(offset)
            if dependency["resourceType"] == "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource":
                return dependency
        raise CatalogError("资源没有已验证的主AssetBundle依赖")
