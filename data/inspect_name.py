import json
data = json.load(open('/mnt/GombakWale/inventory-sync/data/lazada_product_list.json'))
rows = data.get('data',{}).get('data',{}).get('table',{}).get('dataSource',[])
if rows:
    print('itemDesc keys: ' + str(list(rows[0].get('itemDesc',{}).keys())))
    print('itemDesc value: ' + str(rows[0].get('itemDesc',''))[:300])