import json

data = json.load(open('/mnt/YourPool/manxus/data/lazada_product_list.json'))
inner = data.get('data', {}).get('data', {})
table = inner.get('table', {})
rows = table.get('dataSource', [])
print('Total products: ' + str(len(rows)))
print()
if rows:
    p = rows[0]
    print('=== Product top-level keys ===')
    for k, v in p.items():
        if k != 'subDataSource':
            print('  ' + k + ': ' + str(v)[:120])
    print()
    print('=== subDataSource (SKUs) ===')
    subs = p.get('subDataSource', [])
    print('SKU count: ' + str(len(subs)))
    if subs:
        sku = subs[0]
        print('SKU keys:')
        for k, v in sku.items():
            print('  ' + k + ': ' + str(v)[:120])