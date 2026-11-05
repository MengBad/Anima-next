import pathlib
p = pathlib.Path("anima/tools/memoh.py")
s = p.read_text(encoding="utf-8")

old = "".join([
  "        except MemohUnavailable as exc:",
  "            logger.warning(\"[memoh] 委派失败：%s\", exc)",
  "            return f\"工具暂时不可用：执行后端没响应（{exc}）。\"",
])
new = "".join([
  "        except MemohUnavailable as exc:",
  "            # 绝不把 exec 细节写进返回值：模型可见 ⇒ 可能原样出站",
  "            logger.warning(\"[memoh] 委派失败：%s\", exc)",
  "            return \"工具暂时不可用：执行后端没响应，稍后再试。\"",
])
assert s.count(old) == 1, s.count(old)
s = s.replace(old, new, 1)

old2 = "".join([
  "        except ValueError as exc:",
  "            return f\"错误: {exc}\"",
])
new2 = "".join([
  "        except ValueError:",
  "            return \"错误: 任务描述不能为空。\"",
])
assert s.count(old2) == 1, s.count(old2)
s = s.replace(old2, new2, 1)

p.write_text(s, encoding="utf-8")
print("OK")