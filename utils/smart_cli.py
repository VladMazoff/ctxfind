"""
smart_cli.py — Context-Aware Argument Parser (v4.2)
Fix: Unregistered --flags are now treated as strings, not keys.
"""
import sys
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

class _Namespace:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
    def __repr__(self):
        items = (f"{k}={v!r}" for k, v in sorted(self.__dict__.items()))
        return f"Namespace({', '.join(items)})"

class CLIParseError(Exception): pass

class _CoreParser:
    def __init__(self, tokens: List[str], presets: Dict[str, Dict], strict: bool = False):
        self.tokens = tokens; self.presets = presets; self.strict = strict

    def _base_score(self, token: str) -> Dict[str, float]:
        s = {"key": 0.0, "path": 0.0, "string": 0.0}
        clean = token.lstrip("-")
        
        # 🔑 1. Строго только зарегистрированные пресеты
        if clean in self.presets:
            meta = self.presets[clean]
            s[meta["type"]] = meta["weight"]
            if meta.get("context_sensitive") and not self.strict:
                s[meta["type"]] = 0.65; s["string"] = 0.55
            return s

        # 🚫 2. Если начинается с -- но НЕ в пресетах → это СТРОКА (защита от ложных ключей)
        # Это решает баг с --history-h, --no-such-flag и т.д.
        if token.startswith("--") and clean not in self.presets:
            s["string"] = 0.90
            return s

        # 📁 3. Путь
        if re.search(r'(?:[A-Za-z]:[\\/]|[\\/]|\. {1,2}[\\/])|(\.[a-zA-Z0-9]{1,6}$)', token):
            s["path"] = 0.85
            if re.search(r'[;,!:]$', token): s["path"] = 0.20

        # 🔑 4. Свободные ключи (только короткие -x или key=value, если не попали выше)
        # Убрали широкое совпадение ^--... чтобы не перехватывать незарегистрированные флаги
        if re.match(r'^-[a-zA-Z0-9]$', token) or re.match(r'^[a-zA-Z_][\w\-]*=.+$', token):
            s["key"] = 0.85
        elif token.lower() in {"fast", "slow", "quiet", "q", "dry-run", "d"}:
            s["key"] = 0.80

        # 📝 5. Строка / Код
        if re.search(r'[{}()\[\];+*^%~|&]', token):
            s["string"] = 0.95
            s["path"] = max(s["path"] - 0.50, 0.0)
            s["key"] = max(s["key"] - 0.50, 0.0)
        elif ' = ' in token or token.startswith('= ') or token.endswith(' ='):
            s["string"] += 0.30
            
        # Дефолт для одиночных слов
        if max(s.values()) < 0.5: s["string"] = 0.60
        return s

    def run(self) -> Tuple[str, List[str], Dict[str, Any]]:
        if not self.tokens: raise CLIParseError("❌ Аргументы не переданы.")
        scores = [self._base_score(t) for t in self.tokens]
        
        def apply_context(sc_list, direction):
            for i in range(len(sc_list)):
                j = i + direction
                if 0 <= j < len(sc_list):
                    nb = sc_list[j]
                    for cat in ("key", "path", "string"):
                        if nb.get(cat, 0) > 0.8: sc_list[i][cat] += 0.15
        apply_context(scores, 1); apply_context(scores, -1)
        
        merged, ambiguities = [], []
        for i, sc in enumerate(scores):
            total = sum(sc.values())
            if total > 1.0: sc = {k: v/total for k, v in sc.items()}
            winner = max(sc, key=sc.get); conf = sc[winner]
            sv = sorted(sc.values(), reverse=True)
            if len(sv) > 1 and (sv[0] - sv[1]) < 0.20 and conf < 0.65:
                ambiguities.append(i)
            merged.append({"token": self.tokens[i], "category": winner, "confidence": conf})

        str_idx = [i for i, m in enumerate(merged) if m["category"] == "string"]
        if not str_idx and not self.strict:
            cands = [(i, m) for i, m in enumerate(merged) if m["category"] in ("key", "path")]
            if cands: cands.sort(key=lambda x: x[1]["confidence"]); merged[cands[0][0]]["category"] = "string"
        if self.strict and not str_idx: raise CLIParseError("❌ В strict-режиме обязательная строка должна быть задана явно.")

        str_parts, paths_raw, flags = [], [], {}
        i = 0; n = len(merged)
        while i < n:
            m = merged[i]; tok, cat = m["token"], m["category"]
            if cat == "string": str_parts.append(tok)
            elif cat == "path": paths_raw.append(tok)
            elif cat == "key":
                clean = tok.lstrip("-")
                meta = self.presets.get(clean, {})
                if "=" in tok:
                    k, v = tok.split("=", 1); flags.setdefault(k.lstrip("-"), []).append(v)
                elif meta.get("expects_value") and i + 1 < n:
                    nxt = merged[i+1]["token"]
                    if not nxt.startswith("-"): flags.setdefault(clean, []).append(nxt); i += 1
                    else: flags.setdefault(clean, []).append(True)
                else: flags.setdefault(clean, []).append(True)
            i += 1

        mandatory_str = " ".join(str_parts).strip()
        if not mandatory_str:
            for k in ("str", "string", "s", "code", "query", "text"):
                if k in flags: mandatory_str = flags[k][0]; flags.pop(k); break
        if len(ambiguities) > 2 and not self.strict:
            amb = [merged[i]["token"] for i in ambiguities]
            raise CLIParseError(f"❌ Неразрешимая неоднозначность: {amb}")
        return mandatory_str, paths_raw, flags


