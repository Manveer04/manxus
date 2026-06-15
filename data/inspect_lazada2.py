import json

data = json.load(open('/mnt/GombakWale/inventory-sync/data/lazada_product_list.json'))
inner = data.get('data', {}).get('data', {})
print('Top keys: ' + str(list(inner.keys())))
print()

table = inner.get('table', {})
print('Table keys: ' + str(list(table.keys())))
print()

# Rows might be nested inside table
for k, v in table.items():
    print('table[' + k + ']: ' + str(v)[:200])
    print()