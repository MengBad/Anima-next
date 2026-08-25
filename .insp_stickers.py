import json, os, collections
p = 'data/stickers.json'
if not os.path.exists(p):
    print('NO stickers.json'); raise SystemExit
d = json.load(open(p, encoding='utf8'))
print('top-level:', type(d).__name__, 'len:', len(d) if hasattr(d, '__len__') else '?')
def getf(m, k):
    return m.get(k) if isinstance(m, dict) else None
if isinstance(d, dict):
    print('dict keys:', list(d)[:12])
    mapping = None
    for k, v in d.items():
        if isinstance(v, dict) and v:
            n = sum(1 for x in v.values() if isinstance(x, dict))
            if n == len(v) and n > 3:
                mapping = v; print('mapping under key:', k, 'n=', n); break
    if mapping is None:
        mapping = d.get('stickers') or d.get('items') or {}
    items = list(mapping.values())
else:
    items = d
status = collections.Counter(getf(m, 'status') for m in items)
print('by status:', dict(status))
ev = sum(1 for m in items if getf(m, 'is_sticker') is True and (getf(m, 'labels') or getf(m, 'emotion')))
nov = sum(1 for m in items if getf(m, 'is_sticker') not in (True, False))
print('is_sticker=True+evidence:', ev)
print('no explicit is_sticker verdict:', nov)
print('scope pub/local:', dict(collections.Counter(getf(m,'scope') for m in items)))
if items:
    print('sample keys:', sorted(getf(items[0], 'k') or k for k in items[0])[:16])
