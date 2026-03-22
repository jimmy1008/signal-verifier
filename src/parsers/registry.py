"""
Parser 註冊中心

職責：管理所有可用的 parser，根據名稱取得對應 parser
"""

from __future__ import annotations

from src.parsers.base import BaseParser
from src.parsers.default_parser import DefaultParser
from src.parsers.crt_sniper_parser import CrtSniperParser

# 所有可用 parser
_REGISTRY: dict[str, BaseParser] = {}


def register_parser(parser: BaseParser) -> None:
    _REGISTRY[parser.name] = parser


def get_parser(name: str) -> BaseParser:
    if name not in _REGISTRY:
        raise ValueError(f"未知的 parser: {name}，可用: {list(_REGISTRY.keys())}")
    return _REGISTRY[name]


def list_parsers() -> list[str]:
    return list(_REGISTRY.keys())


# 預設註冊
register_parser(DefaultParser())
register_parser(CrtSniperParser())
