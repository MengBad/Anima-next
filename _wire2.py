import io, ast
p2 = "G:/Anima/_memoh_bridge.py"
b = io.open(p2, encoding="utf-8").read()
D = chr(34)

old2 = "        for item in reversed(list(msgs.get(" + D + "items" + D + " or [])):"
new2 = "        from anima.tools.memoh_sanitize import sanitize\n        for item in reversed(list(msgs.get(" + D + "items" + D + " or [])):"
assert b.count(old2) == 1, b.count(old2)
b = b.replace(old2, new2, 1)

old3 = "            joined = " + D + D + ".join(texts).strip()\n            if joined:\n                return joined"
new3 = "            joined = sanitize(" + D + D + ".join(texts))\n            if joined:\n                return joined"
assert b.count(old3) == 1, b.count(old3)
b = b.replace(old3, new3, 1)
io.open(p2, "w", encoding="utf-8", newline="\n").write(b)
print("bridge patched")
for f in ["anima/tools/memoh.py", "G:/Anima/_memoh_bridge.py", "anima/tools/memoh_sanitize.py"]:
    ast.parse(io.open(f, encoding="utf-8").read())
print("SYNTAX_OK")