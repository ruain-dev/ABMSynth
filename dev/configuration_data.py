"""Bounded inert configuration syntax; no Qt,storage paths or domain imports.

All current-format structured settings share this boundary. Domain owners
validate allowed attributes,types,ranges and lifecycle after syntax admission.
"""
import json

INI_MAX_BYTES = 4 * 1024 * 1024
INI_MAX_LINE = 64 * 1024


def read_settings_json(raw):
    """Bounded inert configuration data;duplicates and nonfinite numbers fail."""
    if not isinstance(raw,(str,bytes)) or len(raw)>INI_MAX_BYTES:
        raise ValueError('settings JSON size/type')
    if isinstance(raw,str) and len(raw.encode('utf-8'))>INI_MAX_BYTES:
        raise ValueError('settings JSON size')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate settings property')
            result[key]=value
        return result
    def constant(_value):raise ValueError('nonfinite settings number')
    try:
        result=json.loads(raw,object_pairs_hook=unique,parse_constant=constant)
        pending=[(result,0)]
        while pending:
            value,depth=pending.pop()
            if depth>32:raise ValueError('settings JSON depth')
            if type(value) is float:
                import math
                if not math.isfinite(value):raise ValueError('nonfinite settings number')
            if isinstance(value,(dict,list)):
                pending.extend((item,depth+1) for item in (value.values() if isinstance(value,dict) else value))
        return result
    except (RecursionError,OverflowError,UnicodeError) as exc:
        raise ValueError('settings JSON structure') from exc