class SmartArgumentParser:
    def __init__(self, prog: Optional[str] = None, description: str = "", 
                 epilog: str = "", strict: bool = False, **kwargs):
        self.prog = prog or (sys.argv[0] if sys.argv else "app.py")
        self.description = description
        self.epilog = epilog
        self.strict = strict
        self.presets: Dict[str, Dict] = {}
        self.positionals: List[Dict] = []
        self._registered: List[Dict] = []
        
    def add_argument(self, *args, **kwargs) -> 'SmartArgumentParser':
        is_pos = args and not args[0].startswith('-')
        is_bool = kwargs.get("action") == "store_true"
        is_append = kwargs.get("action") == "append"
        
        dest = kwargs.get("dest")
        if dest is None:
            if is_pos: dest = args[0]
            else:
                opt_names = [f.lstrip('-') for f in args if f.startswith('-')]
                dest = max(opt_names, key=len, default=args[0].lstrip('-')).replace('-', '_')

        meta = {
            "type": "bool" if is_bool else ("append" if is_append else "key"),
            "type_func": kwargs.get("type", str if not is_bool else None),
            "default": kwargs.get("default"),
            "nargs": kwargs.get("nargs"),
            "dest": dest,
            "action": kwargs.get("action", "store"),
            "choices": kwargs.get("choices"),
            "help": kwargs.get("help", ""),
            "weight": kwargs.get("weight", 1.0),
            "context_sensitive": kwargs.get("context_sensitive", False),
            "expects_value": not is_bool and not is_pos,
            "is_short": any(len(f.lstrip("-")) == 1 for f in args if f.startswith("-"))
        }
        
        if is_pos:
            meta["positional"] = True
            self.positionals.append(meta)
        else:
            meta["positional"] = False
            for f in args: self.presets[f.lstrip("-")] = meta
        self._registered.append({"flags": args, "meta": meta})
        return self

    def _apply_types(self, key: str, values: List[Any]) -> List[Any]:
        meta = self.presets.get(key) or next((m for m in self.positionals if m.get("dest")==key), None)
        if not meta: return values
        res = []
        for v in values:
            if meta.get("type_func") and v is not True:
                try: v = meta["type_func"](v)
                except (ValueError, TypeError): pass
            if meta.get("choices") and v not in meta["choices"]:
                print(f"⚠️ Invalid '{v}' for --{key}. Expected: {meta['choices']}", file=sys.stderr)
            res.append(v)
        return res

    def parse_args(self, args: Optional[List[str]] = None) -> _Namespace:
        if args is None: args = sys.argv[1:]
        if not args or any(f in args for f in ("--help", "-h")):
            self.print_help(); sys.exit(0)

        try:
            core = _CoreParser(args, self.presets, strict=self.strict)
            target, paths_raw, flags = core.run()
        except CLIParseError as e:
            print(str(e), file=sys.stderr); sys.exit(1)

        ns = _Namespace()
        if len(self.positionals) > 0:
            p0 = self.positionals[0]
            dest0 = p0["dest"] or "query"
            setattr(ns, dest0, target or p0.get("default"))
        if len(self.positionals) > 1:
            p1 = self.positionals[1]
            dest1 = p1["dest"] or "paths"
            val = paths_raw if paths_raw else (p1.get("default") or [])
            if not isinstance(val, list): val = [val]
            setattr(ns, dest1, self._apply_types(dest1, val))

        for clean, vals in flags.items():
            meta = self.presets.get(clean)
            if not meta: continue
            dest = meta["dest"]
            processed = self._apply_types(clean, vals)
            if meta["action"] == "append": setattr(ns, dest, processed)
            elif meta["action"] == "store_true": setattr(ns, dest, True)
            else: setattr(ns, dest, processed[0] if len(processed) == 1 else processed)

        for name, meta in self.presets.items():
            dest = meta["dest"]
            if not hasattr(ns, dest):
                default = meta.get("default")
                if meta["action"] == "append": setattr(ns, dest, default or [])
                elif meta["action"] == "store_true": setattr(ns, dest, False)
                else: setattr(ns, dest, default)
                
        for r in self._registered:
            m = r["meta"]
            if m.get("dest") and m["dest"] != r["flags"][0].lstrip("-").replace("-", "_"):
                src_name = r["flags"][0].lstrip("-").replace("-", "_")
                if hasattr(ns, src_name):
                    setattr(ns, m["dest"], getattr(ns, src_name))
                    delattr(ns, src_name)
        return ns

    def print_help(self):
        usage = [f"usage: {self.prog}"]
        for p in self.positionals:
            d = p["dest"] or "VALUE"
            usage.append(d.upper() if p.get("nargs") != "*" else f"[{d.upper()} ...]")
        for f_info in self._registered:
            for f in f_info["flags"]:
                if f.startswith("-"):
                    c = f.lstrip("-")
                    if c in ("help", "h"): continue
                    m = self.presets[c]
                    prefix = "--" if not m.get("is_short") else "-"
                    val = f" {c.upper()}" if m["action"] not in ("store_true", "append") else ""
                    usage.append(f"[{prefix}{c}{val}]")

        lines = [" ".join(usage), "", self.description, "", "optional arguments:"]
        for f_info in self._registered:
            for f in f_info["flags"]:
                if f.startswith("-"):
                    c = f.lstrip("-")
                    if c in ("help", "h"): continue
                    m = self.presets[c]
                    prefix = "--" if not m.get("is_short") else "-"
                    val = f" {c.upper()}" if m["action"] not in ("store_true", "append") else ""
                    desc = m.get("help", "")
                    default = f" (default: {m['default']})" if m.get("default") is not None else ""
                    choices = f" (choices: {', '.join(map(str, m['choices']))})" if m.get("choices") else ""
                    lines.append(f"  {prefix}{c}{val: <15} {desc}{default}{choices}")

        if self.positionals:
            lines.append("\npositional arguments:")
            for p in self.positionals:
                d = p["dest"] or "VALUE"
                desc = p.get("help", "")
                default = f" (default: {p['default']})" if p.get("default") is not None else ""
                lines.append(f"  {d: <15} {desc}{default}")

        if self.epilog:
            lines.append(f"\n{self.epilog}")
        print("\n".join(lines))