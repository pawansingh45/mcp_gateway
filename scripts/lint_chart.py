#!/usr/bin/env python3
"""Minimal structural lint for Helm templates for environments WITHOUT helm.
Checks delimiter balance and if/range/with/define/end pairing. It is NOT a replacement for `helm lint`
and `helm template` (run those in CI/locally); it just catches typos early."""
import re, sys, pathlib, yaml

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "deploy/helm/mcp-platform")
bad = 0
for f in sorted((root / "templates").iterdir()):
    text = f.read_text()
    if text.count("{{") != text.count("}}"):
        print(f"{f.name}: unbalanced delimiters"); bad += 1; continue
    stack = []
    for m in re.finditer(r"\{\{-?\s*(/\*.*?\*/|(.*?))\s*-?\}\}", text, re.S):
        body = (m.group(2) or "").strip()
        if not body:
            continue
        word = body.split()[0]
        if word in ("if", "range", "with", "define", "block"):
            stack.append((word, text[:m.start()].count("\n") + 1))
        elif word == "end":
            if not stack:
                print(f"{f.name}: stray end at line {text[:m.start()].count(chr(10)) + 1}"); bad += 1
            else:
                stack.pop()
    for word, line in stack:
        print(f"{f.name}: unclosed {word} from line {line}"); bad += 1
for v in root.glob("values*.yaml"):
    try:
        yaml.safe_load(v.read_text())
    except yaml.YAMLError as e:
        print(f"{v.name}: {e}"); bad += 1
print("chart lint:", "FAILED" if bad else "ok (structure only; run `helm lint` for the real thing)")
sys.exit(1 if bad else 0)
