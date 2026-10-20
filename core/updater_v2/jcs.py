# -*- coding: utf-8 -*-
"""
core/updater_v2/jcs.py: RFC 8785 JSON Canonicalization Scheme (JCS) 구현체

RFC 8785 명세 요구사항:
1. Whitespace: 콜론(:)과 쉼표(,) 전후 일체의 공백 제거
2. Key Sorting: UTF-16 코드 유닛 사전순(lexicographical) 정렬
   - BMP 문자(U+0000~U+FFFF): 코드포인트 자체 비교
   - Supplementary 문자(U+10000 이상): UTF-16 서러게이트 페어(High/Low) 단위 분해 비교
3. Number Formatting:
   - 정수는 소수점 없이 표현
   - 부동소수점은 IEEE 754 유효 범위 내에서 표준 정규화
   - NaN / Infinity는 허용하지 않음 (ValueError 발생)
4. String Escaping:
   - 필수 이스케이프 문자만 역슬래시 처리: \b, \f, \n, \r, \t, \", \\, \u0000~\u001f
   - 기타 모든 유니코드 문자는 이스케이프 없이 순수 UTF-8 바이트로 직렬화
5. Output: UTF-8 인코딩된 bytes 반환
"""

import json
import math
from typing import Any, List, Union


def _utf16_code_units(s: str) -> List[int]:
    """문자열을 UTF-16 코드 유닛 시퀀스로 변환 (RFC 8785 Section 3.2.3)."""
    units: List[int] = []
    for ch in s:
        cp = ord(ch)
        if cp < 0x10000:
            units.append(cp)
        else:
            cp -= 0x10000
            high = 0xD800 + (cp >> 10)
            low = 0xDC00 + (cp & 0x3FF)
            units.append(high)
            units.append(low)
    return units


def _canonicalize_number(val: Union[int, float]) -> str:
    """RFC 8785 Section 3.2.2 숫자 정규화."""
    if isinstance(val, bool):
        raise TypeError("Boolean value passed to number canonicalizer")
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError(f"RFC 8785 rejects NaN and Infinity: {val}")
        if val == 0.0:
            # -0.0 -> 0 (RFC 8785 rule)
            return "0"
        # Python repr float formatting produces minimal IEEE 754 representation
        # Ensure lowercase 'e' and strip redundant '+'
        s = repr(val)
        if "e" in s or "E" in s:
            parts = s.lower().split("e")
            mantissa = parts[0]
            exp = int(parts[1])
            return f"{mantissa}e{exp}"
        if s.endswith(".0"):
            s = s[:-2]
        return s
    raise TypeError(f"Unsupported number type: {type(val)}")


def _serialize_to_canonical_str(obj: Any) -> str:
    """객체를 RFC 8785 정규화 JSON 문자열로 직렬화."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return _canonicalize_number(obj)
    if isinstance(obj, str):
        # json.dumps ensures correct control-character escaping while preserving unicode
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if isinstance(obj, (list, tuple)):
        elems = [_serialize_to_canonical_str(x) for x in obj]
        return "[" + ",".join(elems) + "]"
    if isinstance(obj, dict):
        # RFC 8785 Section 3.2.3: Sort keys by UTF-16 code units lexicographically
        sorted_keys = sorted(obj.keys(), key=_utf16_code_units)
        pairs: List[str] = []
        for k in sorted_keys:
            if not isinstance(k, str):
                raise TypeError(f"Dictionary keys must be strings, got {type(k)}")
            k_str = json.dumps(k, ensure_ascii=False, separators=(",", ":"))
            v_str = _serialize_to_canonical_str(obj[k])
            pairs.append(f"{k_str}:{v_str}")
        return "{" + ",".join(pairs) + "}"
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable under RFC 8785")


def canonicalize(obj: Any) -> bytes:
    """
    주어진 파이썬 객체를 RFC 8785 JCS 사양에 따라 정규화된 UTF-8 바이트로 변환합니다.

    Args:
        obj: 직렬화할 파이썬 기본 데이터 객체 (dict, list, str, int, float, bool, None)

    Returns:
        RFC 8785 canonical bytes (UTF-8)
    """
    canonical_str = _serialize_to_canonical_str(obj)
    return canonical_str.encode("utf-8")
