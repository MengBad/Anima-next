import io, ast

p = "anima/tools/memoh.py"
s = io.open(p, encoding="utf-8").read()
i = s.find("def _strip_backend_scaffolding")
assert i > 0
j = s.find("async def delegate", i)
assert j > i
impl = "\n".join([
    "def _strip_backend_scaffolding(text: str) -> str:",
    "    " + chr(34)*3 + "清洗执行后端的内部协议标记，只留人话。" + chr(34)*3,
    "    from anima.tools.memoh_sanitize import sanitize",
    "    return sanitize(text)",
    "",
    "",
])
s = s[:i] + impl + "\n" + s[j:]
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("memoh.py stripper replaced")

p2 = "G:/Anima/_memoh_bridge.py"
b = io.open(p2, encoding="utf-8").read()
old2 = "        for item in reversed(list(msgs.get(" + chr(34) + "items" + chr(34) + " or [])):"
new2 = "        from anima.tools.memoh_sanitize import sanitize\n        for item in reversed(list(msgs.get(" + chr(34) + "items" + chr(34) + " or [])):"
assert b.count(old2) == 1, b.count(old2)
b = b.replace(old2, new2, 1)
old3 = "            joined = " + chr(34) + chr(34) + ".join(texts).strip()\n            if joined:\n                return joined"
new3 = "            joined = sanitize(" + chr(34) + chr(34) + ".join(texts))\n            if joined:\n                return joined"
assert b.count(old3) == 1, b.count(old3)
b = b.replace(old3, new3, 1)
io.open(p2, "w", encoding="utf-8", newline="\n").write(b)
print("bridge patched")

for f in ["anima/tools/memoh.py", "G:/Anima/_memoh_bridge.py", "anima/tools/memoh_sanitize.py"]:
    ast.parse(io.open(f, encoding="utf-8").read())
print("SYNTAX_OK")