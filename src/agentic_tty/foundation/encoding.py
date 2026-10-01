"""编码与增量解码。

字节流才是真源，解码只服务于"给人看 / 给正则匹配"的视图，因此遇到无法
解码的字节用替换字符表示，而不抛错中断。跨块的字节序列不会被截断。
"""

from __future__ import annotations

import codecs


class StreamDecoder:
    """增量解码器：跨块的字节序列不会被打断成替换字符。"""

    def __init__(self, encoding: str = "utf-8", errors: str = "replace") -> None:
        self._encoding = encoding
        self._errors = errors
        self._decoder = codecs.getincrementaldecoder(encoding)(errors)

    @property
    def encoding(self) -> str:
        return self._encoding

    def decode(self, data: bytes, final: bool = False) -> str:
        """解码一段字节；`final=True` 表示流已结束，残余字节按不完整处理。"""
        return self._decoder.decode(data, final)

    def reset(self) -> None:
        self._decoder.reset()
